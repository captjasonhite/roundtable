"""The file queue.

    $ROUNDTABLE_SPOOL/
        queue/     jobs waiting, oldest first
        running/   the one job a worker has claimed
        done/      finished, with the session directory recorded
        failed/    what went wrong, kept for reading
        logs/      runner output, one file per job
        worker.json  heartbeat: which worker, doing what, since when

There is no lock, no database and no daemon protocol. A worker claims a job by
*renaming* it out of ``queue/`` -- rename is atomic within a filesystem, so if
two workers ever race, exactly one wins and the loser sees FileNotFoundError.
Everything is inspectable with ``ls``, and nothing is lost if the machine dies
mid-run: the job file is still sitting in ``running/``.
"""
import json
import os
import time

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
    job_id = job.setdefault(
        "id", "%s-%04d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid() % 10000))
    queue = paths(spool)["queue"]
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
