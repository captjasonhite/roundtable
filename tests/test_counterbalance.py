"""Per-judge running orders, and the two properties they exist for.

The builder lives in a heredoc inside creative-bench.sh, so this drives the
real script against stub result files rather than reimplementing its logic.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import ranks, session as session_mod

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bin", "creative-bench.sh")

RUN = """---
model: "%s"
model_path: "/models/%s.gguf"
thinking: false
temperature: 1.0
seed: 7
tokens: 100
tokens_per_sec: 10.0
elapsed_sec: 10
error: null
---

## Thinking

(none)

## Output

Output written by %s.
"""


def build_docs(sdir, models, seed="7"):
    """Run just the SUMMARIZE builder out of the bench script."""
    text = open_text(SCRIPT)
    block = re.search(r"SDIR=\"\$SDIR\".*?<<'PYEOF'\n(.*?)\nPYEOF\n", text, re.S)
    assert block, "the SUMMARIZE builder moved -- this test needs updating"
    helper = os.path.join(sdir, "_build.py")
    with open(helper, "w", encoding="utf-8") as f:
        f.write(block.group(1))
    env = dict(os.environ, SDIR=sdir, TEMP="1.0", SEED=seed, BLIND="1",
               JUDGE_SLUGS=",".join(models))
    proc = subprocess.run([sys.executable, helper], env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def open_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def positions_from_doc(sdir, judge):
    """-> [model, ...] in the order that judge's document presents them."""
    text = open_text(os.path.join(sdir, "SUMMARIZE-%s.md" % judge))
    body = text.split("## Results", 1)[1]
    order = []
    for chunk in body.split("### Output ")[1:]:
        order.append(re.search(r"Output written by (\S+?)\.", chunk).group(1))
    return order


class CounterbalanceTests(unittest.TestCase):

    MODELS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-cb-")
        for i, m in enumerate(self.MODELS, 1):
            with open(os.path.join(self.dir, "%02d_x_%s_nothinking.md" % (i, m)),
                      "w", encoding="utf-8") as f:
                f.write(RUN % (m, m, m))
        for name in ("system-prompt.txt", "user-prompt.txt"):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write("prompt")
        build_docs(self.dir, self.MODELS)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_judge_gets_its_own_document(self):
        for m in self.MODELS:
            self.assertTrue(os.path.exists(
                os.path.join(self.dir, "SUMMARIZE-%s.md" % m)), m)
            self.assertTrue(os.path.exists(
                os.path.join(self.dir, "SUMMARIZE-KEY-%s.md" % m)), m)

    def test_a_judge_reads_its_own_entry_last(self):
        """The penalised slot is spent on the vote the scorer throws away."""
        for m in self.MODELS:
            self.assertEqual(positions_from_doc(self.dir, m)[-1], m)

    def test_each_entry_sits_in_each_counted_position_exactly_once(self):
        """The property the whole design exists for: no averaging, no residue."""
        seen = {m: [] for m in self.MODELS}
        for judge in self.MODELS:
            for pos, model in enumerate(positions_from_doc(self.dir, judge), 1):
                if model != judge:                # self-votes are not counted
                    seen[model].append(pos)
        for model, got in seen.items():
            self.assertEqual(sorted(got), [1, 2, 3, 4, 5],
                             "%s was read at %s" % (model, sorted(got)))

    def test_documents_differ_but_carry_the_same_outputs(self):
        orders = {m: positions_from_doc(self.dir, m) for m in self.MODELS}
        self.assertEqual(len(set(tuple(o) for o in orders.values())), len(self.MODELS))
        for order in orders.values():
            self.assertEqual(sorted(order), sorted(self.MODELS))

    def test_the_order_is_not_the_same_every_session(self):
        """Otherwise every session repeats one running order, and with it
        whatever effect an entry has on the one read after it."""
        other = tempfile.mkdtemp(prefix="rt-cb2-")
        try:
            for name in os.listdir(self.dir):
                if name.endswith(".md") or name.endswith(".txt"):
                    shutil.copy(os.path.join(self.dir, name), other)
            for stale in os.listdir(other):
                if stale.startswith("SUMMARIZE"):
                    os.remove(os.path.join(other, stale))
            build_docs(other, self.MODELS, seed="999")
            self.assertNotEqual(positions_from_doc(self.dir, "alpha"),
                                positions_from_doc(other, "alpha"))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_a_verdict_is_read_back_in_canonical_labels(self):
        """A judge's {{A}} is its own; the scorer must never see it as such."""
        judge = "alpha"
        mapping = session_mod.read_judge_key(self.dir, judge)
        self.assertEqual(len(mapping), len(self.MODELS))
        # Write a verdict in this judge's own letters, ranking them in order.
        letters = sorted(mapping)
        line = "RANKING: " + " > ".join("{{%s}}" % l for l in letters)
        with open(os.path.join(self.dir, "summary_20260726-000000_%s.md" % judge),
                  "w", encoding="utf-8") as f:
            f.write("---\nmodel: \"%s\"\nerror: null\n---\n\n## Output\n\n%s\n"
                    % (judge, line))
        data = session_mod.load(self.dir)
        parsed = ranks.extract_all(data)[0]
        # Ranked in its own letter order, so canonical label X must carry the
        # rank of whichever of its letters maps to X.
        for mine, canonical in mapping.items():
            self.assertEqual(parsed["ranks"][canonical], float(letters.index(mine) + 1))
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["positions"][mapping["A"]], 1)

    def test_sessions_without_per_judge_keys_still_read(self):
        """Every session judged before today has one shared order."""
        for name in os.listdir(self.dir):
            if name.startswith("SUMMARIZE-KEY-"):
                os.remove(os.path.join(self.dir, name))
        key = session_mod.read_key(self.dir)
        letters = sorted(key)
        with open(os.path.join(self.dir, "summary_20260726-000000_alpha.md"),
                  "w", encoding="utf-8") as f:
            f.write("---\nmodel: \"alpha\"\nerror: null\n---\n\n## Output\n\n"
                    "RANKING: %s\n" % " > ".join("{{%s}}" % l for l in letters))
        data = session_mod.load(self.dir)
        parsed = ranks.extract_all(data)[0]
        self.assertEqual(parsed["ranks"][letters[0]], 1.0)
        self.assertTrue(parsed["complete"])


if __name__ == "__main__":
    unittest.main()
