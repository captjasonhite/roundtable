"""Turn per-judge rankings into a panel verdict.

Two rules shape everything here:

* **Self-votes are excluded from scoring.** A model judging its own output is
  not an independent opinion. They are kept and reported separately, because
  the size of the self-preference is itself interesting.
* **Only blind sessions are scored.** With model names visible, judges grade
  the label: in the 2026-07-23 sessions, byte-identical text was ranked eight
  places apart depending on which label it carried. A non-blind session is
  still rendered, just without standings.
"""
import math
import re
import statistics as st


def norm_model(name):
    """Loose model identity, for matching a judge against its own outputs.

    Judges are recorded by the filename they were written under and outputs by
    their frontmatter, which can differ in case and punctuation.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _percentiles(ranks):
    """One judge's {label: rank} -> {label: percentile}, 1.0 = best of field."""
    n = len(ranks)
    if n < 2:
        return {}
    ordered = sorted(ranks.values())
    out = {}
    for label, rank in ranks.items():
        # Position among distinct rank values, so declared ties score equally.
        position = ordered.index(rank)
        out[label] = 1.0 - position / (n - 1)
    return out


def kendall_w(rankings):
    """Kendall's W (coefficient of concordance) over complete rankings.

    0 = judges are unrelated, 1 = perfect agreement. Includes the standard tie
    correction; returns None when there is nothing to measure (fewer than two
    judges or fewer than two items).
    """
    rankings = [r for r in rankings if len(r) >= 2]
    if len(rankings) < 2:
        return None
    labels = sorted(set(rankings[0]))
    if any(sorted(set(r)) != labels for r in rankings):
        return None
    m, n = len(rankings), len(labels)
    if n < 2:
        return None
    sums = [sum(r[label] for r in rankings) for label in labels]
    mean = sum(sums) / n
    s = sum((x - mean) ** 2 for x in sums)
    # Tie correction: T = sum(t^3 - t) over each judge's tied groups.
    correction = 0
    for r in rankings:
        counts = {}
        for value in r.values():
            counts[value] = counts.get(value, 0) + 1
        correction += sum(t ** 3 - t for t in counts.values() if t > 1)
    denominator = m ** 2 * (n ** 3 - n) - m * correction
    if denominator <= 0:
        return None
    return 12 * s / denominator


def score(session, rankings):
    """-> dict of standings, agreement and self-preference for one session.

    ``rankings`` is the output of ranks.extract_all().
    """
    key = session["key"]
    labels = sorted(key)
    usable = [r for r in rankings if len(r["ranks"]) >= 2]

    # votes[label] = [(judge, rank, is_self), ...]
    votes = {label: [] for label in labels}
    for entry in usable:
        judge = norm_model(entry["judge"])
        pcts = _percentiles(entry["ranks"])
        for label, rank in entry["ranks"].items():
            if label not in votes:
                continue
            is_self = judge == norm_model(key[label]["model"])
            votes[label].append({
                "judge": entry["judge"], "rank": rank,
                "pct": pcts.get(label), "self": is_self,
            })

    # Head-to-head: for every judge, every pair, who was placed higher.
    wins = {label: 0 for label in labels}
    duels = {label: 0 for label in labels}
    for entry in usable:
        judge = norm_model(entry["judge"])
        for a in entry["ranks"]:
            for b in entry["ranks"]:
                if a >= b or a not in votes or b not in votes:
                    continue
                # Skip any duel the judge has a stake in.
                if judge in (norm_model(key[a]["model"]), norm_model(key[b]["model"])):
                    continue
                ra, rb = entry["ranks"][a], entry["ranks"][b]
                duels[a] += 1
                duels[b] += 1
                if ra < rb:
                    wins[a] += 1
                elif rb < ra:
                    wins[b] += 1
                else:
                    wins[a] += 0.5
                    wins[b] += 0.5

    standings = []
    for label in labels:
        external = [v for v in votes[label] if not v["self"]]
        pcts = [v["pct"] for v in external if v["pct"] is not None]
        ext_ranks = [v["rank"] for v in external]
        self_vote = next((v["rank"] for v in votes[label] if v["self"]), None)
        standings.append({
            "label": label,
            "model": key[label]["model"],
            "mode": key[label]["mode"],
            "score": st.mean(pcts) if pcts else None,
            "mean_rank": st.mean(ext_ranks) if ext_ranks else None,
            "best_rank": min(ext_ranks) if ext_ranks else None,
            "worst_rank": max(ext_ranks) if ext_ranks else None,
            "spread": (max(ext_ranks) - min(ext_ranks)) if ext_ranks else None,
            "votes": len(ext_ranks),
            "h2h": (wins[label] / duels[label]) if duels[label] else None,
            "self_rank": self_vote,
            # Positive = the model placed itself above where the panel placed it.
            "self_bias": (st.mean(ext_ranks) - self_vote)
                         if (self_vote is not None and ext_ranks) else None,
        })
    standings.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))

    complete = [r["ranks"] for r in usable if r["complete"]]
    return {
        "standings": standings,
        "judges": usable,
        "agreement": kendall_w(complete),
        "agreement_n": len(complete),
        "scored": bool(session["blind"]) and any(
            r["score"] is not None for r in standings),
    }


def disagreements(result, limit=3):
    """Outputs the panel split hardest on -- widest rank spread first."""
    rows = [r for r in result["standings"] if r["spread"] is not None]
    rows.sort(key=lambda r: -r["spread"])
    return rows[:limit]


def consensus_order(result):
    """-> {label: 1-indexed position} from the panel's own standings.

    This is "what the group picked" -- the same order the standings table and
    Judged-outputs section are sorted by. Only scored labels get a position;
    a session with no scored panel yields an empty order.
    """
    return {r["label"]: i + 1 for i, r in enumerate(result["standings"])
            if r["score"] is not None}


def judge_distance(result, judge_ranks):
    """How far one judge's ranking sits from the panel's consensus order.

    Mean absolute difference in rank position, lower = closer to the group.
    This is a different question from self-preference: a judge can rank its
    own entry fairly (no self-bias) while still disagreeing with everyone else
    about the other entries, and vice versa. None if there's nothing to
    compare (no consensus order, or the judge ranked nothing in it).
    """
    order = consensus_order(result)
    if not order or not judge_ranks:
        return None
    diffs = [abs(rank - order[label]) for label, rank in judge_ranks.items()
             if label in order]
    return sum(diffs) / len(diffs) if diffs else None


def agreement_label(w):
    """Plain-language reading of Kendall's W, so the number isn't bare."""
    if w is None:
        return "not measurable"
    if w >= 0.8:
        return "very strong agreement"
    if w >= 0.6:
        return "strong agreement"
    if w >= 0.4:
        return "moderate agreement"
    if w >= 0.2:
        return "weak agreement"
    return "little better than chance"


def fmt(value, digits=2):
    if value is None:
        return "–"
    if isinstance(value, float) and math.isnan(value):
        return "–"
    return ("%%.%df" % digits) % value
