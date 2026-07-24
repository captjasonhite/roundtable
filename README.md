![Roundtable](docs/logo.png)

# Roundtable

Local LLMs sitting around a table: every model answers the same prompt, they
judge each other's answers blind, and the panel's own top pick writes the final
word — three rounds, one HTML report of who won, who disagreed, and how much of
the result is just models flattering themselves.

No dependencies. No build step. `roundtable report <session>` writes one
self-contained `report.html` you can open with `file://`; `roundtable up` runs
the whole thing — form, worker, live report — as one local app.

![panel standings, a judge-by-output rank heatmap, and a self-preference chart](docs/screenshot.png)

## The three rounds

1. **Generation.** Every model answers the identical prompt. (`roundtable`
   shells out to `creative-bench.sh` for the actual GPU work — see below.)
2. **Blind judging.** Every model reads all the answers, anonymised, and ranks
   them. See "Why it exists" for why the anonymising is load-bearing, not
   cosmetic.
3. **Synthesis.** Roundtable computes the consensus itself — mean percentile,
   agreement, where judges split — and hands that *table*, not raw judge prose,
   to whichever model the panel rated highest. That model writes the final
   summary. It is reporting the numbers, not re-opening the judging: see
   `digest.py`.

Round 3 is optional and needs Round 2 to have actually produced a scored panel
(blind judging, at least one usable ranking) — skipped otherwise, not
fabricated.

## Why blind judging is load-bearing

Benchmarking creative writing has no unit test. The usual fix is to let the models
judge each other — but that only works if you take two things away from them:

**1. Names.** In an early session, one model produced *byte-identical* text in two
runs, and every judge still ranked the copy labelled "no thinking" lower — one of
them by eight places. They were grading the label, not the prose. So outputs are
shown to judges as anonymous tags (`{{A}}`, `{{B}}`…), shuffled, with speed and
mode stripped out. A session judged with names visible is rendered but never
scored, and the report says why.

**2. Their own vote.** Models are not neutral about their own work — in both
directions. Self-votes are excluded from every score, head-to-head and percentile
on the page, and reported separately as a self-preference chart, because the size
and *sign* of that bias turns out to be one of the more interesting numbers in the
run.

## What's in a report

| Section | Round | Question it answers |
|---|---|---|
| Synthesis | 3 | The panel's top pick reporting the computed consensus in prose |
| Panel standings | 2 | Who won, by mean percentile across judges (self-votes removed) |
| Who ranked what | 2 | A judge × output rank heatmap — where the panel agreed and where it split |
| Agreement (Kendall's W) | 2 | Is this a consensus or a coin flip? Tie-corrected |
| Self-preference | 2 | How far each model placed its own output from where the panel placed it |
| Speed against quality | 1+2 | Panel score against tok/s — what the extra time bought you |
| Runs | 1 | Raw numbers per generation |
| Judges | 2 | How each verdict's ranking was read |

Every chart has a table beside it, works in light and dark, and carries no
JavaScript — the whole page is CSS and hand-generated SVG.

## Usage

```sh
roundtable report SESSION                # -> SESSION/report.html
roundtable report --all ~/bench-results  # every session under a root
roundtable report SESSION --no-prompts   # leave your prompts out, for sharing

roundtable submit job.json               # queue a bench run
roundtable work                          # run queued jobs, one at a time
roundtable work --drain                  # ...then stop when the queue empties
roundtable status                        # queue depth + what the worker is doing
```

`--running` is how live progress works without a server: the report is rewritten
after every run and carries `<meta http-equiv="refresh">`, so the browser polls by
simply reloading. The final write drops the tag — when the page stops reloading,
the session is done. Every write is atomic, so a reload can never catch a
half-written file.

The worker rebuilds **on each new result file**, not on a clock, so the page fills
in as runs land. A fallback timer covers the gaps and retunes itself after every
run — about three rebuilds per run, clamped to 5–60 s. A 20-second run polls
briskly; a five-minute one doesn't rewrite the page thirty times for nothing.
The first run has nothing to learn from yet, so it starts at 10 s.

## The queue

```
$ROUNDTABLE_SPOOL/          (default ~/.local/state/roundtable)
    queue/     jobs waiting, oldest first
    running/   the one job a worker has claimed
    done/      finished, with the session directory and exit code recorded
    failed/    what went wrong, kept for reading
    logs/      runner output, one file per job
    worker.json  heartbeat: which worker, doing what, since when
```

No lock, no database, no daemon protocol. A worker claims a job by **renaming**
it out of `queue/` — rename is atomic within a filesystem, so if two workers ever
race, exactly one wins. Submitting writes a dot-file and renames it in, so a
scan can never pick up a half-written job. Nothing is lost if the machine dies
mid-run: the job file is still sitting in `running/`.

A single worker consuming the queue serially *is* the "one model in VRAM at a
time" constraint — expressed as architecture rather than as a mutex someone has
to remember to take.

A job is plain JSON:

```json
{
  "system_prompt": "You are a careful editor.",
  "user_prompt": "Rewrite the opening with more tension.",
  "temperature": 1.0,
  "mode": "thinking",
  "models": ["Qwen3.6-27B:thinking", "Gemma4-26B:nothinking"],
  "summarize": true,
  "meta_summary": true,
  "blind": true
}
```

`models` entries are substring patterns matched against the runner's model
paths, each optionally carrying its own mode as `pattern:thinking` /
`:nothinking` / `:both` — the form's per-model checkboxes send exactly this.
A bare pattern with no suffix falls back to the top-level `mode`. `summarize`
is Round 2 (blind judging); `meta_summary` is Round 3 and needs
`summarize: true` to have anything to synthesise — the worker skips it,
rather than fabricating a summary, if Round 2 didn't produce a scored panel.
The worker turns the job into flags for the bench script — it reimplements no
benchmarking logic of its own; Round 3 is one more call to the same script,
`--meta-summary <dir> --meta-model <slug>`, appending a `round3_*.md` into the
existing session rather than starting a new one.

### Running it as a service

```sh
cp packaging/roundtable-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now roundtable-worker
```

Then queue five configurations, close the browser, and come back to five
finished reports. The web page never drives a run — it's a window onto a file the
worker keeps rewriting.

## Reading a session directory

Roundtable reads what [`creative-bench.sh`](#the-bench-runner) leaves on disk:

```
20260723-211143/
    system-prompt.txt  user-prompt.txt   Round 1's prompt (never overwritten by Round 3)
    01_<stamp>_<model>_thinking.md       Round 1: one per run, frontmatter + output
    SUMMARIZE.md                         what the Round 2 judges were shown
    SUMMARIZE-KEY.md                     tag -> model (absent = not blind)
    summary_<stamp>_<model>.md           Round 2: one per judge
    round3_<stamp>_<model>.md            Round 3: the panel's top pick's synthesis
```

Rankings are read from each verdict by three readers, most reliable first:

1. **`RANKING: {{A}} > {{B}} > {{C}}`** — an explicit final line. Exact.
2. A markdown rank table — handles bold cells, `9/10` shared ranks, and two
   outputs in one row. Best-effort.
3. An ordered list. Best-effort.

The report always states which reader was used, so a regex guess is never
presented as data. If a verdict is pure prose with no table, Roundtable reports
nothing for it rather than inventing a ranking.

## Status

Working — 135 tests, `python3 -m unittest discover -s tests`:

- `roundtable report`, and the rank/consensus extraction behind it
- `roundtable submit` / `work` / `status`, the file queue, and live report
  rebuilds during a run
- `roundtable serve` / `up` — the submit form (with the built-in role presets,
  see `presets/`) and a one-command app: form + worker together
- Saving, editing, deleting, and resetting your own presets from the form
  itself — no hand-editing `~/.config/roundtable/presets.json` required.
  Saving under an existing preset's name edits it in place (matched by id,
  not by title text, since a bundled preset's id doesn't always match its
  title — e.g. `red-team` for "Red Team / Devil's Advocate"). Deleting an
  edited *bundled* preset reverts to the shipped original rather than
  removing it outright; "Reset to factory presets" does that for everything
  at once by discarding the user file entirely.
- Round 3 (`digest.py`): computing the consensus table, picking the panel's top
  entry, and running it as one more call to the bench script

The worker suite runs against `tests/stub_runner.sh`, a stand-in that writes a
session directory with the same shape as the real thing (all three rounds) in
about a second — so the queue, the worker and the live rebuild are all testable
without loading a model onto a GPU.

Planned:

- **Resume.** A worker stopped mid-run marks the job failed and keeps whatever
  runs finished; it does not restart where it left off. Real resume needs the
  bench runner to skip runs whose result files already exist *and* to reuse the
  original session seed — a resumed session that re-rolls its seed is no longer
  a fair comparison.

One worker consuming a queue serially *is* the "one model in VRAM at a time"
constraint expressed as architecture — no lock manager, no streaming, no session
state. The whole pipeline is debuggable with `ls`.

## License

MIT
