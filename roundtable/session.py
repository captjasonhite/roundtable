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
        # is unknowable from disk alone; the worker writes it when it knows.
        "expected_runs": _num(_read(os.path.join(sdir, ".expected-runs")).strip()),
    }
