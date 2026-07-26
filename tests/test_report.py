"""Report rendering: no dead warning banner, and full text is present but collapsed."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from roundtable import consensus, ranks, report, session as session_mod

RUN = """---
model: "alpha-7B"
thinking: true
temperature: 1.0
seed: 42
tokens: 500
tokens_per_sec: 40.0
elapsed_sec: 12
error: null
---

## Thinking

some reasoning trace

## Output

The generated haiku text.
"""

RUN_B = RUN.replace("alpha-7B", "beta-7B").replace(
    "The generated haiku text.", "A weaker haiku.")

VERDICT = """---
model: "gamma-7B"
thinking: true
temperature: 0.7
seed: 42
tokens: 200
tokens_per_sec: 30.0
elapsed_sec: 6
error: null
---

## Output

Judge verdict prose here.

RANKING: {{A}} > {{B}}
"""

KEY_TWO = ("| Output | Model | Mode |\n|---|---|---|\n"
          "| {{A}} | alpha-7B | thinking |\n| {{B}} | beta-7B | thinking |\n")


class ReportRenderTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rt-report-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def render(self):
        data = session_mod.load(self.dir)
        result = consensus.score(data, ranks.extract_all(data))
        return data, report.render(data, result, ranks.extract_all(data))

    def test_no_labelled_warning_banner(self):
        """The 'Judges saw the model names' banner was removed on request --
        must not regress back in."""
        self.write("01_x_alpha_thinking.md", RUN)
        _, html = self.render()
        self.assertNotIn("Judges saw the model names", html)
        self.assertNotIn("class=\"warn\"", html)

    def test_run_output_present(self):
        self.write("01_x_alpha_thinking.md", RUN)
        _, html = self.render()
        self.assertIn("Judged outputs", html)
        self.assertIn("The generated haiku text.", html)
        # The thinking trace is not the point of this section.
        self.assertNotIn("some reasoning trace", html)

    def test_single_run_no_judges_is_collapsed(self):
        """With no scored panel there is no 'winner' to expand."""
        self.write("01_x_alpha_thinking.md", RUN)
        _, html = self.render()
        self.assertIn('<details><summary><span class="sumtext">alpha-7B', html)
        self.assertNotIn("<details open>", html)

    def test_winning_output_is_expanded_by_default(self):
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write("summary_20260101-000000_alpha-7B.md", VERDICT)
        self.write("SUMMARIZE-KEY.md", KEY_TWO)
        _, html = self.render()
        # Exactly one entry is open, and it's the one the panel ranked first.
        self.assertEqual(html.count("<details open>"), 1)
        winner = html.split("<details open>", 1)[1]
        self.assertTrue(winner.startswith('<summary><span class="sumtext">A'))
        self.assertIn("The generated haiku text.", winner.split("</details>", 1)[0])
        self.assertNotIn("A weaker haiku", winner.split("</details>", 1)[0])

    def test_sections_appear_in_results_first_order(self):
        """Synthesis leads, then outputs, then standings, then judgements --
        benchmark tables (Runs/Judges stats) come after, at the bottom."""
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write("summary_20260101-000000_alpha-7B.md", VERDICT)
        self.write("SUMMARIZE-KEY.md", KEY_TWO)
        self.write("round3_20260101-000000_alpha-7B.md", VERDICT)
        _, html = self.render()
        i_round3 = html.index("Round 3")
        i_outputs = html.index("Judged outputs")
        i_standings = html.index("<h2>Panel standings</h2>")
        i_verdicts = html.index("Judgements")
        i_heatmap = html.index("Who ranked what")
        i_runs_table = html.index("<h2>Runs</h2>")
        self.assertLess(i_round3, i_outputs)
        self.assertLess(i_outputs, i_standings)
        self.assertLess(i_standings, i_verdicts)
        # Judgements sits directly below standings -- nothing else between them.
        self.assertLess(i_verdicts, i_heatmap)
        self.assertLess(i_heatmap, i_runs_table)

    def test_judge_verdict_present_and_collapsed(self):
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write("summary_20260101-000000_alpha-7B.md", VERDICT)
        self.write("SUMMARIZE-KEY.md", KEY_TWO)
        _, html = self.render()
        self.assertIn("Judge verdict prose here.", html)

    def test_outputs_are_sorted_by_rank_not_file_order(self):
        """Files are written alpha, beta, gamma; the panel ranks B > C > A --
        the rendered order must follow the panel, not the filenames."""
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write("03_x_gamma_thinking.md",
                  RUN.replace("alpha-7B", "gamma-7B").replace(
                      "The generated haiku text.", "A gamma haiku."))
        self.write("SUMMARIZE-KEY.md",
                  "| Output | Model | Mode |\n|---|---|---|\n"
                  "| {{A}} | alpha-7B | thinking |\n| {{B}} | beta-7B | thinking |\n"
                  "| {{C}} | gamma-7B | thinking |\n")
        order = "{{B}} > {{C}} > {{A}}"
        for judge in ("beta-7B", "gamma-7B", "alpha-7B"):
            self.write("summary_20260101-000000_%s.md" % judge,
                      VERDICT.replace('model: "gamma-7B"', 'model: "%s"' % judge)
                      .replace("RANKING: {{A}} > {{B}}", "RANKING: " + order))
        _, html = self.render()

        outputs = html[html.index("Judged outputs"):html.index("<h2>Panel standings</h2>")]
        self.assertLess(outputs.index("beta-7B"), outputs.index("gamma-7B"))
        self.assertLess(outputs.index("gamma-7B"), outputs.index("alpha-7B"))

    def test_verdicts_are_sorted_by_agreement_with_consensus(self):
        """Two judges agree with each other (and so with the consensus); one
        ranks the exact opposite. The dissenting verdict must render last --
        and the filenames are deliberately ordered so a naive file-order
        render would get this wrong.
        """
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write("03_x_gamma_thinking.md",
                  RUN.replace("alpha-7B", "gamma-7B").replace(
                      "The generated haiku text.", "A gamma haiku."))
        self.write("SUMMARIZE-KEY.md",
                  "| Output | Model | Mode |\n|---|---|---|\n"
                  "| {{A}} | alpha-7B | thinking |\n| {{B}} | beta-7B | thinking |\n"
                  "| {{C}} | gamma-7B | thinking |\n")
        # Sorts alphabetically as: aaa-dissenter, mmm-agree-one, zzz-agree-two.
        self.write("summary_20260101-000000_aaa-dissenter.md",
                  VERDICT.replace('model: "gamma-7B"', 'model: "aaa-dissenter"')
                  .replace("RANKING: {{A}} > {{B}}", "RANKING: {{C}} > {{B}} > {{A}}"))
        self.write("summary_20260101-000000_mmm-agree-one.md",
                  VERDICT.replace('model: "gamma-7B"', 'model: "mmm-agree-one"')
                  .replace("RANKING: {{A}} > {{B}}", "RANKING: {{A}} > {{B}} > {{C}}"))
        self.write("summary_20260101-000000_zzz-agree-two.md",
                  VERDICT.replace('model: "gamma-7B"', 'model: "zzz-agree-two"')
                  .replace("RANKING: {{A}} > {{B}}", "RANKING: {{A}} > {{B}} > {{C}}"))
        _, html = self.render()

        verdicts = html[html.index("Judgements"):html.index("<h2>Runs</h2>")]
        i_dissenter = verdicts.index("aaa-dissenter")
        i_agree_one = verdicts.index("mmm-agree-one")
        i_agree_two = verdicts.index("zzz-agree-two")
        self.assertLess(i_agree_one, i_dissenter)
        self.assertLess(i_agree_two, i_dissenter)

    def test_no_output_section_when_no_runs(self):
        self.assertEqual(report._outputs_section({"runs": []}, {"standings": []}), "")

    def test_no_verdicts_section_when_no_judges(self):
        self.assertEqual(report._verdicts_section({"judges": []}, {"standings": []}), "")

    def test_tiles_show_done_over_total(self):
        self.write("01_x_alpha_thinking.md", RUN)
        self.write(".expected-runs", "4\n")
        self.write(".expected-judges", "2\n")
        _, html = self.render()
        self.assertIn("Output runs", html)
        self.assertIn(">1/4<", html)
        self.assertIn("Judge runs", html)
        self.assertIn(">0/2<", html)
        self.assertIn("3 to go", html)

    def test_totals_fall_back_to_what_is_on_disk(self):
        """An older session has no expected-* files; it reads 1/1, not 1/0."""
        self.write("01_x_alpha_thinking.md", RUN)
        data, html = self.render()
        self.assertEqual(report._counts(data), (1, 1, 0, 0))
        self.assertIn(">1/1<", html)

    def test_total_never_below_what_exists(self):
        """A stray extra result file must not render '2/1'."""
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("02_x_beta_thinking.md", RUN_B)
        self.write(".expected-runs", "1\n")
        data, _ = self.render()
        self.assertEqual(report._counts(data)[:2], (2, 2))

    def test_progress_bar_has_a_cell_per_run_and_an_eta(self):
        self.write("01_x_alpha_thinking.md", RUN)
        self.write(".expected-runs", "4\n")
        self.write(".expected-judges", "2\n")
        data = session_mod.load(self.dir)
        # 10 minutes in, 1 of 6 units done -> 10 min each, 50 min to go.
        data["started"] = data["touched"] = None
        now = 1_800_000_000.0
        data["started"], data["touched"] = now - 600, now - 60
        bar = report._progress(data, now=now)
        self.assertEqual(bar.count('class="c '), 6)
        self.assertEqual(bar.count("c done"), 1)
        self.assertEqual(bar.count("c pend"), 5)
        self.assertIn("<b>1 of 6</b>", bar)
        self.assertIn("5 to go", bar)
        self.assertIn("50 min", bar)

    def test_no_eta_for_a_session_nobody_is_writing_to(self):
        """A queue that died leaves its counts short forever -- an ETA computed
        from them would be fiction, so the bar says so instead."""
        self.write("01_x_alpha_thinking.md", RUN)
        self.write(".expected-runs", "4\n")
        data = session_mod.load(self.dir)
        now = 1_800_000_000.0
        data["started"] = now - 86400
        data["touched"] = now - 86000
        self.assertIsNone(report._eta(data, 1, 4, now=now))
        self.assertIn("stopped", report._progress(data, now=now))

    def test_finished_session_reports_how_long_it_took(self):
        self.write("01_x_alpha_thinking.md", RUN)
        data = session_mod.load(self.dir)
        now = 1_800_000_000.0
        data["started"], data["touched"] = now - 3600, now - 60
        bar = report._progress(data, now=now)
        self.assertIn("<b>1 of 1</b>", bar)
        self.assertIn("took 59 min", bar)
        self.assertNotIn("ETA", bar)

    def test_bar_sits_directly_under_the_tiles(self):
        self.write("01_x_alpha_thinking.md", RUN)
        _, html = self.render()
        self.assertGreater(html.index('class="prog"'), html.index('class="tiles"'))
        self.assertLess(html.index('class="prog"'), html.index("<footer>"))

    def test_round3_counts_as_the_last_judge_run(self):
        """It loads a model and generates like any judge; a report that ignored
        it would say 'complete' with 17 GB still loading."""
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("summary_20260101-000000_alpha-7B.md", VERDICT)
        self.write(".expected-runs", "1\n")
        self.write(".expected-judges", "1\n")
        self.write(".expected-meta", "1\n")
        data = session_mod.load(self.dir)
        self.assertEqual(report._counts(data), (1, 1, 1, 2))
        bar = report._progress(data, now=1_800_000_000.0)
        self.assertIn("<b>2 of 3</b>", bar)
        self.assertIn("synthesis (round 3) — not run yet", bar)

    def test_round3_present_counts_as_done(self):
        self.write("01_x_alpha_thinking.md", RUN)
        self.write("summary_20260101-000000_alpha-7B.md", VERDICT)
        self.write("round3_20260101-000000_alpha-7B.md", RUN)
        data = session_mod.load(self.dir)
        self.assertEqual(report._counts(data), (1, 1, 2, 2))
        bar = report._progress(data, now=1_800_000_000.0)
        self.assertIn("<b>3 of 3</b>", bar)
        self.assertNotIn("not run yet", bar)


if __name__ == "__main__":
    unittest.main()
