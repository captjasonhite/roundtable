"""The worker: one process, one GPU, one job at a time.

The worker is the lock. Because a single worker consumes the queue serially,
"only one model in VRAM at a time" is expressed by the architecture rather than
by a mutex someone has to remember to take.

It shells out to the bench runner (``creative-bench.sh`` by default) using that
script's non-interactive flags -- no benchmarking logic is reimplemented here.
While a run is in flight the worker rewrites the session's report every few
seconds, which is the entire live-progress mechanism: the browser is just
reloading a file.
"""
import glob
import json
import os
import shutil
import signal
import subprocess
import time

from . import consensus, digest, ranks, session as session_mod, spool

_BUNDLED_RUNNER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin",
    "creative-bench.sh")


def _default_runner():
    """Which creative-bench.sh to shell out to, absent an explicit override.

    ``ROUNDTABLE_RUNNER`` always wins. Otherwise: a separately-maintained
    ``~/Apps/bin/creative-bench.sh`` wins over the repo's own bundled copy --
    if you keep editing your own script there, the worker should pick up
    those edits rather than silently running a frozen snapshot. Only a fresh
    clone with no such personal copy falls back to the bundled one, which is
    what makes the repo work for someone who just cloned it.
    """
    override = os.environ.get("ROUNDTABLE_RUNNER")
    if override:
        return override
    personal = os.path.expanduser("~/Apps/bin/creative-bench.sh")
    return personal if os.path.exists(personal) else _BUNDLED_RUNNER


DEFAULT_RUNNER = _default_runner()
DEFAULT_SESSIONS = os.environ.get(
    "ROUNDTABLE_SESSIONS", os.path.expanduser("~/Apps/creative-bench"))

REPORT_EVERY = 10.0      # first fallback interval, before any run has finished
REPORT_MIN = 5.0         # never rebuild more often than this
REPORT_MAX = 60.0        # ...nor less often, so a stalled run still looks alive
REPORT_PER_RUN = 3.0     # aim for ~3 fallback rebuilds per run
POLL_EVERY = 3.0         # seconds between queue scans when idle


def _artifacts(session_dir):
    """How many result/verdict files exist. Each new one is a progress event."""
    try:
        return len([n for n in os.listdir(session_dir)
                    if n.endswith(".md") and not n.startswith("SUMMARIZE")])
    except OSError:
        return 0


class Stopping(Exception):
    """Raised in the main loop when a shutdown signal arrives."""


class Cancelled(Exception):
    """Raised when someone asked for *this job* to stop.

    Distinct from Stopping: the worker keeps going and takes the next job.
    """


def _listing(root):
    try:
        return {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))}
    except OSError:
        return set()


def build_command(job, runner=None, prompt_dir=None):
    """Turn a job dict into (argv, env) for the bench runner.

    Prompts arrive as text in the job and have to become files, because that is
    what the runner's non-interactive interface takes.
    """
    runner = runner or job.get("runner") or DEFAULT_RUNNER
    if job.get("judge_only"):
        return _build_judge_command(job, runner)
    prompt_dir = prompt_dir or "."
    sys_path = os.path.join(prompt_dir, "system-prompt.txt")
    usr_path = os.path.join(prompt_dir, "user-prompt.txt")
    spool.write_atomic(sys_path, job.get("system_prompt", ""))
    spool.write_atomic(usr_path, job.get("user_prompt", ""))

    argv = [runner,
            "--system", sys_path,
            "--user", usr_path,
            "--mode", job.get("mode", "thinking"),
            "--yes"]
    # Omitted rather than defaulted to 1.0: passing --temp at all sets
    # creative-bench.sh's TEMP_USER_SET, which makes every model ignore its
    # sampler card's temperature. Leaving it out when the job didn't ask for
    # a specific temperature is what lets each run's card supply its own.
    if job.get("temperature") is not None:
        argv += ["--temp", str(job["temperature"])]
    models = job.get("models") or []
    if models:
        argv += ["--models", ",".join(models)]
    argv.append("--summarize" if job.get("summarize", True) else "--no-summarize")
    if not job.get("blind", True):
        argv.append("--no-blind")

    env = dict(os.environ)
    env["OUTDIR"] = job.get("sessions_root") or DEFAULT_SESSIONS
    if job.get("seed"):
        env["SEED"] = str(job["seed"])
    if job.get("summary_temperature"):
        env["SUMMARY_TEMP"] = str(job["summary_temperature"])
    # Anything else the runner understands (MAX_TOKENS, SUMMARY_MODE, ...).
    for key, value in (job.get("env") or {}).items():
        env[str(key)] = str(value)
    return argv, env


def _build_judge_command(job, runner):
    """Round 2 again over a finished session, with a panel of your choosing.

    No prompts and no benchmark: the outputs are already on disk, and
    ``--summarize-only`` rebuilds the judging documents from them. ``--judges``
    is what makes this worth doing -- the panel does not have to be the field,
    so a run of near-relatives can be re-scored by outsiders.
    """
    argv = [runner, "--summarize-only", job["judge_only"], "--summarize", "--yes"]
    judges = job.get("judges") or []
    if judges:
        argv += ["--judges", ",".join(judges)]
    if not job.get("blind", True):
        argv.append("--no-blind")

    env = dict(os.environ)
    env["OUTDIR"] = job.get("sessions_root") or DEFAULT_SESSIONS
    # The judging documents are shuffled per judge from this seed. A re-judge
    # keeps the session's own seed if the job carries one, so the same panel
    # re-run twice reads the outputs in the same order both times.
    if job.get("seed"):
        env["SEED"] = str(job["seed"])
    if job.get("summary_temperature"):
        env["SUMMARY_TEMP"] = str(job["summary_temperature"])
    for key, value in (job.get("env") or {}).items():
        env[str(key)] = str(value)
    return argv, env


# What a re-judge supersedes. The output runs, the prompts and the logs stay:
# only the verdicts about them, and everything computed from those verdicts,
# belong to the old panel.
_VERDICT_GLOBS = ("summary_*.md", "round3_*.md", "SUMMARIZE-*.md")
_VERDICT_KEEP = ("SUMMARIZE-KEY.md",)      # rebuilt in place, not a verdict
_VERDICT_EXTRAS = ("scores.json", "deanon")


def archive_verdicts(session_dir):
    """Move an existing panel's verdicts aside. -> the archive dir, or None.

    A re-judge writes new ``summary_*.md`` files next to the old ones, and the
    scorer reads every one it finds -- two panels would silently be pooled into
    a single set of standings. So the old panel is moved into ``.judges-N/``
    before the new one runs: out of the scorer's way (it lists the session
    directory, never its subdirectories) but still on disk, which is the whole
    reason this is a move and not a delete.
    """
    victims = []
    for pattern in _VERDICT_GLOBS:
        victims += [p for p in glob.glob(os.path.join(session_dir, pattern))
                    if os.path.basename(p) not in _VERDICT_KEEP]
    victims += [p for p in (os.path.join(session_dir, n) for n in _VERDICT_EXTRAS)
                if os.path.exists(p)]
    if not victims:
        return None
    for n in range(1, 1000):
        archive = os.path.join(session_dir, ".judges-%d" % n)
        if not os.path.exists(archive):
            break
    os.makedirs(archive, exist_ok=True)
    for path in victims:
        try:
            shutil.move(path, os.path.join(archive, os.path.basename(path)))
        except OSError:
            pass                      # a verdict we cannot move is not fatal
    return archive


def build_meta_command(model_slug, system_prompt, user_prompt, session_dir,
                       runner=None, prompt_dir=None, temperature=1.0,
                       max_tokens=None):
    """Round 3's (argv, env): one model, appended into an existing session dir.

    ``model_slug`` only needs to be a substring unique to that one model's
    GGUF path — the frontmatter ``model`` field the rest of Roundtable already
    keys on satisfies that by construction (see naming.py).
    """
    runner = runner or DEFAULT_RUNNER
    sys_path = os.path.join(prompt_dir, "round3-system.txt")
    usr_path = os.path.join(prompt_dir, "round3-user.txt")
    spool.write_atomic(sys_path, system_prompt or "")
    spool.write_atomic(usr_path, user_prompt or "")

    argv = [runner, "--meta-summary", session_dir, "--meta-model", model_slug,
            "--system", sys_path, "--user", usr_path,
            "--temp", str(temperature), "--yes"]
    env = dict(os.environ)
    if max_tokens:
        env["MAX_TOKENS"] = str(max_tokens)
    return argv, env


def _expect_meta(session_dir, wanted):
    """Note in the session whether a Round 3 run is still to come.

    One number in ``.expected-meta``, matching the ``.expected-*`` counts the
    bench script writes, so the report can count the synthesis as the judge run
    it is. Best-effort: a session that can't be written to is not a reason to
    fail a job that has already produced its results.
    """
    try:
        spool.write_atomic(os.path.join(session_dir, ".expected-meta"),
                           "1\n" if wanted else "0\n")
    except OSError:
        pass


def _expect_judges(session_dir, count):
    """How many judges this pass will produce, before any of them has run.

    The bench script writes this itself once it starts, but a re-judge has
    just archived the old panel: without this the report would sit at "0 of
    <the last panel's size>" until the runner got far enough to correct it.
    """
    if not count:
        return
    try:
        spool.write_atomic(os.path.join(session_dir, ".expected-judges"),
                           "%d\n" % count)
    except OSError:
        pass


def run_meta_summary(job, session_dir, sp=None, runner=None, log=None):
    """Round 3, after Round 1+2 have finished. -> True if it ran, False if skipped.

    Skipped (not failed) when the session isn't blind-scored — there is no
    panel consensus to summarise — or when the job opted out. A Round 3
    failure is logged and swallowed: Round 1 and 2 already succeeded and their
    results must not be thrown away over an optional third step.
    """
    if not job.get("meta_summary", True) or not session_dir:
        return False
    data = session_mod.load(session_dir)
    if not data or not data["blind"]:
        return False
    result = consensus.score(data, ranks.extract_all(data))
    if not result["scored"]:
        return False
    top = digest.pick_top(result)
    if top is None:
        return False
    system_prompt, user_prompt = digest.build(data, result)
    if not user_prompt:
        return False

    p = spool.paths(sp)
    work_dir = os.path.join(p["logs"], (job.get("id", "job")) + ".round3")
    os.makedirs(work_dir, exist_ok=True)
    argv, env = build_meta_command(
        top["model"], system_prompt, user_prompt, session_dir, runner=runner,
        prompt_dir=work_dir, temperature=job.get("temperature", 1.0),
        max_tokens=(job.get("env") or {}).get("SUMMARY_MAX_TOKENS")
                   or (job.get("env") or {}).get("MAX_TOKENS"))
    log_path = os.path.join(p["logs"], (job.get("id", "job")) + ".round3.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("$ %s\n\n" % " ".join(argv))
        f.flush()
        code = subprocess.call(argv, stdout=f, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, env=env)
    if code != 0 and log:
        log("round 3 failed for %s (exit %d) — see %s"
            % (top["model"], code, log_path))
    return code == 0


def run_job(job, running_path, sp=None, runner=None, on_report=None,
            report_every=REPORT_EVERY, should_stop=None, sessions_root=None,
            is_cancelled=None):
    """Run one job to completion. -> dict of result fields for the job record.

    ``on_report(session_dir, running)`` is called to regenerate the report; it
    is injected so tests can run the whole loop without importing the renderer.
    ``is_cancelled()`` is polled the same way as ``should_stop()``; both kill the
    runner's whole process group, they differ only in what the caller does next.
    """
    if sessions_root and not job.get("sessions_root"):
        # A job submitted from the form names no sessions root of its own; the
        # worker's must match what the HTTP server is browsing, or the report
        # lands somewhere the job page never finds it.
        job = dict(job, sessions_root=sessions_root)
    p = spool.paths(sp)
    job_id = job.get("id", "job")
    log_path = os.path.join(p["logs"], job_id + ".log")
    work_dir = os.path.join(p["logs"], job_id + ".prompts")
    os.makedirs(work_dir, exist_ok=True)

    argv, env = build_command(job, runner=runner, prompt_dir=work_dir)
    sessions_root = env["OUTDIR"]
    os.makedirs(sessions_root, exist_ok=True)
    before = _listing(sessions_root)

    started = time.time()
    # A re-judge writes into a session that already exists, so there is no new
    # directory to watch for: it is known before the runner starts, and the old
    # panel's verdicts have to be out of the way before the new ones land.
    session_dir = job.get("judge_only") or None
    with open(log_path, "w", encoding="utf-8") as log:
        if session_dir:
            archive = archive_verdicts(session_dir)
            if archive:
                log.write("[roundtable] previous verdicts moved to %s\n"
                          % os.path.basename(archive))
            _expect_judges(session_dir, len(job.get("judges") or []))
            _expect_meta(session_dir, job.get("meta_summary", True))
            # The poll loop below announces the session when it first appears;
            # this one was never going to appear, so announce it here instead.
            spool.heartbeat(sp, job=job_id, session=session_dir, state="running")
            spool.note(running_path, session_dir=session_dir)
        log.write("$ %s\n\n" % " ".join(argv))
        log.flush()
        # Own process group: a stop signal reaches llama-server too, not just
        # the shell script that launched it.
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
        last_report = 0.0
        interval = report_every       # adapts to how long this session's runs take
        seen_artifacts = 0
        last_artifact_at = None
        try:
            while proc.poll() is None:
                if should_stop and should_stop():
                    raise Stopping()
                if is_cancelled and is_cancelled():
                    raise Cancelled()
                time.sleep(1.0)
                if session_dir is None:
                    new = _listing(sessions_root) - before
                    if new:
                        session_dir = os.path.join(sessions_root, sorted(new)[-1])
                        spool.heartbeat(sp, job=job_id, session=session_dir,
                                        state="running")
                        # Into the claim file too: a heartbeat does not survive
                        # a reboot, and whoever reaps this claim afterwards needs
                        # to know which session was left half-written.
                        spool.note(running_path, session_dir=session_dir)
                        # The moment the session exists, say whether a Round 3
                        # is coming. Written here rather than after the bench
                        # script exits so the progress bar and the Judge runs
                        # tile count the synthesis from the first tick instead
                        # of growing a seventh unit two rounds in.
                        _expect_meta(session_dir, job.get("meta_summary", True))
                if not (session_dir and on_report):
                    continue

                now = time.time()
                # A new result file is the only real progress event there is, so
                # rebuild on it immediately rather than waiting for a tick.
                count = _artifacts(session_dir)
                fresh = count > seen_artifacts
                if fresh:
                    if last_artifact_at is not None:
                        # Retune: roughly REPORT_PER_RUN rebuilds per run, so a
                        # 20-second run polls briskly and a 5-minute one doesn't
                        # rewrite the page 30 times for nothing.
                        gap = now - last_artifact_at
                        interval = min(REPORT_MAX,
                                       max(REPORT_MIN, gap / REPORT_PER_RUN))
                    seen_artifacts = count
                    last_artifact_at = now
                    spool.heartbeat(sp, job=job_id, session=session_dir,
                                    state="running", done=count,
                                    report_interval=round(interval, 1))

                if fresh or now - last_report >= interval:
                    last_report = now
                    try:
                        on_report(session_dir, True)
                    except Exception as exc:                  # never kill the run
                        log.write("\n[roundtable] report failed: %s\n" % exc)
                        log.flush()
        except (Stopping, Cancelled):
            # However the kill goes, the original Stopping/Cancelled has to be
            # what propagates -- a proc.wait() timeout here is a subprocess.
            # TimeoutExpired, not one of those two, and the caller's generic
            # except Exception would otherwise mistake a real shutdown for an
            # ordinary job failure and keep the loop running past it.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass          # a process this stuck is the OS's problem now
            raise
        code = proc.returncode

    if session_dir is None:                       # nothing was created at all
        new = _listing(sessions_root) - before
        if new:
            session_dir = os.path.join(sessions_root, sorted(new)[-1])

    ran_meta_summary = False
    if code == 0 and session_dir:
        # Normally already written when the session dir first appeared; repeated
        # here for the path where the run was so short the poll loop never saw
        # it, and because a rerun of an existing session dir starts from
        # whatever the previous run left behind.
        _expect_meta(session_dir, job.get("meta_summary", True))
        if on_report:
            on_report(session_dir, True)          # Round 1+2 visible while Round 3 loads
        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\n[roundtable] starting round 3 (meta-summary)\n")
        def _log_to_main(msg):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

        ran_meta_summary = run_meta_summary(job, session_dir, sp=sp, runner=runner,
                                            log=_log_to_main)
        if not ran_meta_summary:
            # Skipped after all (not blind, nothing scored, no top model): take
            # the pending synthesis back off the count rather than leaving the
            # report one run short of complete forever.
            _expect_meta(session_dir, False)
    if session_dir and on_report:
        on_report(session_dir, False)             # final write, no refresh tag

    return {"exit_code": code,
            "meta_summary": ran_meta_summary,
            # What was asked for, as opposed to what happened: finish() merges
            # this record over the job, so without it a Round 3 that was wanted
            # but skipped would look like a job that never wanted one -- and
            # rerunning from the record would quietly drop it.
            "meta_summary_requested": bool(job.get("meta_summary", True)),
            "session_dir": session_dir,
            "log": log_path,
            "elapsed_sec": round(time.time() - started, 1)}


def loop(sp=None, runner=None, on_report=None, once=False, drain=False,
         poll=POLL_EVERY, report_every=REPORT_EVERY, log=print, should_stop=None,
         sessions_root=None):
    """Claim and run jobs. -> number of jobs run.

    Runs forever by default, which is what a service wants. ``once`` stops after
    a single job; ``drain`` stops when the queue empties, which is what a
    one-shot batch (or a test) wants. ``should_stop`` lets a caller that owns the
    main thread -- ``roundtable up``, which runs the worker in a thread -- ask for
    a clean shutdown.
    """
    sp = spool.ensure(sp)
    stop = {"now": False}

    def _stop(signum, frame):
        stop["now"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except ValueError:
            pass                                  # not the main thread

    external_stop = should_stop

    def stopping():
        return stop["now"] or bool(external_stop and external_stop())

    def _settle(session_dir):
        """Stop a killed job's session from advertising itself as live.

        The last report written during a run carries the refresh tag; without
        rewriting it once, a cancelled or reaped session reloads itself forever
        and reads as still running to anyone who opens it.
        """
        if not (session_dir and on_report):
            return
        try:
            on_report(session_dir, False)
        except Exception:                     # a dead job is not worth failing over
            pass

    def _reap():
        # Anything still claimed with no worker behind it belongs to a worker
        # that is gone -- most often this machine rebooted mid-run.
        for job_id, session_dir in spool.reap(sp):
            _settle(session_dir)
            log("reaped  %s (claimed by a worker that is no longer running)"
                % job_id)

    ran = 0
    spool.heartbeat(sp, state="idle")
    _reap()

    while not stopping():
        claimed = spool.claim(sp)
        if not claimed:
            if once or drain:
                break
            spool.heartbeat(sp, state="idle")
            # An idle worker owns nothing, so a claim that appears while it is
            # idle -- a run killed by the machine going down under a *previous*
            # worker, say -- can be cleared without waiting for a restart.
            _reap()
            for _ in range(int(max(1, poll))):
                if stopping():
                    break
                time.sleep(1)
            continue

        job, running_path = claimed
        job_id = job.get("id", os.path.basename(running_path)[:-5])
        log("running %s" % job_id)
        spool.heartbeat(sp, job=job_id, state="running")

        def cancelled(job_id=job_id):
            return spool.cancel_requested(job_id, sp)

        if cancelled():                       # asked for while it was still queued
            spool.clear_cancel(sp)
            spool.finish(running_path, "failed",
                         {"error": "cancelled", "cancelled": True}, spool=sp)
            log("cancelled %s before it started" % job_id)
            continue
        try:
            result = run_job(job, running_path, sp=sp, runner=runner,
                             on_report=on_report, report_every=report_every,
                             should_stop=stopping, sessions_root=sessions_root,
                             is_cancelled=cancelled)
        except Cancelled:
            # Whatever finished is kept and the session stops calling itself
            # live; the worker takes the next job rather than shutting down.
            session_dir = spool.read_job(running_path).get("session_dir")
            spool.clear_cancel(sp)
            spool.finish(running_path, "failed",
                         {"error": "cancelled", "cancelled": True,
                          "session_dir": session_dir}, spool=sp)
            _settle(session_dir)
            log("cancelled %s" % job_id)
            continue
        except Stopping:
            # The session directory keeps whatever runs finished; the job is
            # not requeued, because re-running it would start from scratch.
            session_dir = spool.read_job(running_path).get("session_dir")
            spool.finish(running_path, "failed",
                         {"error": "worker stopped mid-run",
                          "session_dir": session_dir}, spool=sp)
            _settle(session_dir)
            log("stopped %s mid-run" % job_id)
            break
        except Exception as exc:
            spool.finish(running_path, "failed", {"error": repr(exc)}, spool=sp)
            log("failed  %s: %s" % (job_id, exc))
            continue

        state = "done" if result["exit_code"] == 0 else "failed"
        if state == "failed":
            result["error"] = "runner exited %s" % result["exit_code"]
        spool.finish(running_path, state, result, spool=sp)
        ran += 1
        log("%-7s %s (%ss) -> %s"
            % (state, job_id, result["elapsed_sec"], result["session_dir"]))
        if once:
            break

    spool.heartbeat(sp, state="stopped")
    return ran
