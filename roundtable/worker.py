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
import json
import os
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
            report_every=REPORT_EVERY, should_stop=None, sessions_root=None):
    """Run one job to completion. -> dict of result fields for the job record.

    ``on_report(session_dir, running)`` is called to regenerate the report; it
    is injected so tests can run the whole loop without importing the renderer.
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
    session_dir = None
    with open(log_path, "w", encoding="utf-8") as log:
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
                time.sleep(1.0)
                if session_dir is None:
                    new = _listing(sessions_root) - before
                    if new:
                        session_dir = os.path.join(sessions_root, sorted(new)[-1])
                        spool.heartbeat(sp, job=job_id, session=session_dir,
                                        state="running")
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
        except Stopping:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=30)
            raise
        code = proc.returncode

    if session_dir is None:                       # nothing was created at all
        new = _listing(sessions_root) - before
        if new:
            session_dir = os.path.join(sessions_root, sorted(new)[-1])

    ran_meta_summary = False
    if code == 0 and session_dir:
        # Round 3 is the worker's step, not the bench script's, so the bench
        # script can't have counted it. Record it here — between the last judge
        # finishing and Round 3 starting — or the report shows every run
        # complete while a model is still loading for the synthesis.
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

    ran = 0
    spool.heartbeat(sp, state="idle")
    while not stopping():
        claimed = spool.claim(sp)
        if not claimed:
            if once or drain:
                break
            spool.heartbeat(sp, state="idle")
            for _ in range(int(max(1, poll))):
                if stopping():
                    break
                time.sleep(1)
            continue

        job, running_path = claimed
        job_id = job.get("id", os.path.basename(running_path)[:-5])
        log("running %s" % job_id)
        spool.heartbeat(sp, job=job_id, state="running")
        try:
            result = run_job(job, running_path, sp=sp, runner=runner,
                             on_report=on_report, report_every=report_every,
                             should_stop=stopping, sessions_root=sessions_root)
        except Stopping:
            # The session directory keeps whatever runs finished; the job is
            # not requeued, because re-running it would start from scratch.
            spool.finish(running_path, "failed",
                         {"error": "worker stopped mid-run"}, spool=sp)
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
