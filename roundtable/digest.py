"""Round 3: one model explains the panel's verdict in plain prose.

Round 1 generates, Round 2 judges blind. Round 3 is not another AI opinion
piled on top — it is one model (the one the panel itself rated highest)
turning a ranking Roundtable already computed into readable prose.

The ranking stays locked to the arithmetic: the model is handed the standing
order and told plainly not to re-order it. What it *is* given, so it can say
something about the *writing* instead of the statistics, is the panel's own
notes on the two entries that matter most — the one rated best and the one
rated worst — with the blind letters swapped back to model names. It explains
why those two landed where they did, in the judges' terms, and characterises
how the vote looked (a runaway winner, a near-tie, an entry they split on) in
plain words, without quoting any score, mean rank, or agreement coefficient.
"""
import re

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


# Judges refer to the shuffled entries by blind letter, written "{{A}}",
# "{{B}}", … in their verdicts. Match one to pull the prose about an entry,
# and swap the letters back to model names so Round 3 can quote it directly.
_LETTER_RE = re.compile(r"\{\{\s*([A-Z])\s*\}\}")
_THINKING_RE = re.compile(r"(?ims)^\s*#{1,4}\s+thinking\b.*?(?=^\s*#{1,4}\s+\S)")


def _verdict_text(body):
    """Drop a leading reasoning section if one survived the file split."""
    return _THINKING_RE.sub("", body or "").strip()


def _deletter(text, by_label):
    """Replace every '{{X}}' with entry X's model name."""
    return _LETTER_RE.sub(
        lambda m: by_label.get(m.group(1), m.group(0)), text)


def _notes_on(judges, label, by_label, max_chars=1200):
    """The paragraphs judges wrote about one entry, letters resolved to names.

    One representative paragraph per judge, bounded so the prompt stays a
    briefing and not the whole transcript. Table rows and stray one-liners are
    skipped in favour of prose that actually says something.
    """
    token = "{{%s}}" % label
    notes, total = [], 0
    for j in judges:
        for para in re.split(r"\n\s*\n", _verdict_text(j.get("body", ""))):
            p = " ".join(para.split())
            if token not in p or p.startswith("|") or len(p) < 40:
                continue
            p = _deletter(p, by_label)
            if total + len(p) > max_chars and notes:
                return notes
            notes.append(p)
            total += len(p)
            break
    return notes


def build(session, result):
    """-> (system_prompt, user_prompt) for the Round 3 run, or (None, None).

    The model is the panel's pick, but Round 3 is not that model freelancing:
    the order is fixed and it is told to explain, not re-litigate.
    """
    top = pick_top(result)
    if top is None:
        return None, None

    names = naming.short_names([r["model"] for r in result["standings"]])
    rows = sorted((r for r in result["standings"] if r["score"] is not None),
                  key=lambda r: -r["score"])
    by_label = {r["label"]: names[r["model"]] for r in rows if r.get("label")}

    lines = [
        "You wrote one of the entries in this blind panel review, and the "
        "panel rated your entry highest. Your job now is not to defend it or "
        "judge again — it is to explain, in plain prose, how the entries "
        "rated and why.",
        "",
        "The order below is already decided by the panel's votes (your own "
        "vote on your own entry is excluded from every placement). Do not "
        "re-rank the entries or contradict this order — explain it.",
        "",
        "Standing, best first:",
    ]
    lines += ["%d. %s (%s)" % (i, names[r["model"]], r["mode"])
              for i, r in enumerate(rows, 1)]

    judges = session.get("judges") or []
    winner = rows[0]
    loser = rows[-1] if len(rows) > 1 else None

    win_notes = _notes_on(judges, winner["label"], by_label)
    if win_notes:
        lines += ["", "What the judges said about the top entry (%s):"
                  % names[winner["model"]]]
        lines += ["- " + n for n in win_notes]

    if loser is not None:
        lose_notes = _notes_on(judges, loser["label"], by_label)
        if lose_notes:
            lines += ["", "What the judges said about the lowest-rated entry "
                      "(%s):" % names[loser["model"]]]
            lines += ["- " + n for n in lose_notes]

    # How the vote looked, in words the synthesis can lift directly — no
    # coefficient, no raw score, just the shape of the result.
    facts = ["The judges showed %s overall."
             % C.agreement_label(result["agreement"])]
    tied = set()   # entries already explained by a near-tie at the top
    if len(rows) >= 2 and rows[1]["score"] is not None:
        gap = rows[0]["score"] - rows[1]["score"]
        if gap >= 0.30:
            facts.append("%s won by a clear margin." % names[rows[0]["model"]])
        elif gap <= 0.12:
            facts.append("%s and %s finished all but tied at the top."
                         % (names[rows[0]["model"]], names[rows[1]["model"]]))
            tied = {rows[0]["model"], rows[1]["model"]}
    for r in C.disagreements(result, 3):
        # A split between the top two is already told as the near-tie; only
        # flag a contested entry the reader hasn't heard about yet.
        if r.get("spread") and r["model"] not in tied:
            facts.append("The panel split most on %s — placed anywhere from "
                         "#%s to #%s." % (names[r["model"]],
                         C.fmt(r["best_rank"], 0), C.fmt(r["worst_rank"], 0)))
    lines += ["", "How the vote looked:"] + ["- " + f for f in facts]

    lines += [
        "", "---", "",
        "Write a short synthesis (150-300 words) about the quality of these "
        "responses, not the arithmetic:",
        "- Lead with the winning entry: from the judges' notes above, say "
        "concretely what it did well that the others did not.",
        "- Then explain why the lowest-rated entry fell short.",
        "- Work in how the vote looked — a runaway winner, a near-tie at the "
        "top, an entry the judges split on — but in plain words. Do NOT cite "
        "any statistic, coefficient, score, mean rank, or win rate.",
        "- End with a one-line recommendation.",
        "Refer to entries by model name, never by letter.",
    ]

    system_prompt = (
        "You are the moderator summarising a blind panel review. The order is "
        "already settled by the panel's votes — report it faithfully and do "
        "not re-rank. Explain, in the panel's own terms, how the entries "
        "rated and why, in plain prose without statistics."
    )
    return system_prompt, "\n".join(lines)
