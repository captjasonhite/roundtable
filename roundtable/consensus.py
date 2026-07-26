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


def _scores_from(entries, key, labels):
    """{label: score} from one set of judge verdicts, self-votes excluded.

    Split out of score() so the bootstrap can recompute the same number from a
    resampled panel without duplicating the rule about self-votes.
    """
    pcts = {label: [] for label in labels}
    for entry in entries:
        judge = norm_model(entry["judge"])
        entry_pcts = _percentiles(entry["ranks"])
        for label, pct in entry_pcts.items():
            if label in pcts and judge != norm_model(key[label]["model"]):
                pcts[label].append(pct)
    return {label: (st.mean(v) if v else None) for label, v in pcts.items()}


def bootstrap(session, rankings, rounds=2000, seed=20260726):
    """How much of the standings order survives resampling the judges?

    The score is a mean over five or six opinions, and it is reported to two
    decimal places -- which invites reading a 0.04 gap as a result. This
    resamples the panel with replacement and reports, per entry, the range its
    score moves over and how often it comes out on top.

    ``p_best`` is the honest headline: four entries at 0.25 each means the run
    ranked them, but the panel could not. Seeded, so a report rebuilt twice
    says the same thing.
    """
    import random
    key = session["key"]
    labels = sorted(key)
    entries = [r for r in rankings if len(r["ranks"]) >= 2]
    if len(entries) < 3:
        # Below three judges a resample is mostly the same judge repeated; the
        # interval it produces would be theatre.
        return None
    rng = random.Random(seed)
    samples = {label: [] for label in labels}
    wins = {label: 0.0 for label in labels}
    for _ in range(rounds):
        panel = [entries[rng.randrange(len(entries))] for _ in entries]
        scores = _scores_from(panel, key, labels)
        best, top = None, []
        for label, value in scores.items():
            if value is None:
                continue
            samples[label].append(value)
            if best is None or value > best:
                best, top = value, [label]
            elif value == best:
                top.append(label)
        for label in top:                       # a tie splits the win
            wins[label] += 1.0 / len(top)
    out = {}
    for label in labels:
        values = sorted(samples[label])
        if not values:
            out[label] = None
            continue
        out[label] = {
            # 90%, not 95%: with six judges the tails are three resampled
            # opinions wide and a 95% bound is mostly noise about noise.
            "low": values[int(0.05 * (len(values) - 1))],
            "high": values[int(0.95 * (len(values) - 1))],
            "p_best": wins[label] / rounds,
        }
    return out


def indistinguishable(result, boot):
    """The entries at the top that the panel cannot actually separate.

    -> list of labels, longest run from the top whose intervals all overlap the
    leader's. One label means the winner really is clear of the field; four
    means the order in the table is a coin toss dressed as a ranking.
    """
    if not boot:
        return []
    ordered = [r for r in result["standings"] if r["score"] is not None]
    if not ordered:
        return []
    lead = boot.get(ordered[0]["label"])
    if not lead:
        return []
    tied = []
    for row in ordered:
        band = boot.get(row["label"])
        if not band or band["high"] < lead["low"]:
            break
        tied.append(row["label"])
    return tied


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
    result = {
        "standings": standings,
        "judges": usable,
        "agreement": kendall_w(complete),
        "agreement_n": len(complete),
        "scored": bool(session["blind"]) and any(
            r["score"] is not None for r in standings),
    }
    boot = bootstrap(session, usable) if result["scored"] else None
    result["bootstrap"] = boot
    result["indistinguishable"] = indistinguishable(result, boot)
    for row in standings:                       # so a caller has it row-wise
        row["band"] = (boot or {}).get(row["label"])
    return result


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
