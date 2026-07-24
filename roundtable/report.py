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
.barrow .track{flex:1;min-width:80px}
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


def _tiles(session, result, rankings):
    runs = session["runs"]
    ok = [r for r in runs if not r["error"]]
    tps = [r["tps"] for r in ok if r["tps"]]
    tiles = []

    top = next((r for r in result["standings"] if r["score"] is not None), None)
    if top:
        tiles.append(("Panel pick", short_model(top["model"], 30),
                      "%s · score %s" % (top["mode"], C.fmt(top["score"]))))
    tiles.append(("Runs", str(len(ok)),
                  "%d failed" % (len(runs) - len(ok)) if len(ok) != len(runs) else "all completed"))
    tiles.append(("Judges", str(len(result["judges"])),
                  "%d ranked every output" % result["agreement_n"]))
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


def _standings(result):
    rows = [r for r in result["standings"] if r["score"] is not None]
    if not rows:
        return ""
    out = ['<p class="eyebrow">Round 2 &middot; blind judging</p>',
           '<h2>Panel standings</h2>',
           '<p class="note">Mean percentile across judges, self-votes removed '
           '(1.00 = best of the field, 0.00 = worst). Bars share one scale.</p>',
           '<div class="card">']
    for r in rows:
        width = max(1.0, 100 * r["score"])
        title = "%s (%s) · score %s · mean rank %s · H2H %s" % (
            r["model"], r["mode"], C.fmt(r["score"]), C.fmt(r["mean_rank"], 1),
            C.fmt(100 * r["h2h"], 0) + "%" if r["h2h"] is not None else "–")
        out.append(
            '<div class="barrow" title="%s"><div class="lbl"><span class="tag">%s</span> '
            '%s <span style="color:var(--muted)">· %s</span></div>'
            '<div class="track"><div class="bar" style="width:%.1f%%"></div></div>'
            '<div class="val">%s</div></div>'
            % (esc(title), esc(r["label"]), esc(short_model(r["model"], 26)),
               esc(r["mode"]), width, C.fmt(r["score"])))

    out.append('<table style="margin-top:16px"><tr><th>Out</th><th>Model</th><th>Mode</th>'
               '<th class="num">score</th><th class="num">mean rank</th>'
               '<th class="num">best–worst</th><th class="num">H2H</th>'
               '<th class="num">votes</th></tr>')
    for r in rows:
        out.append('<tr><td><span class="tag">%s</span></td><td class="mono">%s</td>'
                   '<td>%s</td><td class="num">%s</td><td class="num">%s</td>'
                   '<td class="num">%s–%s</td><td class="num">%s</td>'
                   '<td class="num">%d</td></tr>'
                   % (esc(r["label"]), esc(r["model"]), esc(r["mode"]),
                      C.fmt(r["score"]), C.fmt(r["mean_rank"], 1),
                      C.fmt(r["best_rank"], 0), C.fmt(r["worst_rank"], 0),
                      (C.fmt(100 * r["h2h"], 0) + "%") if r["h2h"] is not None else "–",
                      r["votes"]))
    out.append("</table></div>")
    return "\n".join(out)


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
    """A run's raw body has '## Thinking' then '## Output' -- show the answer."""
    parts = text.split("## Output", 1)
    return parts[1].lstrip("\n") if len(parts) > 1 else text


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
          'matched the panel&rsquo;s overall consensus (closest first).</p>',
          '<div class="card">']
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
                'rebuild</a> · <a href="/new">+ new run</a></p>'
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
    body.append(_meta_summary(session, result))
    body.append(_outputs_section(session, result))
    body.append(_standings(result))
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
