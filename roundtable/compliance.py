"""Did the model actually do what the brief said?

An editing brief makes demands a reader can verify without taste: every
sentence of the source has to survive, the section markers have to stay where
they were, the draft has to reach the end, and additions have to be bracketed.
None of that is a judgement call, and none of it should be left to a panel --
in session 20260727-200609 one entry deleted 300 words out of the middle of the
manuscript and six judges ranked it fourth without noticing. A deterministic
gate in front of a probabilistic one.

What this cannot do is tell you whether a *rewrite* was justified: Rule 1 of
the brief permits changing sentences that "actively hinder pacing", so a
sentence that comes back altered is a matter of degree, not a fault. This
measures deletion, truncation and marker damage, which are not.
"""
import difflib
import re

MARKER = "***CUT***"

# A source sentence is "still there" if some sentence in the output is at least
# this similar to it. Deliberately loose: the brief allows edits, and bracketed
# insertions land mid-paragraph and split sentences apart. Validated against
# 20260727-200609, where it separates the entry that dropped half a section
# from the five that did not.
MATCH = 0.60

# Sentences shorter than this are skipped -- "She nodded." matches half the
# manuscript at any threshold and tells you nothing.
MIN_WORDS = 6

# Below this share of source sentences surviving, a section counts as deleted
# rather than edited. Tuned on 20260727-200609: the five sound drafts sit at
# 93-98%, the one that lost a section at 81%.
COVERAGE_FLOOR = 0.85

# How much of the manuscript's tail has to be reachable before the draft counts
# as finished. More than one, because the closing line is the likeliest in the
# book to be rewritten on purpose.
TAIL_SENTENCES = 3

_PART2 = re.compile(r"\*{0,2}PART\s*2\b[^\n]*", re.I)
_PART3 = re.compile(r"\*{0,2}PART\s*3\b[^\n]*", re.I)


def draft_of(output):
    """The integrated draft alone, without the critiques around it.

    Coverage has to be measured against PART 2 and nothing else. PART 3 is a
    review of the draft and quotes the source while discussing it -- counting
    those quotes would let a model delete a passage from the draft and win the
    coverage back by mentioning it afterwards.
    """
    text = output or ""
    m2 = _PART2.search(text)
    if not m2:
        return text
    rest = text[m2.end():]
    m3 = _PART3.search(rest)
    return rest[:m3.start()] if m3 else rest


_WS = re.compile(r"\s+")
_MARKUP = re.compile(r"[*_`#>]+")
_SENT = re.compile(r"(?<=[.!?])[\s\n]+")
_BRACKET = re.compile(r"\[([^\[\]]{10,})\]")


def _flatten(text):
    """Lowercased, de-markuped, quote-normalised text: compare words, not typography."""
    text = (text or "")
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'),
                 ("”", '"'), ("—", "-"), ("–", "-"),
                 ("…", "...")):
        text = text.replace(a, b)
    text = _MARKUP.sub(" ", text)
    return _WS.sub(" ", text).strip().lower()


def sentences(text):
    """-> [sentence], long enough to be worth matching."""
    out = []
    for part in _SENT.split(_flatten(text)):
        part = part.strip()
        if len(part.split()) >= MIN_WORDS:
            out.append(part)
    return out


def _best_ratio(needle, haystack):
    """Highest similarity of `needle` against any sentence in `haystack`.

    quick_ratio() first: it is an upper bound and cheap, so a candidate that
    cannot beat the best score so far never costs a real comparison. That keeps
    a 120-sentence manuscript against a 200-sentence draft well under a second.
    """
    best = 0.0
    m = difflib.SequenceMatcher()
    m.set_seq2(needle)
    for cand in haystack:
        m.set_seq1(cand)
        if m.real_quick_ratio() <= best or m.quick_ratio() <= best:
            continue
        best = max(best, m.ratio())
        if best >= 0.99:
            break
    return best


def coverage(source, output):
    """-> (share of source sentences that survive, [sentences that did not]).

    A sentence that comes back rewritten past recognition counts as missing,
    which is the intended reading: the brief says preserve the prose, and a
    reader who cannot find their sentence has lost it either way.
    """
    src = sentences(source)
    if not src:
        return 1.0, []
    out = sentences(output)
    missing = [s for s in src if _best_ratio(s, out) < MATCH]
    return (len(src) - len(missing)) / float(len(src)), missing


def _segments(text):
    return [p for p in re.split(re.escape(MARKER), text or "")]


def check(source, output):
    """-> findings dict for one run's output against the source it was given.

    ``ok`` is False only for the hard faults -- deletion, truncation, marker
    damage. Bracketing problems are reported but do not fail the run: they are
    a defect in the annotation, not in the deliverable.
    """
    findings = {"faults": [], "warnings": [], "segments": [],
                "coverage": 1.0, "markers": {"source": 0, "output": 0}}
    source = source or ""
    # Everything below judges the draft, not the critiques wrapped around it.
    output = draft_of(output or "")

    n_src = source.count(MARKER)
    n_out = output.count(MARKER)
    findings["markers"] = {"source": n_src, "output": n_out}
    if n_src and n_out != n_src:
        findings["faults"].append({
            "code": "markers",
            "detail": "source has %d %s marker(s), the draft has %d"
                      % (n_src, MARKER, n_out)})

    share, missing = coverage(source, output)
    findings["coverage"] = round(share, 3)
    findings["missing_sentences"] = missing[:20]
    if share < COVERAGE_FLOOR:
        findings["faults"].append({
            "code": "coverage",
            "detail": "%d%% of the source survives; %d sentence(s) are missing"
                      % (round(share * 100), len(missing))})

    # Per section, but only when both sides agree on how many there are --
    # otherwise the sections do not line up and the numbers would be fiction.
    src_segs, out_segs = _segments(source), _segments(output)
    if n_src and len(src_segs) == len(out_segs):
        for i, (a, b) in enumerate(zip(src_segs, out_segs), start=1):
            seg_share, seg_missing = coverage(a, b)
            findings["segments"].append({
                "n": i, "coverage": round(seg_share, 3),
                "source_words": len(a.split()), "output_words": len(b.split()),
                "missing": len(seg_missing)})
            if seg_share < COVERAGE_FLOOR:
                findings["faults"].append({
                    "code": "section",
                    "detail": "section %d: %d%% of its source survives"
                              % (i, round(seg_share * 100))})

    # Reaching the end matters on its own: a draft can carry 90% of the source
    # and still stop mid-manuscript, and that is a different failure from
    # trimming. Tested against the last few sentences rather than the last one,
    # because the closing line is the single most likely sentence in the
    # manuscript to be deliberately rewritten -- it is where a tell-heavy
    # ending sits, and Rule 1 permits replacing it. Requiring it verbatim
    # failed two drafts that had finished the story properly.
    src_sents = sentences(source)
    tail = src_sents[-TAIL_SENTENCES:]
    if tail and not any(_best_ratio(s, sentences(output)) >= MATCH for s in tail):
        findings["faults"].append({
            "code": "unfinished",
            "detail": "none of the last %d sentences of the source appear -- "
                      "the draft stops early" % len(tail)})

    opens, closes = output.count("["), output.count("]")
    if opens != closes:
        findings["warnings"].append({
            "code": "brackets",
            "detail": "%d '[' against %d ']'" % (opens, closes)})
    # Bracketing the client's own prose is the inverse of failing to bracket an
    # addition: both leave the reader unable to see what changed.
    src_sents_set = sentences(source)
    quoted = [m.group(1) for m in _BRACKET.finditer(output)]
    lifted = [q for q in quoted
              if _best_ratio(_flatten(q), src_sents_set) >= 0.85]
    if lifted:
        findings["warnings"].append({
            "code": "bracketed_source",
            "detail": "%d bracketed passage(s) are the client's own text"
                      % len(lifted)})
    findings["additions"] = len(quoted)
    findings["ok"] = not findings["faults"]
    return findings


def retry_message(findings):
    """-> what to tell the model it got wrong, or None if nothing did.

    Specific, because the checker knows exactly what is missing, and a vague
    complaint gets a vague correction. Never asks whether it would like to try
    again: that costs a turn and the answer is always yes.
    """
    if not findings.get("faults"):
        return None
    lines = ["Your draft did not meet the brief:"]
    for f in findings["faults"]:
        lines.append("- %s" % f["detail"])
    lines.append("")
    lines.append("Every sentence of the source must appear in PART 2, and every "
                 "%s marker must stay exactly where it was. Reproduce the "
                 "complete draft with the missing text restored, keeping your "
                 "existing additions and their [brackets] unchanged." % MARKER)
    return "\n".join(lines)


def summary(findings):
    """One line for a table or a log."""
    if findings.get("ok"):
        return "ok (%d%% source kept, %d additions)" % (
            round(findings.get("coverage", 1) * 100), findings.get("additions", 0))
    return "; ".join(f["detail"] for f in findings["faults"])
