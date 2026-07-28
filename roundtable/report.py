"""Render one session as a self-contained HTML report.

No network requests, no build step: the output is a single file you can open
with file:// or serve from anywhere. Charts are hand-written SVG and CSS so
the report has no external dependencies. The only JavaScript is the copy-to-
clipboard button on each expandable section.

While a session is still running the report is regenerated after every run and
carries a <meta refresh>, so the browser polls by simply reloading. The final
write drops the refresh tag -- "it stopped reloading" means "it finished".
"""
import html
import os
import time

from . import consensus as C
from . import naming
from . import session as session_mod

# Palette: the validated default from the data-viz reference (blue sequential
# ramp, blue<->red diverging pair). Both modes are selected, not auto-flipped.
CSS = """
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10); --series:#2a78d6;
  --pos:#e34948; --neg:#2a78d6; --zero:#f0efec;
  --b0:#0d366b; --b1:#184f95; --b2:#256abf; --b3:#3987e5;
  --b4:#6da7ec; --b5:#9ec5f4; --b6:#cde2fb;
  --t0:#fff; --t1:#fff; --t2:#fff; --t3:#0b0b0b; --t4:#0b0b0b; --t5:#0b0b0b; --t6:#0b0b0b;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --series:#3987e5;
    --pos:#e66767; --neg:#3987e5; --zero:#383835;
    --b0:#86b6ef; --b1:#5598e7; --b2:#3987e5; --b3:#256abf;
    --b4:#1c5cab; --b5:#184f95; --b6:#104281;
    --t0:#0b0b0b; --t1:#0b0b0b; --t2:#0b0b0b; --t3:#fff; --t4:#fff; --t5:#fff; --t6:#fff;
  }
}
:root[data-theme="dark"]{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --series:#3987e5;
  --pos:#e66767; --neg:#3987e5; --zero:#383835;
  --b0:#86b6ef; --b1:#5598e7; --b2:#3987e5; --b3:#256abf;
  --b4:#1c5cab; --b5:#184f95; --b6:#104281;
  --t0:#0b0b0b; --t1:#0b0b0b; --t2:#0b0b0b; --t3:#fff; --t4:#fff; --t5:#fff; --t6:#fff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 72px}
h1{font-size:23px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:16px;margin:38px 0 6px;letter-spacing:-.005em}
.eyebrow{font-size:11.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);margin:38px 0 2px}
.eyebrow+h2{margin-top:2px}
.round3{border:1px solid var(--border);border-radius:10px;padding:20px 22px;
  margin:0 0 18px;background:var(--surface)}
.round3 .body{white-space:pre-wrap;font-size:14.5px;line-height:1.6;margin-top:10px}
p.note{color:var(--ink2);margin:4px 0 16px;font-size:13.5px}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:18px 20px;margin:0 0 18px;overflow-x:auto}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 18px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:12px 16px;min-width:130px;flex:1}
.tile .v{font-size:26px;line-height:1.15;letter-spacing:-.02em}
.tile .v.sm{font-size:16px;line-height:1.3;letter-spacing:0;padding:4px 0 3px}
.tile .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .h{font-size:12.5px;color:var(--ink2);margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{text-align:left;font-weight:600;color:var(--ink2);padding:6px 10px 6px 0;
  border-bottom:1px solid var(--axis);white-space:nowrap}
td{padding:6px 10px 6px 0;border-bottom:1px solid var(--grid);vertical-align:middle}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.tag{display:inline-block;background:var(--zero);border:1px solid var(--border);
  border-radius:5px;padding:0 5px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
.bar{height:13px;border-radius:0 4px 4px 0;background:var(--series);min-width:2px}
.barrow{display:flex;align-items:center;gap:10px;margin:0 0 6px}
.barrow .lbl{width:345px;flex:none;font-size:13px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.barrow .track{flex:1;min-width:80px;position:relative}
/* Where the score lands if the panel is resampled -- drawn over the bar, so
   two entries whose whiskers overlap read as the tie they are. */
.barrow .ci{position:absolute;top:3px;height:7px;border-left:2px solid var(--ink2);
  border-right:2px solid var(--ink2);opacity:.55}
.barrow .ci::before{content:"";position:absolute;top:2px;left:0;right:0;
  border-top:2px solid var(--ink2)}
.barrow .val{width:52px;flex:none;text-align:right;font-size:13px;
  font-variant-numeric:tabular-nums;color:var(--ink2)}
.heat{border-collapse:separate;border-spacing:2px;width:auto}
.heat td.cell{width:44px;text-align:center;border:none;padding:7px 0;border-radius:4px;
  font-size:12.5px;font-variant-numeric:tabular-nums}
.heat th{border-bottom:none;font-size:12.5px;padding:2px 8px 2px 0}
.heat th.out{width:44px;padding:0 0 6px;vertical-align:bottom;height:162px}
.heat th.out .rot{writing-mode:vertical-rl;transform:rotate(180deg);
  white-space:nowrap;font-weight:600;color:var(--ink);margin:0 auto;
  font-size:12px;letter-spacing:.01em}
.heat th.out .rot .tag{font-weight:400;font-size:10.5px;color:var(--muted);
  background:none;border:none;padding:0}
.s0{background:var(--b0);color:var(--t0)} .s1{background:var(--b1);color:var(--t1)}
.s2{background:var(--b2);color:var(--t2)} .s3{background:var(--b3);color:var(--t3)}
.s4{background:var(--b4);color:var(--t4)} .s5{background:var(--b5);color:var(--t5)}
.s6{background:var(--b6);color:var(--t6)}
.na{background:var(--zero);color:var(--muted)}
.legend{display:flex;align-items:center;gap:6px;margin:12px 0 0;font-size:12px;
  color:var(--muted)}
.legend i{width:26px;height:11px;border-radius:2px;display:inline-block}
.dbar{display:flex;align-items:center;gap:10px;margin:0 0 6px;font-size:13px}
.dbar .lbl{width:345px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dbar .track{flex:1;display:flex;align-items:center;min-width:120px}
.dbar .half{flex:1;display:flex}
.dbar .half.l{justify-content:flex-end}
.dbar .zero{width:1px;background:var(--axis);height:17px;flex:none}
.dbar b{height:13px;display:block;border-radius:4px 0 0 4px}
.dbar .half.r b{border-radius:0 4px 4px 0}
.dbar .val{width:52px;flex:none;text-align:right;color:var(--ink2);
  font-variant-numeric:tabular-nums}
/* Sits directly under the tiles and spans the same width, so the row of
   numbers and the bar that explains them read as one block. */
.prog{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px;margin:0 0 18px}
.prog .track{display:flex;gap:2px;align-items:stretch;height:16px}
.prog .c{flex:1;min-width:3px;border-radius:3px;display:block}
/* One colour per phase, walking down the sequential ramp: generating is most
   of the wall clock, judging follows it, the synthesis is the last step. */
.prog .c.run{background:var(--b4)}
.prog .c.judge{background:var(--b2)}
.prog .c.meta{background:var(--b0)}
.prog .c.fail{background:var(--pos)}
.prog .key{display:flex;gap:14px;margin-top:8px;font-size:12px;color:var(--muted)}
.prog .key span{display:flex;align-items:center;gap:5px}
.prog .key i{width:11px;height:11px;border-radius:3px;display:inline-block}
.prog .c.pend{background:var(--zero);border:1px solid var(--border)}
.prog .gap{flex:none;width:10px}
.prog .foot{display:flex;justify-content:space-between;gap:12px;margin-top:7px;
  font-size:12.5px;color:var(--ink2)}
.prog .foot b{color:var(--ink);font-weight:600}
.prog .eta{font-variant-numeric:tabular-nums;white-space:nowrap}
.prog .at{color:var(--muted)}
details{margin:10px 0 0;font-size:13.5px}
summary{cursor:pointer;color:var(--ink2);display:flex;align-items:center;gap:8px}
summary .sumtext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.copy-btn{flex:none;font-size:13px;line-height:1;color:var(--ink2);
  background:var(--page);border:1px solid var(--border);border-radius:5px;
  padding:4px 6px;cursor:pointer}
.copy-btn:hover{color:var(--ink);border-color:var(--axis)}
.copy-btn.copied{color:var(--series);border-color:var(--series)}
pre{white-space:pre-wrap;background:var(--page);border:1px solid var(--border);
  border-radius:8px;padding:12px;font-size:12.5px;overflow-x:auto;max-height:340px}
.warn{border-left:3px solid var(--pos);padding-left:12px;margin:0 0 18px}
footer{color:var(--muted);font-size:12.5px;margin-top:44px;border-top:1px solid var(--grid);
  padding-top:14px}
a{color:var(--series)}
"""


COPY_BTN = ('<button type="button" class="copy-btn" title="Copy" aria-label="Copy" '
           'onclick="rtCopy(this,event)">&#10697;</button>')


def esc(text):
    return html.escape(str(text if text is not None else ""))


def short_model(name, width=34):
    """Trim a GGUF name for a label without losing what it is."""
    name = (name or "").split("/")[-1]
    return name if len(name) <= width else name[:width - 1] + "…"


def _bin(rank, n):
    """Rank -> one of 7 sequential steps, 0 = best."""
    if n < 2:
        return 0
    p = (rank - 1) / (n - 1)
    return max(0, min(6, int(round(p * 6))))


# A session whose newest file is older than this isn't being written to any
# more: the queue was interrupted or the machine went to sleep. Its counts stay
# short forever, so an ETA derived from them would be fiction — the bar says
# "stopped" instead of guessing. Generous, because one 35B judge run at 16k
# tokens plus its model load is minutes, not seconds.
STALE_AFTER = 1800


def _has_meta(session):
    """Is a Round 3 synthesis part of this session -- done or still coming?"""
    return bool(session.get("meta_summary") or session.get("expected_meta"))


def _counts(session):
    """-> (runs_done, runs_total, judges_done, judges_total, meta_done, meta_total).

    Totals come from the counts creative-bench.sh drops in the session dir; they
    fall back to what's on disk, so an old session (or one whose runner never
    wrote them) reports "6/6" rather than "6/0". Never reports fewer total than
    done, so a stray extra result file can't produce "7/6".

    Round 3 -- the top model reading the panel's verdicts and writing the final
    synthesis -- counts as a judge run: it loads a model and generates, exactly
    like the six before it, and a report that ignored it would say "complete"
    with 17 GB still loading.
    """
    runs_done = len(session["runs"])
    judges_done = len(session["judges"])
    expected_runs = session.get("expected_runs")
    expected_judges = session.get("expected_judges")
    runs_total = max(runs_done, int(expected_runs or 0) or runs_done)
    judges_total = max(judges_done, int(expected_judges or 0) or judges_done)

    meta_done = 1 if session.get("meta_summary") else 0
    expected_meta = session.get("expected_meta")
    meta_total = meta_done if expected_meta is None else int(expected_meta)
    meta_total = max(meta_done, meta_total)
    # Reported apart from the judges. It IS a judge run -- it loads a model and
    # generates -- but folding it in made "0/6" read as "0 of 6 judges" when it
    # meant "0 of 5 judges and a synthesis", and the two are different waits.
    return runs_done, runs_total, judges_done, judges_total, meta_done, meta_total


def fmt_dur(seconds):
    """Seconds -> '4 min', '1 h 12 min', '< 1 min'. Coarse on purpose."""
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "< 1 min"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d min" % minutes
    return "%d h %02d min" % divmod(minutes, 60)


def _eta(session, done, total, now=None):
    """-> seconds until the queue finishes, or None when that can't be honest.

    A flat average over completed units: total wall time so far divided by the
    units that produced it, which is deliberately not the sum of the runs' own
    ``elapsed`` — that measures generation only, while half of a run's real cost
    is loading ~17 GB of weights onto the GPU and letting them release again.
    Judges and output runs are averaged together; they aren't the same length,
    so this is a working estimate, not a promise, and it's labelled as one.
    """
    now = now or time.time()
    started, touched = session.get("started"), session.get("touched")
    if not started or done <= 0 or done >= total:
        return None
    if now - started <= 0 or (touched and now - touched > STALE_AFTER):
        return None
    return (now - started) / done * (total - done)


def _progress(session, now=None):
    """The full-width bar under the tiles: one cell per run, plus an ETA.

    Reads the same as a finished session or a running one -- what's done, what's
    left, how long the rest will take -- so it needs no separate "running" mode;
    an ETA simply stops being offered once there's nothing left to wait for.
    """
    now = now or time.time()
    (runs_done, runs_total, judges_done, judges_total,
     meta_done, meta_total) = _counts(session)
    total = runs_total + judges_total + meta_total
    done = runs_done + judges_done + meta_done
    if not total:
        return ""

    meta_items = [session["meta_summary"]] if session.get("meta_summary") else []
    failed = sum(1 for r in session["runs"] if r["error"])
    failed += sum(1 for j in list(session["judges"]) + meta_items if j["error"])

    cells = []
    # One colour per phase: writing, judging, and the synthesis that reads the
    # judges. Without it a stalled bar gives no clue which stage stalled in.
    for group, items, group_total, noun in (
            ("run", session["runs"], runs_total, "output run"),
            ("judge", session["judges"], judges_total, "judge run"),
            ("meta", meta_items, meta_total, "synthesis (round 3)")):
        for i in range(group_total):
            item = items[i] if i < len(items) else None
            what = noun if group == "meta" else "%s %d" % (noun, i + 1)
            if item is None:
                cells.append('<i class="c pend" title="%s — not run yet"></i>'
                             % esc(what))
                continue
            name = item.get("model") or item.get("judge") or ""
            label = "%s · %s" % (what, short_model(name, 34))
            if group == "run" and item.get("mode"):
                label += " (%s)" % item["mode"]
            if item.get("elapsed"):
                label += " · %s" % fmt_dur(item["elapsed"])
            cls = "c fail" if item["error"] else "c %s" % group
            if item["error"]:
                label += " · failed"
            cells.append('<i class="%s" title="%s"></i>' % (cls, esc(label)))
        # A separator only when another phase actually follows, or the bar
        # ends on a gap that looks like a missing cell.
        if ((group == "run" and (judges_total or meta_total))
                or (group == "judge" and meta_total)):
            cells.append('<i class="gap"></i>')

    # Counted per phase, because they are different waits: generating is most of
    # the wall clock, judging follows it, and the synthesis is one last model.
    left = ["<b>%d/%d</b> generating" % (runs_done, runs_total)]
    if judges_total:
        left.append("<b>%d/%d</b> judging" % (judges_done, judges_total))
    if meta_total:
        left.append("<b>%d/%d</b> summary" % (meta_done, meta_total))
    if failed:
        left.append("%d failed" % failed)

    right = ""
    eta = _eta(session, done, total, now)
    if eta is not None:
        right = ("ETA &asymp; %s <span class=\"at\">(about %s)</span>"
                 % (fmt_dur(eta), time.strftime("%H:%M", time.localtime(now + eta))))
    elif done >= total:
        if session.get("started") and session.get("touched"):
            right = "took %s" % fmt_dur(session["touched"] - session["started"])
        else:
            right = "complete"
    elif session.get("touched") and now - session["touched"] > STALE_AFTER:
        right = ("stopped &mdash; nothing written for %s"
                 % fmt_dur(now - session["touched"]))

    key = ['<span><i class="c run"></i>generating</span>']
    if judges_total:
        key.append('<span><i class="c judge"></i>judging</span>')
    if meta_total:
        key.append('<span><i class="c meta"></i>summary</span>')
    if failed:
        key.append('<span><i class="c fail"></i>failed</span>')
    return ('<div class="prog"><div class="track">%s</div>'
            '<div class="foot"><span>%s</span><span class="eta">%s</span></div>'
            '<div class="key">%s</div></div>'
            % ("".join(cells), " · ".join(left), right, "".join(key)))


def _tiles(session, result, rankings):
    runs = session["runs"]
    ok = [r for r in runs if not r["error"]]
    tps = [r["tps"] for r in ok if r["tps"]]
    tiles = []

    top = next((r for r in result["standings"] if r["score"] is not None), None)
    if top:
        tiles.append(("Panel pick", short_model(top["model"], 30),
                      "%s · score %s" % (top["mode"], C.fmt(top["score"]))))
    (runs_done, runs_total, judges_done, judges_total,
     meta_done, meta_total) = _counts(session)
    run_hint = "all completed" if runs_done >= runs_total else "%d to go" % (runs_total - runs_done)
    if len(ok) != len(runs):
        run_hint = "%d failed" % (len(runs) - len(ok))
    tiles.append(("Output runs", "%d/%d" % (runs_done, runs_total), run_hint))
    # "0/6 + 0/1": the panel and the synthesis are separate waits, and a single
    # 0/7 hid the fact that a seventh model load was still to come.
    judge_value = "%d/%d" % (judges_done, judges_total)
    if meta_total:
        judge_value += " + %d/%d" % (meta_done, meta_total)
    left = (judges_total - judges_done) + (meta_total - meta_done)
    tiles.append(("Judge runs", judge_value,
                  "%d ranked every output" % result["agreement_n"]
                  if not left and judges_total
                  else "%d to go%s" % (left, ", incl. summary" if
                                       meta_total > meta_done else "")))
    if result["agreement"] is not None:
        tiles.append(("Agreement", C.fmt(result["agreement"]),
                      C.agreement_label(result["agreement"])))
    if tps:
        tiles.append(("Median speed", "%.0f" % sorted(tps)[len(tps) // 2], "tok/s"))

    out = ['<div class="tiles">']
    for k, v, h in tiles:
        cls = "v sm" if len(str(v)) > 12 else "v"
        out.append('<div class="tile"><div class="k">%s</div><div class="%s">%s</div>'
                   '<div class="h">%s</div></div>' % (esc(k), cls, esc(v), esc(h)))
    out.append("</div>")
    return "\n".join(out)


def _too_close(result):
    """The sentence that stops a 0.04 gap being read as a result.

    Says what the resampling covers as plainly as what it found: it varies the
    judges, and nothing else. Run the same prompt twice and the outputs
    themselves change, which is a larger source of movement than this and is
    not measured here -- claiming otherwise would replace one false precision
    with a better-dressed one.
    """
    tied = result.get("indistinguishable") or []
    boot = result.get("bootstrap") or {}
    rows = [r for r in result["standings"] if r["score"] is not None]
    if not rows or not boot:
        return ""
    lead = boot.get(rows[0]["label"]) or {}
    p = lead.get("p_best")
    if len(tied) > 1:
        names = ", ".join(esc(short_model(r["model"], 26)) for r in rows
                          if r["label"] in tied)
        body = ("<b>Too close to call at the top.</b> Resampling the judges "
                "leaves %d entries overlapping — %s. The leader holds first "
                "place in %s of resamples." % (len(tied), names,
                                               "%.0f%%" % (100 * p) if p else "–"))
    else:
        body = ("<b>The leader is clear of the field</b> on this panel: it "
                "holds first place in %s of judge resamples."
                % ("%.0f%%" % (100 * p) if p else "–"))
    return ('<p class="note warn">%s That is a floor, not a margin: it '
            'resamples the verdicts already on file, and re-running the judges '
            'moves the panel further than resampling them does — see the '
            'caveat below the table.</p>' % body)


def _truncation_warning(session, result):
    """Name any entry that ran out of tokens before it finished.

    Without this the standings punish an unfinished deliverable exactly as if
    the model had written badly, and nothing on the page says which it was. It
    cost a session: two entries hit the cap mid-sentence and finished last and
    second-last, which read as a quality collapse until the token counts were
    checked by hand.
    """
    cut = [r for r in session["runs"] if r.get("truncated")]
    if not cut:
        return ""
    names = ", ".join("%s (%s)" % (esc(short_model(r["model"], 26)), esc(r["mode"]))
                      for r in cut)
    return ('<p class="note warn"><b>%d entr%s stopped at the token limit '
            'mid-sentence</b> — %s. Judges grade an unfinished draft as a bad '
            'one, so treat %s placing below the rest as a budget result, not a '
            'quality result. Raise MAX_TOKENS and run again to compare them '
            'fairly.</p>'
            % (len(cut), "y" if len(cut) == 1 else "ies", names,
               "its" if len(cut) == 1 else "their"))


def _compliance_warning(session):
    """Entries that broke the brief's checkable rules, and ones that needed asking twice.

    Kept separate from the standings maths on purpose: nothing here changes a
    score. A panel cannot be relied on to notice that a draft is missing a
    paragraph -- six judges ranked one fourth while it was short 300 words of
    the source -- so the page says it instead.
    """
    runs = session.get("runs") or []
    failed = [r for r in runs if r.get("compliance_ok") is False]
    retried = [r for r in runs if r.get("compliance_retry")]
    out = []
    if failed:
        items = "; ".join(
            "%s — %s" % (esc(short_model(r["model"], 26)),
                         esc(r.get("compliance_faults") or "failed the brief"))
            for r in failed)
        out.append('<p class="note warn"><b>%d entr%s did not meet the brief</b> '
                   '— %s. These are mechanical checks, not taste: text that is '
                   'missing is missing. The standings below are unchanged, '
                   'because the panel ranked what it was given.</p>'
                   % (len(failed), "y" if len(failed) == 1 else "ies", items))
    if retried:
        out.append('<p class="note">%s %s asked a second time after failing the '
                   'check, and the corrected draft is what the panel read. No '
                   'penalty is applied — but a draft that complied first time '
                   'is not the same result as one that had to be told.</p>'
                   % (", ".join(esc(short_model(r["model"], 26)) for r in retried),
                      "was" if len(retried) == 1 else "were"))
    return "\n".join(out)


def _standings(result, session=None):
    rows = [r for r in result["standings"] if r["score"] is not None]
    if not rows:
        return ""
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
           '<h2>Panel standings</h2>',
           _truncation_warning(session, result) if session else "",
           _compliance_warning(session) if session else "",
           '<p class="note">Mean percentile across judges, self-votes removed '
           '(1.00 = best of the field, 0.00 = worst). Bars share one scale.</p>',
           _too_close(result),
           '<div class="card">']
    for r in rows:
        width = max(1.0, 100 * r["score"])
        band = r.get("band")
        title = "%s (%s) · score %s · mean rank %s · H2H %s" % (
            r["model"], r["mode"], C.fmt(r["score"]), C.fmt(r["mean_rank"], 1),
            C.fmt(100 * r["h2h"], 0) + "%" if r["h2h"] is not None else "–")
        whisker = ""
        if band and band["high"] > band["low"]:
            title += " · resampling the panel puts it between %s and %s" % (
                C.fmt(band["low"]), C.fmt(band["high"]))
            whisker = ('<div class="ci" style="left:%.1f%%;width:%.1f%%"></div>'
                       % (100 * band["low"], 100 * (band["high"] - band["low"])))
        out.append(
            '<div class="barrow" title="%s"><div class="lbl"><span class="tag">%s</span> '
            '%s <span style="color:var(--muted)">· %s</span></div>'
            '<div class="track"><div class="bar" style="width:%.1f%%"></div>%s</div>'
            '<div class="val">%s</div></div>'
            % (esc(title), esc(r["label"]), esc(short_model(r["model"], 26)),
               esc(r["mode"]), width, whisker, C.fmt(r["score"])))

    out.append('<table style="margin-top:16px"><tr><th>Out</th><th>Model</th><th>Mode</th>'
               '<th class="num">score</th><th class="num">panel range</th>'
               '<th class="num">p(1st)</th><th class="num">mean rank</th>'
               '<th class="num">best–worst</th><th class="num">H2H</th>'
               '<th class="num">votes</th></tr>')
    for r in rows:
        band = r.get("band")
        out.append('<tr><td><span class="tag">%s</span></td><td class="mono">%s</td>'
                   '<td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                   '<td class="num">%s</td><td class="num">%s</td>'
                   '<td class="num">%s–%s</td><td class="num">%s</td>'
                   '<td class="num">%d</td></tr>'
                   % (esc(r["label"]), esc(r["model"]), esc(r["mode"]),
                      C.fmt(r["score"]),
                      ("%s–%s" % (C.fmt(band["low"]), C.fmt(band["high"])))
                      if band else "–",
                      ("%.0f%%" % (100 * band["p_best"])) if band else "–",
                      C.fmt(r["mean_rank"], 1),
                      C.fmt(r["best_rank"], 0), C.fmt(r["worst_rank"], 0),
                      (C.fmt(100 * r["h2h"], 0) + "%") if r["h2h"] is not None else "–",
                      r["votes"]))
    out.append("</table>")
    if any(r.get("band") for r in rows):
        out.append('<p class="note">“Panel range” is a 90% interval from '
                   'resampling <i>these</i> verdicts with replacement, and '
                   '“p(1st)” is how often the resampled panel puts the entry '
                   'first. Read both as lower bounds. They take each verdict '
                   'as fixed, and a judge re-run at a different seed does not '
                   'write the same verdict: judging one identical set of six '
                   'outputs five times produced three different winners '
                   '(measured 2026-07-26). Nor does any of this cover '
                   'generation variance — every entry is a single sample, and '
                   'the same prompt run again produces different text. For '
                   'either, run it more than once.</p>')
    out.append("</div>")
    return "\n".join(out)


def _order_note(rankings):
    """Say whether the judges read in one order or in counterbalanced ones.

    Worth stating on the page: it is the difference between a session where an
    entry's position is a fixed advantage for the whole panel, and one where
    the advantage has been designed out. Sessions judged before 2026-07-26 are
    the former, and their reports should not imply otherwise.
    """
    counted = [r for r in rankings if r.get("positions")]
    if not counted:
        return ('<p class="note">All judges read the outputs in the same order, '
                'so where an entry sat was a fixed advantage or handicap across '
                'the whole panel.</p>')
    spread = set()
    for r in counted:
        for label, pos in r["positions"].items():
            spread.add((label, pos))
    per_label = {}
    for label, pos in spread:
        per_label.setdefault(label, set()).add(pos)
    balanced = all(len(v) >= len(counted) - 1 for v in per_label.values())
    return ('<p class="note">Each judge read the outputs in its own order, with '
            'its own entries last &mdash; %s. Position is therefore not an '
            'advantage any one entry holds.</p>'
            % ("counterbalanced so each entry sat in a different slot for every "
               "judge that counted it" if balanced else
               "shuffled per judge, though not perfectly balanced this time"))


def _heatmap(session, result, rankings):
    usable = [r for r in rankings if r["ranks"]]
    if not usable:
        return ""
    labels = [r["label"] for r in result["standings"]]
    n = len(labels)
    shorts = naming.short_names([session["key"][l]["model"] for l in labels]
                               + [j["judge"] for j in usable])
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
           '<h2>Who ranked what</h2>',
           '<p class="note">Every judge&rsquo;s placement of every output, best (dark) to '
           'worst (pale). Columns are the outputs being judged, ordered by panel consensus, '
           'so a tidy left-to-right gradient means the judges agreed; scattered colour marks '
           'a contested output. Rows are the judges &mdash; a judge&rsquo;s own outputs are '
           'outlined in its row.</p>',
           _order_note(rankings),
           '<div class="card"><table class="heat"><tr><th></th>']
    for label in labels:
        info = session["key"][label]
        thinking = info["mode"] == "thinking"
        out.append('<th class="out"><div class="rot">%s %s <span class="tag">%s</span>'
                   '</div></th>'
                   % ("&#9679;" if thinking else "&#9675;",
                      esc(naming.clip(shorts[info["model"]], 22)), esc(label)))
    out.append("</tr>")

    for entry in usable:
        out.append('<tr><th>%s</th>' % esc(naming.clip(shorts[entry["judge"]], 24)))
        for label in labels:
            rank = entry["ranks"].get(label)
            if rank is None:
                out.append('<td class="cell na" title="not ranked">–</td>')
                continue
            own = C.norm_model(entry["judge"]) == C.norm_model(
                session["key"][label]["model"])
            ring = (";box-shadow:0 0 0 2px var(--surface),0 0 0 3.5px var(--ink2)"
                    if own else "")
            out.append('<td class="cell s%d" style="position:relative%s" title="%s '
                       'ranked %s (%s) at %s%s">%s</td>'
                       % (_bin(rank, n), ring, esc(short_model(entry["judge"], 24)),
                          esc(label), esc(session["key"][label]["model"]),
                          C.fmt(rank, 0), " — its own output" if own else "",
                          C.fmt(rank, 0)))
        out.append("</tr>")
    out.append("</table>")
    out.append('<div class="legend"><span>best</span>')
    for i in range(7):
        out.append('<i class="s%d"></i>' % i)
    out.append("<span>worst</span>"
               "<span style=\"margin-left:14px\">&#9679; thinking</span>"
               "<span>&#9675; no thinking</span></div>")

    methods = sorted({r["method"] for r in usable})
    if methods != ["ranking-line"]:
        out.append('<p class="note" style="margin-top:14px">Rankings for this session were '
                   'salvaged from freeform verdict tables (%s), not from an explicit '
                   '<code>RANKING:</code> line. Spot-check anything surprising against the '
                   'verdict text.</p>' % esc(", ".join(methods)))
    out.append("</div>")
    return "\n".join(out)


def _meta_summary(session, result):
    """Round 3: the panel's own top pick reporting the computed consensus."""
    meta = session.get("meta_summary")
    if not meta:
        return ""
    top = next((r for r in result["standings"] if r["score"] is not None), None)
    by_line = ("by %s" % esc(short_model(meta["model"], 30))) if not top else (
        "by %s, the panel's own top pick" % esc(short_model(top["model"], 30)))
    if meta["error"]:
        return ("<p class=\"eyebrow\">Round 3 &middot; synthesis</p>"
               "<div class=\"round3\"><p class=\"note\">Round 3 did not complete "
               "(%s).</p></div>" % esc(meta["error"]))
    return (
        '<p class="eyebrow">Round 3 &middot; synthesis</p>'
        '<div class="round3"><div class="sub" style="margin:0">Final synthesis, %s</div>'
        '<div class="body">%s</div></div>'
        % (by_line, esc(meta["body"].strip())))


def _selfbias(result):
    rows = [r for r in result["standings"] if r["self_bias"] is not None]
    if not rows:
        return ""
    rows.sort(key=lambda r: -(r["self_bias"] or 0))
    span = max(abs(r["self_bias"]) for r in rows) or 1
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
           '<h2>Self-preference</h2>',
           '<p class="note">How far each model placed its own output from where the rest of '
           'the panel placed it, in rank positions. Right of the line, it flattered itself; '
           'left of the line, it was harder on itself than the panel was.</p>',
           '<div class="card">']
    for r in rows:
        v = r["self_bias"]
        pct = 100 * abs(v) / span
        left = ('<b style="width:%.1f%%;background:var(--neg)"></b>' % pct) if v < 0 else ""
        right = ('<b style="width:%.1f%%;background:var(--pos)"></b>' % pct) if v > 0 else ""
        out.append('<div class="dbar" title="%s ranked itself %s; panel mean %s">'
                   '<div class="lbl"><span class="tag">%s</span> %s '
                   '<span style="color:var(--muted)">· %s</span></div>'
                   '<div class="track"><div class="half l">%s</div><div class="zero"></div>'
                   '<div class="half r">%s</div></div><div class="val">%+.1f</div></div>'
                   % (esc(short_model(r["model"], 24)), C.fmt(r["self_rank"], 0),
                      C.fmt(r["mean_rank"], 1), esc(r["label"]),
                      esc(short_model(r["model"], 26)), esc(r["mode"]), left, right, v))
    out.append("</div>")
    return "\n".join(out)


def _scatter(session, result):
    pts = []
    by_label = {r["label"]: r for r in result["standings"]}
    for run in session["runs"]:
        row = by_label.get(run["label"])
        if not row or row["score"] is None or not run["tps"]:
            continue
        pts.append((run["tps"], row["score"], row, run))
    if len(pts) < 3:
        return ""

    W, H = 720, 320
    L, R, T, B = 52, 20, 14, 46
    xs = [p[0] for p in pts]
    xmin, xmax = min(xs), max(xs)
    pad = (xmax - xmin) * 0.08 or 1
    xmin, xmax = max(0, xmin - pad), xmax + pad

    def px(v):
        return L + (v - xmin) / (xmax - xmin) * (W - L - R)

    def py(v):
        return T + (1 - v) * (H - T - B)

    svg = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" '
           'aria-label="Speed against panel score">' % (W, H)]
    for g in (0, 0.25, 0.5, 0.75, 1.0):
        y = py(g)
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--grid)" '
                   'stroke-width="1"/>' % (L, y, W - R, y))
        svg.append('<text x="%d" y="%.1f" fill="var(--muted)" font-size="11" '
                   'text-anchor="end">%.2f</text>' % (L - 8, y + 4, g))
    svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--axis)" '
               'stroke-width="1"/>' % (L, py(0), W - R, py(0)))
    for i in range(5):
        v = xmin + (xmax - xmin) * i / 4
        svg.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="11" '
                   'text-anchor="middle">%.0f</text>' % (px(v), H - 12, v))
    svg.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="11" '
               'text-anchor="middle">generation speed (tok/s)</text>'
               % ((L + W - R) / 2, H - 1))
    svg.append('<text transform="translate(13,%.1f) rotate(-90)" fill="var(--muted)" '
               'font-size="11" text-anchor="middle">panel score</text>'
               % ((T + py(0)) / 2))

    top = sorted(pts, key=lambda p: -p[1])[:3]
    for tps, score, row, run in pts:
        x, y = px(tps), py(score)
        # Filled = thinking, open ring = no thinking: identity without a second hue.
        if row["mode"] == "thinking":
            mark = ('<circle cx="%.1f" cy="%.1f" r="5.5" fill="var(--series)" '
                    'stroke="var(--surface)" stroke-width="2"/>' % (x, y))
        else:
            mark = ('<circle cx="%.1f" cy="%.1f" r="5" fill="var(--surface)" '
                    'stroke="var(--series)" stroke-width="2"/>' % (x, y))
        svg.append('<g><title>%s (%s) · %.0f tok/s · score %s</title>%s</g>'
                   % (esc(row["model"]), esc(row["mode"]), tps,
                      C.fmt(row["score"]), mark))
        if (tps, score, row, run) in top:
            # Flip the label inboard near the right edge so it can't be clipped.
            flip = x > (L + W - R) / 2
            svg.append('<text x="%.1f" y="%.1f" fill="var(--ink2)" font-size="11" '
                       'text-anchor="%s">%s</text>'
                       % (x - 9 if flip else x + 9, y + 4, "end" if flip else "start",
                          esc(short_model(row["model"], 22))))
    svg.append("</svg>")

    return ("\n".join([
        '<h2>Speed against quality</h2>',
        '<p class="note">Panel score plotted against generation speed. Filled dots are '
        'thinking runs, open dots are no-thinking. Up and to the right is free quality; '
        'the run table below carries the same numbers.</p>',
        '<div class="card">'] + svg + ["</div>"]))


def _runs_table(session):
    out = ['<p class="eyebrow">Round 1 &middot; generation</p>',
           '<h2>Runs</h2>', '<div class="card"><table>',
           '<tr><th>Out</th><th>Model</th><th>Mode</th><th class="num">tokens</th>'
           '<th class="num">tok/s</th><th class="num">elapsed</th><th>note</th></tr>']
    for r in session["runs"]:
        out.append('<tr><td><span class="tag">%s</span></td><td class="mono">%s</td>'
                   '<td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                   '<td class="num">%s</td><td>%s</td></tr>'
                   % (esc(r["label"] or "–"), esc(r["model"]), esc(r["mode"]),
                      C.fmt(r["tokens"], 0), C.fmt(r["tps"], 1),
                      (C.fmt(r["elapsed"], 0) + "s") if r["elapsed"] else "–",
                      esc(r["error"] or "")))
    out.append("</table></div>")
    return "\n".join(out)


def _judges_table(session, rankings):
    if not rankings:
        return ""
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
           '<h2>Judges</h2>',
           '<p class="note">How each verdict was read. <code>ranking-line</code> is exact; '
           'the others are recovered from a prose table and can be wrong.</p>',
           '<div class="card"><table>',
           '<tr><th>Judge</th><th>read via</th><th class="num">outputs ranked</th>'
           '<th class="num">tokens</th><th class="num">elapsed</th></tr>']
    # Every verdict, not just the ones that produced a usable ranking.
    by_file = {j["file"]: j for j in session["judges"]}
    for r in rankings:
        j = by_file.get(r["file"], {})
        out.append('<tr><td class="mono">%s</td><td>%s</td><td class="num">%d%s</td>'
                   '<td class="num">%s</td><td class="num">%s</td></tr>'
                   % (esc(r["judge"]), esc(r["method"]), len(r["ranks"]),
                      "" if (r["complete"] or not r["ranks"]) else " (partial)",
                      C.fmt(j.get("tokens"), 0),
                      (C.fmt(j.get("elapsed"), 0) + "s") if j.get("elapsed") else "–"))
    out.append("</table></div>")
    return "\n".join(out)


def _body_only(text):
    """A run's raw body has '## Thinking' then '## Output' -- show the answer.

    Delegated, because getting this wrong is silent: splitting on the first
    bare "## Output" put half a reasoning trace on the page for any model that
    named the section while planning its answer.
    """
    return session_mod.output_of(text).lstrip("\n")


def _rank_order(result):
    """-> {label: rank position} best = 0, for sorting anything by the panel's
    verdict. Unscored labels (no external vote) sort after every scored one."""
    return {r["label"]: i for i, r in enumerate(result["standings"])}


def _outputs_section(session, result):
    """Round 1's raw generations, ranked best first -- this IS the answer to
    "why is the top pick the best": read it next to Judgements below.

    The panel's own top pick is left expanded; every other entry is collapsed.
    There is no "winner" to expand for a session that wasn't blind-scored, so
    everything stays collapsed and in original order in that case.
    """
    runs = session["runs"]
    if not runs:
        return ""
    top = next((r for r in result["standings"] if r["score"] is not None), None)
    winner_label = top["label"] if top else None
    order = _rank_order(result)
    runs = sorted(runs, key=lambda r: order.get(r["label"], len(order)))
    out = ['<p class="eyebrow">Round 1 &middot; generation</p>',
          '<h2>Judged outputs</h2>',
          '<p class="note">Ranked by the panel, best first.%s</p>'
          % (" The top pick is shown open." if winner_label else ""),
          '<div class="card">']
    for r in runs:
        title = "%s%s (%s)" % (("%s — " % r["label"]) if r["label"] else "",
                               r["model"], r["mode"])
        open_attr = " open" if r["label"] and r["label"] == winner_label else ""
        out.append("<details%s><summary><span class=\"sumtext\">%s</span>%s</summary>"
                  "<pre>%s</pre></details>"
                  % (open_attr, esc(title), COPY_BTN,
                     esc(_body_only(r["body"]).strip() or r["error"] or "(empty)")))
    out.append("</div>")
    return "\n".join(out)


def _judging_documents(session):
    """-> links to what the judges were actually shown, or "".

    The page reports what the panel concluded; these two files are what it
    concluded it *from*. Blind judging means the document the judges read has
    no model names in it, so the pair is the point: the anonymous document,
    and the key that turns its tags back into models. Relative links, so they
    resolve the same whether the report is served or opened off disk.
    """
    sdir = session.get("dir")
    if not sdir:
        return ""
    links = []
    for name, label, hint in (
            ("SUMMARIZE.md", "blind", "The document the judges read: every "
             "output under a letter, shuffled, with model, mode and speed "
             "stripped out"),
            ("SUMMARIZE-KEY.md", "with model names", "The key: which letter "
             "was which model. Written for you, never shown to a judge")):
        if os.path.exists(os.path.join(sdir, name)):
            links.append('<a href="%s" title="%s">%s</a>'
                         % (esc(name), esc(hint), esc(label)))
    if not links:
        return ""
    return "What the judges were shown: %s" % " &middot; ".join(links)


def _verdicts_section(session, result=None, rankings=None):
    """Round 2's raw verdicts, most representative of the group first.

    "Representative" = judge_distance: how close that judge's own ranking sat
    to the panel's consensus order. This is deliberately NOT the rank of the
    judge's own entry (that's self-preference, a different question) -- it's
    "whose individual verdict looked most like everyone's, together".
    """
    judges = session["judges"]
    if not judges:
        return ""
    by_file = {r["file"]: r["ranks"] for r in (rankings or [])}
    distance = {j["file"]: C.judge_distance(result, by_file.get(j["file"], {}))
               for j in judges}
    judges = sorted(judges, key=lambda j: (distance[j["file"]] is None,
                                           distance[j["file"]] or 0))
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
          "<h2>Judgements</h2>",
          '<p class="note">Full text of every verdict, ordered by how closely it '
          'matched the panel&rsquo;s overall consensus (closest first).</p>']
    sources = _judging_documents(session)
    if sources:
        out.append('<p class="note">%s</p>' % sources)
    out.append('<div class="card">')
    for j in judges:
        d = distance[j["file"]]
        note = (" &middot; avg. %s ranks from consensus" % C.fmt(d, 1)
               if d is not None else "")
        out.append("<details><summary><span class=\"sumtext\">%s%s</span>%s</summary>"
                  "<pre>%s</pre></details>"
                  % (esc(j["judge"]), note, COPY_BTN,
                     esc(j["body"].strip() or j["error"] or "(empty)")))
    out.append("</div>")
    return "\n".join(out)


def _prompts(session):
    out = ['<h2>Prompt</h2><div class="card">']
    for title, text in (("System prompt", session["system_prompt"]),
                        ("User prompt", session["user_prompt"])):
        if not text.strip():
            continue
        out.append("<details><summary><span class=\"sumtext\">%s (%d characters)</span>%s"
                   "</summary><pre>%s</pre></details>"
                   % (esc(title), len(text), COPY_BTN, esc(text)))
    out.append("</div>")
    return "\n".join(out)


def render(session, result, rankings, running=False, refresh=15,
           include_prompts=True):
    """-> complete HTML document for one session."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    head = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
            '<meta name="viewport" content="width=device-width,initial-scale=1">']
    if running:
        head.append('<meta http-equiv="refresh" content="%d">' % refresh)
    head.append("<title>%s — creative-bench</title>" % esc(session["name"]))
    # Absolute path: resolves when served by `roundtable serve`/`up` (any
    # session, any nesting depth); harmlessly 404s if opened via file://,
    # same as the rebuild link below.
    head.append('<link rel="icon" type="image/png" sizes="32x32" '
               'href="/assets/favicon-32.png">')
    head.append("<style>%s</style></head><body><div class=\"wrap\">" % CSS)

    body = ["<h1>%s</h1>" % esc(session["name"])]
    bits = ["%d runs" % len(session["runs"]), "%d judges" % len(session["judges"])]
    if session["temperature"]:
        bits.append("temp %s" % session["temperature"])
    if session["seed"]:
        bits.append("seed %s" % session["seed"])
    bits.append("blind" if session["blind"] else "labelled")
    body.append('<p class="sub">%s · generated %s%s · '
                '<a href="/rebuild/%s" title="Regenerate this page — useful if '
                'Roundtable was updated since this session finished">&#8635; '
                'rebuild</a> · <a href="/?tab=new">+ new run</a></p>'
                % (esc(" · ".join(bits)), stamp,
                   " · <b>running</b>, this page reloads itself" if running else "",
                   esc(session["name"])))

    # Chronological, matching how the session actually ran: what got generated,
    # what the judges said about it, then the panel's own final word.
    # Results first: the synthesis, the outputs it's about, and the verdicts
    # that produced it, all ranked best-first. Raw performance bookkeeping
    # (tokens, tok/s, how each verdict was parsed) is real but secondary --
    # it goes at the bottom, after Prompt.
    body.append(_tiles(session, result, rankings))
    body.append(_progress(session))
    body.append(_meta_summary(session, result))
    body.append(_outputs_section(session, result))
    body.append(_standings(result, session))
    body.append(_verdicts_section(session, result, rankings))
    body.append(_heatmap(session, result, rankings))
    body.append(_selfbias(result))
    body.append(_scatter(session, result))
    body.append(_runs_table(session))
    body.append(_judges_table(session, rankings))
    if include_prompts:
        body.append(_prompts(session))

    contested = C.disagreements(result, 2)
    notes = []
    if result["agreement"] is not None:
        notes.append("Agreement measures how closely the %d judges that ranked every "
                     "output lined up (0 = unrelated, 1 = identical order): %s, %s."
                     % (result["agreement_n"], C.fmt(result["agreement"]),
                        C.agreement_label(result["agreement"])))
    if contested and contested[0]["spread"]:
        notes.append("Widest disagreement: %s, placed between %s and %s by different judges."
                     % (esc(short_model(contested[0]["model"], 40)),
                        C.fmt(contested[0]["best_rank"], 0),
                        C.fmt(contested[0]["worst_rank"], 0)))
    notes.append("Self-votes are excluded from every score, head-to-head and percentile on "
                 "this page; they appear only in the self-preference section.")
    body.append("<footer>%s<br><br>Generated by Roundtable from <code>%s</code>.</footer>"
                % (" ".join(notes), esc(session["dir"])))
    body.append("""<script>
function rtCopy(btn, ev){
  ev.preventDefault(); ev.stopPropagation();
  var pre = btn.closest('details').querySelector('pre');
  var text = pre.textContent;
  var done = function(){
    var old = btn.innerHTML;
    btn.innerHTML = '&#10003;'; btn.classList.add('copied');
    setTimeout(function(){ btn.innerHTML = old; btn.classList.remove('copied'); }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, done);
  } else {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta); done();
  }
}
</script>""")

    return "\n".join(head + body) + "\n</div></body></html>\n"


def write(path, text):
    """Atomic write: a reader mid-refresh never sees a half-written page."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path
