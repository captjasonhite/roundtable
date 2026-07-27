"""Pull a ranking out of a judge's verdict.

Judges write markdown for humans, so the shape varies a lot between models:
bolded rank cells, ``9/10`` for a tie, two labels in one row, an ordered list
instead of a table. This module tries three readers in order of reliability and
reports which one worked, so a report can be honest about how the numbers were
obtained rather than presenting a regex guess as data.

The only fully reliable reader is the first one: a judge that ends its verdict
with an explicit ``RANKING:`` line. The other two are best-effort salvage for
verdicts written before that was asked for.
"""
import re

# "RANKING: {{E}} > {{C}} > {{J}}" -- one line, unambiguous, order is the rank.
RANKING_LINE = re.compile(r"^\s*RANKING\s*:\s*(.+)$", re.M | re.I)

# A markdown row whose first cell is a rank: "| 1 |", "| **2** |", "| 9/10 |".
TABLE_ROW = re.compile(r"^\|([^|\n]*)\|([^|\n]*)\|", re.M)

# "1. Output A -- ..." or "### 3. {{C}}"
#
# The 80 characters are a window on the start of the row, not a limit on the
# row: a judge that follows its label with a sentence of justification is
# writing the same ranking as one that doesn't. Anchoring this to $ dropped
# whole entries -- silently, since a short row either side of a long one still
# parsed, so the verdict came back looking merely "incomplete" rather than
# misread. It cost the winner of session 20260726-145930 a first-place vote.
LIST_ROW = re.compile(r"^\s*#{0,4}\s*\**(\d{1,2})\**[.)]\s+(.{0,80})", re.M)


def _labels_in(cell, allowed):
    """Labels mentioned in one table cell, in order, deduped.

    Accepts "{{A}}", "**A**", "A", "B & G", "A=Model/T" (a de-anonymised file).
    Only labels the session actually has are returned, which is what keeps a
    stray capital in prose from being read as a label.
    """
    found = []
    for m in re.finditer(r"\{\{\s*([A-Z])\s*\}\}|(?<![\w])([A-Z])(?![\w])", cell):
        label = m.group(1) or m.group(2)
        if label in allowed and label not in found:
            found.append(label)
    return found


def _ranks_in(cell):
    """Rank numbers in a rank cell. '9/10' -> [9.0, 10.0], '**2**' -> [2.0]."""
    return [float(n) for n in re.findall(r"\d{1,2}", cell)]


def from_ranking_line(body, allowed):
    """'RANKING: {{E}} > {{C}} > ...' -> {label: rank}. Exact, no guessing."""
    m = RANKING_LINE.search(body)
    if not m:
        return {}
    order = []
    for chunk in re.split(r"[>,]", m.group(1)):
        order += [l for l in _labels_in(chunk, allowed) if l not in order]
    return {label: float(i + 1) for i, label in enumerate(order)}


def from_table(body, allowed):
    """Read the first markdown table that looks like a ranking.

    Rows are taken in document order and the first mention of a label wins, so
    a judge repeating its table in a conclusion doesn't overwrite the original.
    """
    ranks = {}
    for row in TABLE_ROW.finditer(body):
        rank_cell, label_cell = row.group(1), row.group(2)
        numbers = _ranks_in(rank_cell)
        # A rank cell is a small number and nothing else; "| 1 |" not "| 2168 |".
        if not numbers or len(rank_cell.strip(" *`")) > 6 or max(numbers) > 40:
            continue
        labels = _labels_in(label_cell, allowed)
        if not labels:
            continue
        value = sum(numbers) / len(numbers)      # "9/10" -> 9.5, a declared tie
        for label in labels:
            ranks.setdefault(label, value)
    return ranks


def from_list(body, allowed):
    """Fallback: '1. Output A', '### 2. {{B}}'."""
    ranks = {}
    for m in LIST_ROW.finditer(body):
        for label in _labels_in(m.group(2), allowed):
            ranks.setdefault(label, float(m.group(1)))
    return ranks


READERS = (
    ("ranking-line", from_ranking_line),   # exact
    ("table", from_table),                 # best-effort
    ("list", from_list),                   # best-effort
)


def extract(body, allowed):
    """-> (ranks, method, complete).

    ``complete`` is True when every label in the session got a rank; a partial
    ranking is still returned (it is usable for head-to-head) but is excluded
    from agreement statistics, which need a full ordering from every judge.
    """
    allowed = set(allowed)
    for method, reader in READERS:
        ranks = reader(body, allowed)
        if len(ranks) >= 2:
            return ranks, method, len(ranks) == len(allowed)
    return {}, "none", False


def extract_all(session):
    """-> list of {judge, ranks, method, complete} for every judge verdict.

    Ranks come back in CANONICAL labels whatever order the judge read in. A
    judge with its own document wrote its own letters; translating here means
    nothing downstream -- scoring, the heatmap, the de-anonymiser -- has to know
    that per-judge orders exist at all.
    """
    canonical = set(session["key"])
    out = []
    for judge in session["judges"]:
        mapping = judge.get("label_map") or {}
        allowed = set(mapping) if mapping else canonical
        ranks, method, complete = extract(judge["body"], allowed)
        if mapping:
            ranks = {mapping[l]: r for l, r in ranks.items() if l in mapping}
            complete = len(ranks) == len(canonical)
        out.append({
            "judge": judge["judge"],
            "file": judge["file"],
            "ranks": ranks,
            "method": method,
            "complete": complete,
            # Where this judge read each entry: 1 = first. Same thing as its
            # own letters, which the builder assigns by position.
            "positions": {c: ord(l) - 64 for l, c in mapping.items()},
        })
    return out
