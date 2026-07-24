"""Round 3: one model synthesises the panel's computed consensus.

Round 1 generates, Round 2 judges blind. Round 3 is not another AI opinion
piled on top — it is one model (the one the panel itself rated highest)
writing prose about a table Roundtable already computed. That ordering
matters: the model is handed the *arithmetic* (mean percentile, agreement,
where judges split, self-preference), not the judges' raw text, so it cannot
re-derive a ranking it has no way to verify. See consensus.py.
"""
from . import consensus as C
from . import naming


def pick_top(result):
    """-> the standings row Round 3 should be assigned to, or None.

    The single external-vote score is the criterion — the same number the
    report's Panel standings table is sorted by. A session with no scored
    standings (not blind, or no judges) has no eligible model.
    """
    for row in result["standings"]:
        if row["score"] is not None:
            return row
    return None


def build(session, result):
    """-> (system_prompt, user_prompt) for the Round 3 run, or (None, None).

    The model is the panel's pick, but Round 3 is not that model
    freelancing — it is told plainly to report the numbers, not re-litigate
    the judging.
    """
    top = pick_top(result)
    if top is None:
        return None, None

    names = naming.short_names([r["model"] for r in result["standings"]])
    rows = sorted((r for r in result["standings"] if r["score"] is not None),
                  key=lambda r: -r["score"])

    lines = [
        "You wrote one of the entries judged in this session, and the panel "
        "rated your entry highest. Your job now is not to defend it or judge "
        "again — it is to report, in plain prose, what the panel as a whole "
        "concluded.",
        "",
        "Below is the COMPUTED consensus: mean percentile across every judge "
        "(your own vote on your own entry excluded from every number), head-to-"
        "head win rate, and where judges disagreed. Do not re-rank the entries "
        "yourself or introduce a verdict the numbers don't support — summarise "
        "what is already decided.",
        "",
        "Panel agreement (Kendall's W, 0=no agreement, 1=perfect): %s (%s)."
        % (C.fmt(result["agreement"]), C.agreement_label(result["agreement"])),
        "",
        "| Rank | Model | Mode | Score | Mean rank | Head-to-head |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append("| %d | %s | %s | %s | %s | %s |" % (
            i, names[r["model"]], r["mode"], C.fmt(r["score"]),
            C.fmt(r["mean_rank"], 1),
            (C.fmt(100 * r["h2h"], 0) + "%") if r["h2h"] is not None else "–"))

    contested = C.disagreements(result, 3)
    if contested and contested[0]["spread"]:
        lines += ["", "Most contested (widest spread between judges):"]
        for r in contested:
            if not r["spread"]:
                continue
            lines.append("- %s (%s): placed anywhere from #%s to #%s"
                         % (names[r["model"]], r["mode"],
                            C.fmt(r["best_rank"], 0), C.fmt(r["worst_rank"], 0)))

    lines += [
        "", "---", "",
        "Write a short synthesis (150-300 words): what the panel agreed on, "
        "what it split on and why that might be, and a one-line final "
        "recommendation. Refer to entries by model name, not by letter.",
    ]

    system_prompt = (
        "You are the moderator summarising a blind panel review, not one of "
        "its participants. Report the computed consensus faithfully; do not "
        "substitute your own literary judgement for the panel's numbers."
    )
    return system_prompt, "\n".join(lines)
