"""Read a creative-bench session directory into plain data.

A session directory is produced by creative-bench.sh and looks like this::

    20260723-211143/
        system-prompt.txt
        user-prompt.txt
        01_<stamp>_<model>_thinking.md      one per benchmark run
        ...
        SUMMARIZE.md                        what the judges were shown
        SUMMARIZE-KEY.md                    label -> model, absent if not blind
        summary_<stamp>_<model>.md          one per judge

Nothing here knows about HTML or scoring -- it just turns files into dicts.
"""
import os
import re
import time


def parse_front(text):
    """Split '---\\nkey: value\\n---\\nbody' into (dict, body).

    Returns ({}, text) when there is no frontmatter, so callers never have to
    special-case a malformed file.
    """
    meta = {}
    if not text.startswith("---\n"):
        return meta, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return meta, text
    for line in text[4:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[end + 5:].lstrip("\n")


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_key(sdir):
    """SUMMARIZE-KEY.md -> {label: {'model', 'mode'}}.

    Empty dict means the session was judged with model names visible, which
    makes its rankings unusable -- see consensus.py for why.
    """
    text = _read(os.path.join(sdir, "SUMMARIZE-KEY.md"))
    key = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        # "{{A}}" since 2026-07-24; a bare "A" in older sessions.
        label = cells[0].strip("{}").strip()
        if not re.fullmatch(r"[A-Z]", label):
            continue
        mode = cells[2] if len(cells) > 2 and "think" in cells[2].lower() else ""
        key[label] = {"model": cells[1], "mode": mode}
    return key


def read_judge_key(sdir, judge):
    """SUMMARIZE-KEY-<judge>.md -> {this judge's letter: canonical label}.

    Each judge reads the same outputs in its own order, with its own entries
    last, so its "{{A}}" is whatever it happened to read first -- not the
    canonical {{A}} the report talks about. This is the translation, and its
    absence is not an error: sessions judged before per-judge documents existed
    have one shared order, where the two labellings are the same thing.
    """
    text = _read(os.path.join(sdir, "SUMMARIZE-KEY-%s.md" % judge))
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        mine, canonical = cells[0].strip("{}").strip(), cells[1].strip("{}").strip()
        if re.fullmatch(r"[A-Z]", mine) and re.fullmatch(r"[A-Z]", canonical):
            out[mine] = canonical
    return out


def _is_result(name):
    return (name.endswith(".md")
            and not name.startswith(("SUMMARIZE", "summary_", "round3_", "README")))


def read_runs(sdir, key):
    """Benchmark results -> list of dicts, one per run, in file order."""
    label_of = {}
    for label, info in key.items():
        label_of.setdefault((info["model"], info["mode"]), label)

    runs = []
    for name in sorted(os.listdir(sdir)):
        if not _is_result(name):
            continue
        meta, body = parse_front(_read(os.path.join(sdir, name)))
        if not meta.get("model"):
            continue
        mode = "thinking" if meta.get("thinking") == "true" else "no thinking"
        err = meta.get("error")
        runs.append({
            "file": name,
            "label": label_of.get((meta["model"], mode), ""),
            "model": meta["model"],
            "mode": mode,
            "image": meta.get("image", ""),
            "tokens": _num(meta.get("tokens")),
            "tps": _num(meta.get("tokens_per_sec")),
            "elapsed": _num(meta.get("elapsed_sec")),
            "context": meta.get("context", ""),
            "temperature": meta.get("temperature", ""),
            "seed": meta.get("seed", ""),
            "samplers": meta.get("samplers", ""),
            "sampler_profile": meta.get("sampler_profile", ""),
            "error": None if err in (None, "null", "") else err,
            "body": body,
        })
    return runs


def read_judges(sdir):
    """summary_*.md -> list of dicts, one per judge verdict."""
    judges = []
    for name in sorted(os.listdir(sdir)):
        if not (name.startswith("summary_") and name.endswith(".md")):
            continue
        meta, body = parse_front(_read(os.path.join(sdir, name)))
        # summary_<stamp>_<model>.md -- the model is the judge
        judge = re.sub(r"^summary_[0-9-]*_", "", name)[:-3]
        judges.append({
            "file": name,
            "judge": meta.get("model", judge),
            # {} when this session had one shared order for everyone.
            "label_map": read_judge_key(sdir, judge),
            "ranking_retry": meta.get("ranking_retry") == "true",
            "tokens": _num(meta.get("tokens")),
            "tps": _num(meta.get("tokens_per_sec")),
            "elapsed": _num(meta.get("elapsed_sec")),
            "error": None if meta.get("error") in (None, "null", "") else meta.get("error"),
            # Judges reason before answering; the ranking lives after "## Output".
            "body": body.split("## Output", 1)[-1],
        })
    return judges


def read_meta_summary(sdir):
    """round3_*.md -> the Round 3 synthesis, or None if it hasn't run.

    Written by ``creative-bench.sh --meta-summary`` — the single top-ranked
    model reading the computed Round 2 consensus and writing a final verdict.
    Named for the file it reads, not the round number, so a rename of "Round 3"
    doesn't ripple into every file on disk.
    """
    names = sorted(n for n in os.listdir(sdir)
                   if n.startswith("round3_") and n.endswith(".md"))
    if not names:
        return None
    # Only one is ever produced per session; a rerun's file sorts last by stamp.
    name = names[-1]
    meta, body = parse_front(_read(os.path.join(sdir, name)))
    return {
        "file": name,
        "model": meta.get("model", "?"),
        "tokens": _num(meta.get("tokens")),
        "tps": _num(meta.get("tokens_per_sec")),
        "elapsed": _num(meta.get("elapsed_sec")),
        "error": None if meta.get("error") in (None, "null", "") else meta.get("error"),
        "body": body.split("## Output", 1)[-1],
    }


def load(sdir):
    """Read one session directory. -> dict, or None if it holds no runs."""
    sdir = os.path.abspath(sdir)
    key = read_key(sdir)
    runs = read_runs(sdir, key)
    if not runs:
        return None
    judges = read_judges(sdir)
    meta_summary = read_meta_summary(sdir)
    first = runs[0]
    return {
        "dir": sdir,
        "name": os.path.basename(sdir),
        "blind": bool(key),
        "key": key,
        "runs": runs,
        "judges": judges,
        "meta_summary": meta_summary,
        "images": sorted({r["image"] for r in runs if r["image"]}),
        "temperature": first["temperature"],
        "seed": first["seed"],
        "samplers": first["samplers"],
        "system_prompt": _read(os.path.join(sdir, "system-prompt.txt")),
        "user_prompt": _read(os.path.join(sdir, "user-prompt.txt")),
        # A run still in flight has no result file yet, so "how many are coming"
        # is unknowable from disk alone; creative-bench.sh writes both counts
        # when it knows them (judges only once --summarize is settled, which on
        # an interactive run happens after the last output run).
        "expected_runs": _num(_read(os.path.join(sdir, ".expected-runs")).strip()),
        "expected_judges": _num(_read(os.path.join(sdir, ".expected-judges")).strip()),
        # Round 3 is the worker's step, so the worker writes this one.
        "expected_meta": _num(_read(os.path.join(sdir, ".expected-meta")).strip()),
        "started": started(sdir),
        "touched": touched(sdir),
    }


def started(sdir):
    """-> epoch seconds the session began, or None.

    The directory name is a local-time stamp (creative-bench.sh: date
    +%Y%m%d-%H%M%S); its mtime is not usable, since every finished run writes
    into the directory and pushes it forward.
    """
    name = os.path.basename(os.path.abspath(sdir))
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        try:
            return os.path.getctime(sdir)
        except OSError:
            return None


def touched(sdir):
    """-> epoch seconds the runner last wrote something, or None.

    How a reader tells "still running" from "stopped half way": a queue that
    died leaves its counts short forever, and an ETA computed from it would be
    fiction.

    Deliberately NOT the newest file in the directory. report.html and
    scores.json are rewritten into the session by every rebuild, and the
    de-anonymiser rewrites deanon/ -- reading those would make a session that
    died last week look like it was alive a second ago, and would count a much
    later rebuild as part of how long the session took to run. Only files a run
    itself produces count: the result markdown, and the per-run logs, which tick
    while a run is still in flight and has no result file yet.
    """
    candidates = []
    for name in os.listdir(sdir):
        if _is_result(name) or name.startswith(("summary_", "round3_")):
            candidates.append(os.path.join(sdir, name))
    logs = os.path.join(sdir, "logs")
    try:
        candidates += [os.path.join(logs, n) for n in os.listdir(logs)]
    except OSError:
        pass
    newest = None
    for path in candidates:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest
