"""The file queue.

    $ROUNDTABLE_SPOOL/
        queue/     jobs waiting, oldest first
        running/   the one job a worker has claimed
        done/      finished, with the session directory recorded
        failed/    what went wrong, kept for reading
        logs/      runner output, one file per job
        worker.json  heartbeat: which worker, doing what, since when
        cancel.json  a standing request to stop one job, if any

There is no lock, no database and no daemon protocol. A worker claims a job by
*renaming* it out of ``queue/`` -- rename is atomic within a filesystem, so if
two workers ever race, exactly one wins and the loser sees FileNotFoundError.
Everything is inspectable with ``ls``, and nothing is lost if the machine dies
mid-run: the job file is still sitting in ``running/``. Nothing *else* notices
that, though, which is what ``orphans``/``reap`` are for -- a claim whose worker
is gone has to be failed by whoever comes next, or it reads as running forever.
"""
import json
import os
import time
import uuid

STATES = ("queue", "running", "done", "failed")

DEFAULT_SPOOL = os.environ.get(
    "ROUNDTABLE_SPOOL",
    os.path.join(os.environ.get("XDG_STATE_HOME",
                                os.path.expanduser("~/.local/state")),
                 "roundtable"))


def paths(spool=None):
    spool = spool or DEFAULT_SPOOL
    return {name: os.path.join(spool, name) for name in STATES + ("logs",)}


def ensure(spool=None):
    """Create the spool layout. Safe to call every time."""
    spool = spool or DEFAULT_SPOOL
    for path in paths(spool).values():
        os.makedirs(path, exist_ok=True)
    return spool


def write_atomic(path, text):
    """Write via temp file + rename, so no reader ever sees a partial file."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def submit(job, spool=None):
    """Put a job in the queue. -> job id.

    The file is written under a dot-name first and then renamed in, so a worker
    scanning the directory can never pick up a half-written job.
    """
    spool = ensure(spool)
    job = dict(job)
    job.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
    queue = paths(spool)["queue"]
    # pid % 10000 alone is not unique within a second: a long-running caller
    # (serve.py handling two rapid submissions) could mint the same id twice
    # and silently clobber the first job on rename. A few random hex digits on
    # top makes a same-second collision astronomically unlikely without giving
    # up the human-readable timestamp prefix.
    job_id = job.get("id") or "%s-%04d-%s" % (
        time.strftime("%Y%m%d-%H%M%S"), os.getpid() % 10000, uuid.uuid4().hex[:4])
    job["id"] = job_id
    staging = os.path.join(queue, "." + job_id + ".json")
    with open(staging, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(staging, os.path.join(queue, job_id + ".json"))
    return job_id


def pending(spool=None):
    """Queued job files, oldest first. Dot-files are still being written."""
    queue = paths(spool)["queue"]
    if not os.path.isdir(queue):
        return []
    names = [n for n in os.listdir(queue)
             if n.endswith(".json") and not n.startswith(".")]
    return sorted(names, key=lambda n: os.path.getmtime(os.path.join(queue, n)))


def claim(spool=None):
    """Take the oldest queued job. -> (job dict, path in running/) or None.

    Losing a race is normal, not an error: another worker got there first, so
    move on to the next candidate.
    """
    spool = ensure(spool)
    p = paths(spool)
    for name in pending(spool):
        src = os.path.join(p["queue"], name)
        dst = os.path.join(p["running"], name)
        try:
            os.rename(src, dst)
        except OSError:
            continue                      # someone else claimed it
        try:
            with open(dst, encoding="utf-8") as f:
                return json.load(f), dst
        except (OSError, ValueError) as exc:
            finish(dst, "failed", {"error": "unreadable job file: %s" % exc},
                   spool=spool)
    return None


def finish(running_path, state, extra=None, spool=None):
    """Move a claimed job to done/ or failed/, recording how it went."""
    assert state in ("done", "failed")
    p = paths(spool)
    try:
        with open(running_path, encoding="utf-8") as f:
            job = json.load(f)
    except (OSError, ValueError):
        job = {"id": os.path.basename(running_path)[:-5]}
    job.update(extra or {})
    job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    job["state"] = state
    dst = os.path.join(p[state], os.path.basename(running_path))
    write_atomic(dst, json.dumps(job, indent=2))
    try:
        os.remove(running_path)
    except OSError:
        pass
    return dst


def read_job(path):
    """One job file -> dict. Unreadable files still get an id, from the name."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"id": os.path.basename(path)[:-5]}


def jobs(state, spool=None):
    """Job files in one state, oldest first. -> [(path, job dict)]."""
    p = paths(spool)
    try:
        names = [n for n in os.listdir(p[state])
                 if n.endswith(".json") and not n.startswith(".")]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(p[state], n)))
    out = []
    for name in names:
        path = os.path.join(p[state], name)
        job = read_job(path)
        job.setdefault("id", name[:-5])
        out.append((path, job))
    return out


def _alive(pid):
    """Is that pid a running process? Anything unknown counts as dead.

    Signal 0 checks existence without delivering anything. EPERM means the pid
    exists but belongs to someone else, which still counts as alive.
    """
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def orphans(spool=None):
    """Claims in running/ with no worker behind them. -> [path], oldest first.

    A machine that reboots mid-run leaves its claim in running/ forever: the
    rename that claimed it survived, the process that was going to finish it did
    not. Nothing else notices, so the job shows as "running" until someone reads
    the directory by hand.

    The one claim that is not an orphan is the one a *live* worker says it is
    working on right now, so a second worker starting up can never fail a run
    that is genuinely in flight.
    """
    p = paths(spool)
    beat = read_heartbeat(spool) or {}
    mine = None
    if beat.get("state") == "running" and _alive(beat.get("pid")):
        mine = str(beat.get("job") or "") + ".json"
    try:
        names = sorted(n for n in os.listdir(p["running"])
                       if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []
    return [os.path.join(p["running"], n) for n in names if n != mine]


def reap(spool=None, reason=None):
    """Fail every orphaned claim. -> [(job id, session_dir or None)].

    Failed rather than requeued: the runner writes into a session directory as
    it goes, so re-running would start a second one from scratch while the half
    finished first is still on disk. What did complete is kept and readable;
    rerunning is the reader's call, not ours.
    """
    reason = reason or ("worker stopped without finishing this job "
                        "(machine rebooted, or the process was killed)")
    reaped = []
    for path in orphans(spool):
        job = read_job(path)
        finish(path, "failed", {"error": reason, "reaped": True}, spool=spool)
        reaped.append((job.get("id", os.path.basename(path)[:-5]),
                       job.get("session_dir")))
    return reaped


def note(running_path, spool=None, **fields):
    """Record something about a claim in the claim file itself.

    Used for the session directory, so a claim that is later found orphaned says
    where its half-finished session is rather than needing a heartbeat that a
    reboot has already overwritten.
    """
    job = read_job(running_path)
    job.update(fields)
    try:
        write_atomic(running_path, json.dumps(job, indent=2))
    except OSError:
        pass
    return job


CANCEL_FILE = "cancel.json"


def request_cancel(job_id, spool=None):
    """Ask the worker to abandon the job it is running. -> the request dict.

    A file, like everything else here: the worker polls it once a second while a
    run is in flight, so cancelling needs no signal, no pid and no socket -- and
    works just as well when the thing asking is an HTTP handler in another
    process.
    """
    spool = ensure(spool)
    req = {"job": job_id, "asked": time.strftime("%Y-%m-%dT%H:%M:%S")}
    write_atomic(os.path.join(spool, CANCEL_FILE), json.dumps(req, indent=2))
    return req


def cancel_requested(job_id, spool=None):
    """Has anyone asked for this job to stop?"""
    path = os.path.join(spool or DEFAULT_SPOOL, CANCEL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("job") == job_id
    except (OSError, ValueError):
        return False


def clear_cancel(spool=None):
    """Drop a cancel request once it has been acted on."""
    try:
        os.remove(os.path.join(spool or DEFAULT_SPOOL, CANCEL_FILE))
    except OSError:
        pass


def requeue(running_path, spool=None):
    """Put an unstarted claim back. Used when a worker shuts down cleanly."""
    p = paths(spool)
    dst = os.path.join(p["queue"], os.path.basename(running_path))
    os.rename(running_path, dst)
    return dst


def heartbeat(spool=None, **fields):
    """Record what the worker is doing, so a stale report is recognisable."""
    spool = ensure(spool)
    state = {"pid": os.getpid(), "seen": time.strftime("%Y-%m-%dT%H:%M:%S")}
    state.update(fields)
    write_atomic(os.path.join(spool, "worker.json"), json.dumps(state, indent=2))
    return state


def read_heartbeat(spool=None):
    path = os.path.join(spool or DEFAULT_SPOOL, "worker.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def counts(spool=None):
    """-> {state: n} for a status line."""
    p = paths(spool)
    out = {}
    for state in STATES:
        try:
            out[state] = len([n for n in os.listdir(p[state])
                              if n.endswith(".json") and not n.startswith(".")])
        except OSError:
            out[state] = 0
    return out
