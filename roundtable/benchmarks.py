"""Pull the panel's scores out of past sessions and roll them up by sampler
profile, so a card being edited on the page can show "here's how this profile
has actually scored" instead of just the raw numbers with no track record.

Two files this module owns:

* ``<session_dir>/scores.json`` -- one session's standings (already computed by
  ``consensus.score`` for the report) plus each run's ``sampler_profile``,
  written once a session has a scored panel. Written by ``write_session_scores``,
  called from ``bin/roundtable``'s ``build()`` right alongside ``report.write``,
  so it costs nothing extra to compute.
* ``benchmark-history.json`` (default: ``$ROUNDTABLE_SPOOL/benchmark-history.json``)
  -- every session's scores.json, aggregated by sampler profile. Rebuilt by
  ``aggregate()``, which re-derives it from scratch each time rather than
  incrementally updating, so a hand-edited or deleted session is never stale
  data haunting the rollup.
"""
import json
import os
import statistics as st

from . import consensus, ranks, session as session_mod, spool

HISTORY_FILENAME = "benchmark-history.json"


def history_path(spool_dir=None):
    override = os.environ.get("ROUNDTABLE_BENCHMARK_HISTORY")
    if override:
        return override
    return os.path.join(spool_dir or spool.DEFAULT_SPOOL, HISTORY_FILENAME)


def session_scores(data, result):
    """(session dict, consensus result) -> the scores.json payload, or None.

    None when the panel never produced a scored standing (not blind, or no
    usable judge rankings yet) -- nothing worth recording.
    """
    if not result["scored"]:
        return None
    by_label = {r["label"]: r for r in data["runs"] if r["label"]}
    standings = []
    for row in result["standings"]:
        run = by_label.get(row["label"], {})
        standings.append({
            "label": row["label"],
            "model": row["model"],
            "mode": row["mode"],
            "sampler_profile": run.get("sampler_profile") or "",
            "temperature": run.get("temperature") or "",
            "score": row["score"],
            "mean_rank": row["mean_rank"],
            "votes": row["votes"],
            "self_bias": row["self_bias"],
        })
    return {
        "session": data["name"],
        "agreement": result["agreement"],
        "agreement_n": result["agreement_n"],
        "standings": standings,
    }


def write_session_scores(session_dir, data=None, result=None):
    """Compute (if not given) and write ``<session_dir>/scores.json``.

    -> the payload written, or None if there was nothing scorable.
    """
    data = data or session_mod.load(session_dir)
    if not data:
        return None
    if result is None:
        result = consensus.score(data, ranks.extract_all(data))
    payload = session_scores(data, result)
    if payload is None:
        return None
    spool.write_atomic(os.path.join(session_dir, "scores.json"),
                       json.dumps(payload, indent=2) + "\n")
    return payload


def _read_scores(session_dir):
    try:
        with open(os.path.join(session_dir, "scores.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def aggregate(root, out_path=None, spool_dir=None):
    """Walk every session under ``root``, roll up scores.json by sampler
    profile. -> the history dict, also written to ``out_path``.

    A session missing scores.json (never scored, or written before this
    feature existed) is skipped rather than recomputed here -- run
    ``roundtable report --all`` first if you want everything included, since
    that's the one place a full re-render is already expected to be cheap.
    """
    by_profile = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        sdir = os.path.join(root, name)
        if not os.path.isdir(sdir):
            continue
        payload = _read_scores(sdir)
        if not payload:
            continue
        for row in payload["standings"]:
            profile = row["sampler_profile"]
            if not profile or row["score"] is None:
                continue
            entry = by_profile.setdefault(profile, {"scores": [], "sessions": set(),
                                                      "models": set()})
            entry["scores"].append(row["score"])
            entry["sessions"].add(payload["session"])
            entry["models"].add(row["model"])

    history = {}
    for profile, entry in by_profile.items():
        scores = entry["scores"]
        history[profile] = {
            "runs": len(scores),
            "sessions": len(entry["sessions"]),
            "mean_score": st.mean(scores),
            "models": sorted(entry["models"]),
            "last_session": max(entry["sessions"]),
        }
    spool.write_atomic(out_path or history_path(spool_dir),
                       json.dumps(history, indent=2, sort_keys=True) + "\n")
    return history


def load_history(path=None, spool_dir=None):
    """-> the last-written history dict, or {} if it hasn't been built yet."""
    try:
        with open(path or history_path(spool_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
