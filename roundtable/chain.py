"""A chain: several roundtable stages run in sequence, each one seeing a slice
of the previous stage's outputs as its ``{{PREVIOUS}}`` input.

Two knobs are deliberately kept separate at every handoff (see the spec
format below):

* **Roster** -- ``models`` on a stage: who generates *that* stage's answers.
* **Selection** -- ``use_previous`` on a stage: how much of the *prior*
  stage's field gets shown to this stage's roster as context: ``all``,
  ``top3``, ``top1`` (ranked by that stage's own blind judging, shared by
  every roster model), or ``own`` (each surviving model from the prior stage
  continues with only *its own* prior output -- see below).

``all``/``top3``/``top1`` compose as one shared block, not a cartesian
product: every model on a stage's roster sees the *same* merged
``{{PREVIOUS}}`` text. Ten models with ``use_previous: top3`` is ten
generations conditioned on the same three survivors, not thirty.

``own`` is the one place a chain runs more than one prompt per stage: model
A's own prior output becomes model A's ``{{PREVIOUS}}``, model B's own prior
output becomes model B's, and so on -- one thread per prior participant, each
still a single independent generation, all merged back into one judged field
at the end of the stage. This is still not branching in the sense the rest of
the chain avoids: nothing fans out into more threads than the roster already
has, and everything reconverges into one ranking every stage.

A chain spec is plain JSON::

    {
      "manuscript": "path/to/manuscript.txt",   // optional, fills {{MANUSCRIPT}}
      // or "manuscript_text": "...", inline text (what the GUI sends; wins
      // over "manuscript" if both are present)
      "stages": [
        {
          "name": "analyze",
          "models": ["Qwen3.6-27B:thinking", "Gemma4-26B:thinking"],
          "system_prompt": "...",
          "user_prompt": "... {{MANUSCRIPT}} ...",
          "use_previous": "all"        // ignored on the first stage
        },
        {
          "name": "plan",
          "models": ["..."],
          "use_previous": "top1",
          "system_prompt": "...",
          "user_prompt": "... {{PREVIOUS}} ..."
        },
        {
          "name": "rewrite",
          "use_previous": "own",       // each model continues its own plan
          "system_prompt": "...",
          "user_prompt": "... {{PREVIOUS}} ...",
          "meta_summary": true         // last stage: panel's top pick explains the verdict
        }
      ]
    }

Each stage is an ordinary roundtable generate+judge round (Round 1 + Round 2)
-- reusing ``worker.build_command`` so no bench-runner flag logic is
duplicated here. A stage can also opt into Round 3 (``"meta_summary": true``):
the same single-model synthesis ``digest.py`` already computes for a plain
session, run automatically once that stage's panel has a scored verdict.
"""
import json
import os
import re
import shutil
import subprocess
import time

from . import consensus, digest as digest_mod, ranks, report, session as session_mod, worker

SELECT_ALL = "all"
SELECT_TOP3 = "top3"
SELECT_TOP1 = "top1"
SELECT_OWN = "own"
_SELECT_N = {SELECT_ALL: None, SELECT_TOP3: 3, SELECT_TOP1: 1}
_USE_PREVIOUS_VALUES = set(_SELECT_N) | {SELECT_OWN}


def validate_spec(spec):
    """Raise ValueError with a plain-language reason if the spec is unusable."""
    if not isinstance(spec, dict) or not spec.get("stages"):
        raise ValueError("chain spec needs a non-empty 'stages' list")
    for stage in spec["stages"]:
        use_previous = stage.get("use_previous", SELECT_ALL)
        if use_previous not in _USE_PREVIOUS_VALUES:
            raise ValueError("stage %r: use_previous must be one of %s, got %r"
                             % (stage.get("name"), sorted(_USE_PREVIOUS_VALUES),
                                use_previous))


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    validate_spec(spec)
    return spec


def _select_runs(data, result, use_previous):
    """The previous stage's runs to carry forward, best first.

    Ranked by that stage's own blind judging when one exists. Falls back to
    every successful run when there is nothing to rank by -- a single-model
    roster, or a non-blind stage -- rather than pretending a ranking exists.
    Used for the shared all/top3/top1 selections; ``own`` (below) reads the
    unranked full field instead, since every survivor continues regardless
    of where it placed.
    """
    n = _SELECT_N.get(use_previous)
    scored = [r for r in result["standings"] if r["score"] is not None] if result else []
    if not scored:
        return [run for run in data["runs"] if not run.get("error")]
    order = scored if n is None else scored[:n]
    rank_of = {r["label"]: i for i, r in enumerate(order)}
    chosen = [run for run in data["runs"]
             if run.get("label") in rank_of and not run.get("error")]
    chosen.sort(key=lambda run: rank_of[run["label"]])
    return chosen


def _previous_block(chosen):
    """Selected runs -> the text that fills {{PREVIOUS}} in the next stage.

    Anonymised the same way blind judging is: candidates are lettered, not
    named, so the next stage's roster works from the writing rather than
    recognising (and deferring to) a particular model's style.
    """
    if not chosen:
        return "(no usable output from the previous stage)"
    parts = []
    for i, run in enumerate(chosen):
        label = chr(ord("A") + i)
        body = session_mod.output_of(run.get("body", "")).strip()
        parts.append("**Candidate %s:**\n\n%s" % (label, body))
    return "\n\n---\n\n".join(parts)


def _fill(template, previous_text, manuscript_text):
    text = (template or "").replace("{{PREVIOUS}}", previous_text or "")
    if manuscript_text is not None:
        text = text.replace("{{MANUSCRIPT}}", manuscript_text)
    return text


def _stage_dirname(i, stage):
    slug = re.sub(r"[^a-z0-9]+", "-", stage.get("name", "stage-%d" % i).lower()).strip("-")
    return "%02d-%s" % (i, slug or "stage")


def _live_report(session_dir, running):
    """Rebuild one session's report.html, live -- the same rebuild-on-every-
    new-result-file behaviour a plain job gets, just reached from a chain
    stage's subprocess instead of the worker's. Never raises: a rendering
    hiccup must not take down a run that is still producing real output.
    """
    try:
        data = session_mod.load(session_dir)
        if not data:
            return
        rankings = ranks.extract_all(data)
        result = consensus.score(data, rankings)
        html = report.render(data, result, rankings, running=running)
        report.write(os.path.join(session_dir, "report.html"), html)
    except Exception:
        pass


def _run_stage(job, stage_root, runner=None, log=print, should_stop=None):
    """One stage's subprocess, with the same live-progress rebuild a plain
    job gets (see ``worker.run_and_poll``). -> the session directory it
    produced, or None.

    Raises ``worker.Stopping``/``worker.Cancelled`` if ``should_stop()``
    turns true mid-run -- unlike the top-of-stage check in ``run_chain``,
    this one can interrupt a stage that is already in flight, the same as
    cancelling a plain job does.
    """
    prompt_dir = os.path.join(stage_root, ".prompts")
    os.makedirs(prompt_dir, exist_ok=True)
    argv, env = worker.build_command(job, runner=runner, prompt_dir=prompt_dir)
    sessions_root = env["OUTDIR"]
    os.makedirs(sessions_root, exist_ok=True)
    before = {n for n in os.listdir(sessions_root)
             if os.path.isdir(os.path.join(sessions_root, n))}

    log_path = os.path.join(stage_root, "run.log")
    session_dir, code = worker.run_and_poll(
        argv, env, log_path, sessions_root=sessions_root, before=before,
        on_report=_live_report, should_stop=should_stop)
    if code != 0:
        log("  stage failed (exit %d) -- see %s" % (code, log_path))
    return session_dir


def _pattern_matches(pattern, model):
    return pattern.split(":", 1)[0].lower() in model.lower()


def _run_own_stage(stage, stage_root, previous, manuscript_text, runner=None, log=print,
                   should_stop=None):
    """Each surviving model from the prior stage runs this stage's prompt
    filled with *its own* prior output, as a separate single-model job; the
    results are merged into one session directory and judged together.

    -> the merged session directory.
    """
    if previous is None:
        raise ValueError("stage %r: use_previous 'own' needs a prior stage"
                         % stage.get("name"))
    data, result = previous
    threads = _select_runs(data, result, SELECT_ALL)
    if stage.get("models"):
        threads = [r for r in threads
                  if any(_pattern_matches(p, r["model"]) for p in stage["models"])]
    if not threads:
        raise RuntimeError("stage %r: no prior outputs to continue from"
                           % stage.get("name"))

    merged_dir = os.path.join(stage_root, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.join(merged_dir, "logs"), exist_ok=True)
    with open(os.path.join(merged_dir, "system-prompt.txt"), "w", encoding="utf-8") as f:
        f.write(stage.get("system_prompt", ""))
    with open(os.path.join(merged_dir, "user-prompt.txt"), "w", encoding="utf-8") as f:
        f.write("(each model in this stage worked from its own previous-stage "
                "output -- see ../../chain.json for what each one carried forward)")

    for idx, run in enumerate(threads, 1):
        own_text = session_mod.output_of(run.get("body", "")).strip()
        job = {
            "system_prompt": _fill(stage.get("system_prompt", ""), own_text,
                                   manuscript_text),
            "user_prompt": _fill(stage.get("user_prompt", ""), own_text,
                                 manuscript_text),
            "mode": "thinking" if run["mode"] == "thinking" else "nothinking",
            "models": [run["model"]],
            "summarize": False,
            "sessions_root": os.path.join(stage_root, ".own-%02d" % idx),
        }
        if stage.get("temperature") is not None:
            job["temperature"] = stage["temperature"]
        if stage.get("env"):
            job["env"] = stage["env"]

        scratch_dir = _run_stage(job, job["sessions_root"], runner=runner, log=log,
                                 should_stop=should_stop)
        if scratch_dir is None:
            log("  %s produced no output for its own thread -- skipped" % run["model"])
            continue
        for fname in sorted(os.listdir(scratch_dir)):
            if fname.endswith(".md") and not fname.startswith(
                    ("SUMMARIZE", "summary_", "round3_")):
                shutil.copy(os.path.join(scratch_dir, fname),
                           os.path.join(merged_dir, "%02d_%s" % (idx, fname)))

    if stage.get("summarize", True):
        judge_job = {"judge_only": merged_dir, "blind": stage.get("blind", True)}
        argv, env = worker.build_command(judge_job, runner=runner)
        log_path = os.path.join(merged_dir, "logs", "judge.log")
        worker.run_and_poll(argv, env, log_path, session_dir=merged_dir,
                            on_report=_live_report, should_stop=should_stop)
    return merged_dir


def _run_meta_summary(stage, session_dir, data, result, runner=None, log=print):
    """Round 3 for one stage: the panel's own top pick explains the verdict.

    Reuses ``digest.py`` exactly as a plain session's Round 3 does -- the
    order is locked to the panel's own arithmetic, the model just narrates
    it. Skipped, not faked, when the stage has no scored panel to explain.
    """
    if not (result and result["scored"]):
        log("  meta-summary skipped -- stage has no scored panel")
        return False
    top = digest_mod.pick_top(result)
    if top is None:
        return False
    system_prompt, user_prompt = digest_mod.build(data, result)
    if not user_prompt:
        return False
    prompt_dir = os.path.join(session_dir, ".round3-prompts")
    os.makedirs(prompt_dir, exist_ok=True)
    argv, env = worker.build_meta_command(
        top["model"], system_prompt, user_prompt, session_dir, runner=runner,
        prompt_dir=prompt_dir, temperature=stage.get("temperature", 1.0))
    log_path = os.path.join(session_dir, "round3.log")
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write("$ %s\n\n" % " ".join(argv))
        logf.flush()
        code = subprocess.call(argv, stdout=logf, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, env=env)
    if code != 0:
        log("  meta-summary failed (exit %d) -- see %s" % (code, log_path))
    return code == 0


def _load_running_stage(stage_root):
    """Best-effort session data for a stage that hasn't finished yet -- the
    newest non-hidden subdirectory under it that has produced any result
    file. -> a ``session.load()`` dict, or None if nothing has landed yet.

    Covers both shapes a stage can take: a plain stage's single timestamped
    session dir, and an "own" stage's merged dir (also timestamped, also
    directly under the stage root -- the per-model scratch work happens
    under dot-prefixed ``.own-NN`` dirs, which this skips).
    """
    if not os.path.isdir(stage_root):
        return None
    candidates = [os.path.join(stage_root, name) for name in os.listdir(stage_root)
                 if not name.startswith(".")
                 and os.path.isdir(os.path.join(stage_root, name))]
    if not candidates:
        return None
    return session_mod.load(max(candidates, key=os.path.getmtime))


def _write_report(root, manifest):
    """The chain's report.html -- built from the exact same section
    functions a plain session's report uses, one stack per stage, in run
    order. A chain is more rounds of the same report, not a different kind
    of page, so it reuses ``report.render_chain`` rather than inventing its
    own layout; written to ``report.html`` (not a separate index page) so a
    chain is, on disk, structurally the same shape as a plain session --
    which is what lets it show up in the ordinary Results list instead of a
    walled-off "chains" list of its own.
    """
    finished = manifest["stages"]
    planned = manifest.get("planned") or [s["name"] for s in finished]
    n_done = len(finished)
    still_running = "finished" not in manifest

    entries = []
    for i, stage_name in enumerate(planned):
        if i < n_done:
            s = finished[i]
            data = session_mod.load(s["session_dir"]) if s.get("session_dir") else None
            state = "done"
        elif i == n_done and still_running:
            stage_root = os.path.join(root, _stage_dirname(i + 1, {"name": stage_name}))
            data = _load_running_stage(stage_root)
            state = "running"
        else:
            data = None
            state = "queued"
        if data:
            rankings = ranks.extract_all(data)
            result = consensus.score(data, rankings)
            entries.append((stage_name, data, result, rankings, state))
        else:
            entries.append((stage_name, None, None, None, state))

    status_note = "stopped early" if manifest.get("stopped") else ""
    html = report.render_chain(os.path.basename(root), entries, running=still_running,
                               status_note=status_note)
    report.write(os.path.join(root, "report.html"), html)


def _save(root, manifest):
    """Write chain.json and report.html as they stand -- called after every
    stage, not just at the end, so a chain in flight has live progress the
    same way a plain session's report does.
    """
    with open(os.path.join(root, "chain.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    _write_report(root, manifest)


def run_chain(spec, root, runner=None, log=print, should_stop=None):
    """Run every stage in order. -> the manifest dict (also written to chain.json).

    ``should_stop()`` is checked both between stages and, via
    ``worker.run_and_poll``, while a stage's subprocess is running -- the
    same kill-the-process-group behaviour cancelling a plain job gets.
    Either way, whatever already finished is kept and the manifest is marked
    ``"stopped": true`` rather than raising -- the "keep what ran" rule the
    plain worker follows.
    """
    root = os.path.abspath(os.path.expanduser(root))
    os.makedirs(root, exist_ok=True)

    # Inline text wins over a file path when a spec somehow carries both --
    # the GUI always sends text (pasted straight into the form), the CLI/JSON
    # form typically points at a file. Either way it fills {{MANUSCRIPT}}.
    manuscript_text = spec.get("manuscript_text")
    if manuscript_text is None and spec.get("manuscript"):
        with open(os.path.expanduser(spec["manuscript"]), encoding="utf-8") as f:
            manuscript_text = f.read()

    stages = spec["stages"]
    planned = [stage.get("name", "stage-%d" % i) for i, stage in enumerate(stages, 1)]
    manifest = {"root": root, "started": time.time(), "stages": [], "planned": planned}
    _save(root, manifest)      # index.html exists (queued, 0/N) before any stage runs

    previous = None            # (data, result) of the prior stage
    previous_text = ""

    for i, stage in enumerate(stages, 1):
        if should_stop and should_stop():
            manifest["stopped"] = True
            log("stopped before stage %d/%d" % (i, len(stages)))
            break

        name = stage.get("name", "stage-%d" % i)
        use_previous = stage.get("use_previous", SELECT_ALL)
        log("[%d/%d] %s (roster %d, carrying forward: %s)"
            % (i, len(stages), name, len(stage.get("models") or []), use_previous))
        _save(root, manifest)   # promote this stage from "queued" to "running" now

        if use_previous == SELECT_OWN:
            if previous is not None:
                data0, result0 = previous
                manifest["stages"][-1]["passed_forward"] = [
                    {"model": r["model"], "mode": r["mode"]}
                    for r in _select_runs(data0, result0, SELECT_ALL)]
            stage_root = os.path.join(root, _stage_dirname(i, stage))
        else:
            if previous is not None:
                data, result = previous
                chosen = _select_runs(data, result, use_previous)
                previous_text = _previous_block(chosen)
                manifest["stages"][-1]["passed_forward"] = [
                    {"model": r["model"], "mode": r["mode"]} for r in chosen]

            stage_root = os.path.join(root, _stage_dirname(i, stage))
            os.makedirs(stage_root, exist_ok=True)

            job = {
                "system_prompt": _fill(stage.get("system_prompt", ""), previous_text,
                                       manuscript_text),
                "user_prompt": _fill(stage.get("user_prompt", ""), previous_text,
                                     manuscript_text),
                "mode": stage.get("mode", "thinking"),
                "models": stage.get("models") or [],
                "blind": stage.get("blind", True),
                "summarize": stage.get("summarize", True),
                "sessions_root": stage_root,
            }
            if stage.get("temperature") is not None:
                job["temperature"] = stage["temperature"]
            if stage.get("seed") is not None:
                job["seed"] = stage["seed"]
            if stage.get("env"):
                job["env"] = stage["env"]

        # should_stop() can interrupt a stage already in flight here -- unlike
        # the top-of-loop check above, which only catches the gap between
        # stages -- the same as cancelling a plain job kills its subprocess.
        try:
            if use_previous == SELECT_OWN:
                session_dir = _run_own_stage(stage, stage_root, previous, manuscript_text,
                                             runner=runner, log=log, should_stop=should_stop)
            else:
                session_dir = _run_stage(job, stage_root, runner=runner, log=log,
                                         should_stop=should_stop)
        except (worker.Stopping, worker.Cancelled):
            manifest["stopped"] = True
            log("stopped mid-stage %d/%d (%s)" % (i, len(stages), name))
            break

        if session_dir is None:
            raise RuntimeError("stage %r produced no session" % name)

        data = session_mod.load(session_dir)
        result = consensus.score(data, ranks.extract_all(data)) if data else None

        if stage.get("meta_summary") and data:
            if _run_meta_summary(stage, session_dir, data, result, runner=runner, log=log):
                data = session_mod.load(session_dir)   # pick up round3_*.md

        if data:
            html = report.render(data, result, ranks.extract_all(data))
            report.write(os.path.join(session_dir, "report.html"), html)

        manifest["stages"].append({
            "name": name, "session_dir": session_dir, "use_previous": use_previous,
            "roster": stage.get("models") or [],
            "scored": bool(result and result["scored"]),
        })
        previous = (data, result) if data else None
        _save(root, manifest)

    manifest["finished"] = time.time()
    _save(root, manifest)
    log(("chain stopped -> %s" if manifest.get("stopped") else "chain complete -> %s")
       % os.path.join(root, "index.html"))
    return manifest
