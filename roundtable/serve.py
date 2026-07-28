"""A small HTTP server for browsing sessions and watching a run.

Deliberately boring: stdlib only, no framework, no session state, no streaming.
It serves files the worker has already written and an index of the sessions on
disk. Live progress needs nothing from the server, because a live report
refreshes itself.

Binds to localhost by default. Serving is read-only and confined to the sessions
root -- see ``_resolve``, which is the one security-relevant function in here.
"""
import errno
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import benchmarks, chain as chain_mod, consensus, ranks
from . import models as models_mod
from . import model_cards as model_cards_mod
from . import presets as presets_mod
from . import report, session as session_mod, spool

class AlreadyRunning(RuntimeError):
    """The port is taken, almost always by another copy of this app."""


DEFAULT_PORT = 8420
DEFAULT_HOST = "127.0.0.1"

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

FAVICON_HEAD = (
    '<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">'
    '<link rel="icon" type="image/png" sizes="16x16" href="/assets/favicon-16.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/assets/favicon-180.png">'
)
LOGO_HTML = ('<img src="/assets/logo.png" alt="Roundtable" class="logo">')

INDEX_CSS = report.CSS + """
.logo{display:block;max-width:420px;width:100%;height:auto;margin:0 0 6px}
.row{display:flex;align-items:baseline;gap:12px;padding:9px 0;
  border-bottom:1px solid var(--grid)}
.row:last-child{border-bottom:none}
.row a{font-weight:600;text-decoration:none}
.row a:hover{text-decoration:underline}
.row .meta{color:var(--muted);font-size:13px;margin-left:auto;
  font-variant-numeric:tabular-nums}
.row .acts{display:flex;align-items:center;gap:10px;flex:none}
.row .acts form{margin:0}
/* The two rerun controls are the same action as "New run" at a smaller size,
   so they carry the same filled accent rather than reading as secondary. */
.row .acts button,.row .acts a.go{font-size:12.5px;font-weight:600;
  padding:6px 12px;border-radius:7px;border:1px solid transparent;
  background:var(--series);color:#fff;text-decoration:none;line-height:1.35}
.row .acts button:hover,.row .acts a.go:hover{filter:brightness(1.08);
  text-decoration:none;color:#fff}
/* Removal carries the same weight as the rerun controls, in red: it acts on
   the first click, so it should not look like something that opens a page. */
.row .acts button.del{font-size:12.5px;font-weight:600;padding:6px 12px;
  border-radius:7px;border:1px solid transparent;background:var(--pos);
  color:#fff;line-height:1.35}
.row .acts button.del:hover{filter:brightness(1.08);color:#fff}
a.quiet{color:var(--ink2);font-size:14px;margin-left:14px}
/* The trash button, on the index and on the trash page itself. */
.trash{display:inline-block;background:var(--pos);color:#fff;font-weight:600;
  font-size:14.5px;text-decoration:none;padding:11px 22px;border-radius:8px;
  border:1px solid transparent;margin-left:12px;line-height:1.35;
  font-family:inherit}
.trash:hover{filter:brightness(1.08);color:#fff;text-decoration:none}
@media (max-width:640px){
  .row{flex-wrap:wrap}
  .row .meta{margin-left:auto}
  .row .acts{width:100%;justify-content:flex-end}
}
.pill{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
  border:1px solid var(--border);border-radius:999px;padding:1px 8px;
  color:var(--ink2)}
.live{border-color:var(--pos);color:var(--pos)}
.stale{border-color:var(--neg);color:var(--neg)}
button.danger{font:inherit;font-size:14px;font-weight:600;padding:9px 18px;
  border-radius:8px;border:1px solid var(--pos);background:none;color:var(--pos)}
button.danger:hover{background:var(--pos);color:#fff;filter:none}
label{display:block;font-weight:600;font-size:13.5px;margin:0 0 5px}
.field{margin:0 0 20px}
.hint{color:var(--muted);font-size:12.5px;margin:5px 0 0;font-weight:400}
select,input[type=text],input[type=number],textarea{width:100%;font:inherit;
  font-size:14px;color:var(--ink);background:var(--page);
  border:1px solid var(--axis);border-radius:8px;padding:9px 10px}
textarea{font-size:13.5px;line-height:1.5;resize:vertical;min-height:90px}
select:focus,input:focus,textarea:focus{outline:2px solid var(--series);
  outline-offset:-1px;border-color:var(--series)}
.grid{display:flex;gap:14px;flex-wrap:wrap}
.grid>div{flex:1;min-width:160px}
.checks{display:grid;grid-template-columns:1fr;gap:2px;
  max-height:290px;overflow-y:auto;border:1px solid var(--border);
  border-radius:8px;padding:8px 10px}
.check{display:flex;align-items:center;gap:9px;padding:4px 2px;font-size:13.5px;
  font-weight:400}
.check input{width:auto;margin:0}
.check .sz{margin-left:auto;color:var(--muted);font-size:12.5px;
  font-variant-numeric:tabular-nums}
.check code{font-size:12.5px}
.models .check{display:grid;grid-template-columns:100px 100px 1fr auto;gap:9px}
.models .check.head{color:var(--muted);font-size:11px;
  padding-bottom:6px;border-bottom:1px solid var(--grid);margin-bottom:2px}
.models .check.head span:nth-child(1),.models .check.head span:nth-child(2){
  text-align:center;line-height:1.25}
.models .check.head span:nth-child(4){text-align:right}
.models .check input{justify-self:center}
button{font:inherit;font-size:14.5px;font-weight:600;color:#fff;
  background:var(--series);border:none;border-radius:8px;padding:11px 22px;
  cursor:pointer}
button:hover{filter:brightness(1.08)}
button.ghost{background:none;color:var(--series);border:1px solid var(--border);
  font-weight:500}
.actions{display:flex;gap:10px;align-items:center;margin-top:6px}
.err{border-left:3px solid var(--pos);padding-left:12px;margin:0 0 18px;
  color:var(--ink2);font-size:13.5px}
.notice{border-left:3px solid var(--series);padding-left:12px;margin:0 0 18px;
  color:var(--ink2);font-size:13.5px}
.preset-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin-top:-6px}
.preset-actions input[type=text]{flex:1;min-width:160px;padding:7px 9px;
  font-size:13px}
.preset-actions button{font-size:13px;font-weight:500;padding:7px 12px;
  border-radius:7px;border:1px solid var(--border);background:none;
  color:var(--ink2);cursor:pointer}
.preset-actions button:hover{border-color:var(--series);color:var(--series)}
.preset-actions #delete_preset:hover{border-color:var(--pos);color:var(--pos)}
.preset-actions a{font-size:12.5px;color:var(--muted);text-decoration:none;
  margin-left:auto}
.preset-actions a:hover{color:var(--series)}
.preset-actions .hint{width:100%;margin:0}
.mc-section{border-top:1px solid var(--grid);padding-top:18px;margin-top:4px}
.mc-card{border:1px solid var(--border);border-radius:8px;padding:12px 14px;
  margin:0 0 12px}
.mc-card:last-child{margin-bottom:0}
.mc-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.mc-head b{font-size:14.5px}
.mc-head .hint{margin:0}
.mc-hist{font-size:12.5px;color:var(--muted);margin:4px 0 0}
.mc-profiles{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 0}
.mc-profile{flex:1;min-width:260px}
.mc-profile>div{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);margin:0 0 6px}
.mc-fields{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.mc-fields label{display:block;font-size:11px;font-weight:400;color:var(--muted);
  margin:0 0 3px}
.mc-fields input{padding:6px 7px;font-size:13px}
/* The three tabs. Underlined rather than boxed: they sit directly on top of
   the cards below them, and a second box around a box reads as a dialog. */
.tabs{display:flex;gap:4px;margin:0 0 20px;
  border-bottom:1px solid var(--border)}
.tabs .tab{font:inherit;font-size:14.5px;font-weight:600;background:none;
  color:var(--ink2);border:none;border-bottom:2px solid transparent;
  border-radius:0;padding:10px 16px;margin-bottom:-1px;cursor:pointer}
.tabs .tab:hover{color:var(--series);filter:none}
.tabs .tab.on{color:var(--series);border-bottom-color:var(--series)}
.tabs .tabn{display:inline-block;margin-left:7px;font-size:11.5px;
  font-weight:600;color:var(--ink2);background:var(--grid);border-radius:999px;
  padding:1px 7px;font-variant-numeric:tabular-nums}
.tabs .tab.on .tabn{color:#fff;background:var(--series)}
.panel.off{display:none}
.trash-line{margin:18px 0 0}
.trash-line .trash{margin-left:0}
@media (max-width:640px){
  .tabs .tab{flex:1;padding:10px 6px;font-size:13.5px}
}
"""


def _resolve(root, rel):
    """Map a URL path to a file inside root, or None if it escapes.

    Everything served goes through here. ``..`` segments, absolute paths and
    symlinks that point outside the root all resolve to None rather than to a
    file, so a URL can never read outside the sessions directory.
    """
    rel = urllib.parse.unquote(rel).lstrip("/")
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def _report_is_live(path, files):
    if "report.html" not in files:
        return False
    try:
        with open(os.path.join(path, "report.html"), encoding="utf-8") as f:
            return 'http-equiv="refresh"' in f.read(4000)
    except OSError:
        return False


def _chain_session_row(path, name, files):
    """A chain directory, in the same row shape ``list_sessions`` yields for
    a plain session -- chain.json + report.html is the same "session" shape
    with more rounds inside it, so it belongs in the same list rather than a
    separate one. -> row dict, or None if chain.json can't be read.
    """
    try:
        with open(os.path.join(path, "chain.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None
    stages = manifest.get("stages", [])
    planned = manifest.get("planned") or [s["name"] for s in stages]
    return {
        "name": name, "path": path, "report": "report.html" in files,
        "runs": len(stages), "stage_total": len(planned),
        "judges": sum(1 for s in stages if s.get("scored")),
        "live": _report_is_live(path, files), "chain": True,
        "mtime": os.path.getmtime(os.path.join(path, "chain.json")),
    }


def list_sessions(root):
    """-> [{name, path, report, runs, judges, live, mtime, chain}] newest
    first. Chain runs are included here too (``chain: True``), not only in
    a separate listing -- see ``_chain_session_row``.
    """
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        # .trash holds removed sessions, which are still complete session
        # directories -- without this they would list straight back in.
        if not os.path.isdir(path) or name.startswith("."):
            continue
        try:
            files = os.listdir(path)
        except OSError:
            continue
        if "chain.json" in files:
            row = _chain_session_row(path, name, files)
            if row:
                out.append(row)
            continue
        # session._is_result, not a looser filter of its own: it also excludes
        # round3_*.md, which is the synthesis, not a seventh output run. The row
        # said "7 runs" next to a report saying "6/6" until this was shared.
        results = [f for f in files if session_mod._is_result(f)]
        if not results:
            continue
        out.append({
            "name": name, "path": path, "report": "report.html" in files,
            "runs": len(results),
            "judges": len([f for f in files if f.startswith("summary_")]),
            "live": _report_is_live(path, files), "chain": False,
            "mtime": os.path.getmtime(path),
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def list_chains(root):
    """-> [{name, path, stages, total, scored, stopped, mtime}] newest first.

    A chain writes its own manifest (chain.json) after every stage, so this
    is read straight off disk rather than recomputed -- the same "reports are
    static files" rule everything else here follows.
    """
    out = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return out
    for name in names:
        path = os.path.join(root, name)
        manifest_path = os.path.join(path, "chain.json")
        if not (name.startswith("chain-") and os.path.isfile(manifest_path)):
            continue
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({
            "name": name, "path": path,
            "stages": [s["name"] for s in manifest.get("stages", [])],
            "scored": sum(1 for s in manifest.get("stages", []) if s.get("scored")),
            "stopped": bool(manifest.get("stopped")),
            "finished": bool(manifest.get("finished")),
            "mtime": os.path.getmtime(manifest_path),
        })
    return out


def _job_card(jobs):
    """The "in flight" card: what is queued or running, and how to stop it."""
    parts = []
    if not jobs:
        return ""
    parts.append('<div class="card">')
    for job in jobs:
        if job["stale"]:
            pill = '<span class="pill stale">stopped</span>'
            what = ("claimed but no worker is running it — the machine or the "
                    "worker went down mid-run")
            button = "Clear"
        elif job["state"] == "running":
            pill = '<span class="pill live">running</span>'
            what = "in flight now"
            button = "Cancel"
        else:
            pill = '<span class="pill">queued</span>'
            what = "waiting for the worker"
            button = "Cancel"
        link = ""
        if job["session"]:
            name = os.path.basename(str(job["session"]).rstrip("/"))
            link = (' · <a href="/s/%s/report.html">%s</a>'
                    % (urllib.parse.quote(name), html.escape(name)))
        parts.append(
            '<div class="row"><a href="/job/%s">%s</a>%s'
            '<span class="meta">%s%s</span>'
            '<span class="acts"><form method="post" action="/cancel">'
            '<input type="hidden" name="job" value="%s">'
            '<button type="submit">%s</button></form></span></div>'
            % (urllib.parse.quote(job["id"]), html.escape(job["id"]), pill,
               html.escape(what), link, html.escape(job["id"]), button))
    parts.append("</div>")
    return "\n".join(parts)


TRASH = ".trash"


def trash_session(root, name, sp=None):
    """Move one session out of the listing. -> (ok, message).

    Moved, not deleted. A session is a few hundred kilobytes and cost a
    quarter-hour of GPU to produce; making the button irreversible to save a
    megabyte would be a bad trade. It lands in <root>/.trash/, which the
    listing skips, and can be moved back by hand.
    """
    name = _safe_name(name)
    target = _resolve(root, name) if name else None
    if target is None or not os.path.isdir(target):
        return False, "No such session."

    beat = spool.read_heartbeat(sp) or {}
    live = os.path.basename(str(beat.get("session", "")).rstrip("/"))
    if beat.get("state") == "running" and live == name:
        return False, ("That session is being written right now — cancel the "
                       "job first.")

    trash = os.path.join(os.path.realpath(root), TRASH)
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, name)
    if os.path.exists(dest):                  # same name binned twice
        dest = "%s.%s" % (dest, time.strftime("%H%M%S"))
    try:
        shutil.move(target, dest)
    except OSError as exc:
        return False, "Could not remove it: %s" % exc
    return True, "Moved %s to %s/ — still on disk if you want it back." % (name, TRASH)


def trash_list(root):
    """What's in the bin. -> [{name, mtime, runs}] newest first."""
    trash = os.path.join(os.path.realpath(root), TRASH)
    out = []
    try:
        names = os.listdir(trash)
    except OSError:
        return out
    for name in names:
        path = os.path.join(trash, name)
        if not os.path.isdir(path):
            continue
        try:
            files = os.listdir(path)
        except OSError:
            files = []
        out.append({"name": name, "path": path,
                    "runs": len([f for f in files if session_mod._is_result(f)]),
                    "mtime": os.path.getmtime(path)})
    out.sort(key=lambda r: -r["mtime"])
    return out


def empty_trash(root):
    """Delete the bin's contents for good. -> (count, message).

    The one irreversible action in the app, so it is confined to <root>/.trash
    by realpath and refuses anything that resolves outside it -- a session
    directory is user-named, and this is the code path where that matters.
    """
    trash = os.path.join(os.path.realpath(root), TRASH)
    removed = 0
    for row in trash_list(root):
        target = os.path.realpath(row["path"])
        if target != trash and target.startswith(trash + os.sep):
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    try:
        os.rmdir(trash)                       # tidy up if it came out empty
    except OSError:
        pass
    return removed, ("Deleted %d session(s) for good." % removed if removed
                     else "The trash was already empty.")


def render_rejudge(root, name, models_root=None, error=None):
    """Pick a new panel for a session that has already been judged.

    A page of its own rather than a tab: it acts on one session, and the New
    run tab is about starting a contest, not re-scoring a finished one.
    """
    name = _safe_name(name)
    sdir = os.path.join(root, name) if name else None
    if not sdir or not os.path.isdir(sdir):
        return None
    data = session_mod.load(sdir)
    if not data:
        return None

    ran = {r["model"] for r in data["runs"]}
    judged = [j["judge"] for j in data["judges"]]

    body = ["<h1>Re-judge</h1>",
            '<p class="sub">%s · %d output(s), %d judge(s) now. '
            '<a href="/">All sessions</a></p>'
            % (html.escape(name), len(data["runs"]), len(judged))]
    if error:
        body.append('<div class="err">%s</div>' % html.escape(error))
    body.append('<form method="post" action="/rejudge">'
                '<input type="hidden" name="session" value="%s">'
                '<div class="card">' % html.escape(name))
    body.append('<p class="note">Round 2 again over the same outputs, with a '
                'panel you choose. Nothing is generated a second time — the '
                'judges read what is already here. A panel drawn from the '
                'field is a field grading its own relatives; naming outsiders '
                'is how you find out whether the standings survive it.</p>')
    if judged:
        body.append('<p class="hint">Judged so far by: %s. Those verdicts move '
                    'into <code>.judges-N/</code> inside the session when the '
                    'new panel runs — kept, but out of the scoring.</p>'
                    % html.escape(", ".join(report.short_model(j, 34)
                                            for j in judged)))

    found = models_mod.discover(models_root)
    body.append('<div class="field"><label>Judges</label>')
    if not found:
        body.append('<p class="hint">No .gguf files found under <code>%s</code>.'
                    '</p>' % html.escape(models_root or models_mod.DEFAULT_ROOT))
    else:
        body.append('<div class="checks">')
        for m in found:
            # Both sides are the name naming.py derives from the GGUF path --
            # the runs' frontmatter and the discovery listing agree by
            # construction, so this is an equality test, not a fuzzy match.
            competed = m["name"] in ran
            body.append('<label class="check"><input type="checkbox" '
                        'name="judges" value="%s"><code>%s</code>%s'
                        '<span class="sz">%.1f GB</span></label>'
                        % (html.escape(m["name"]), html.escape(m["name"]),
                           '<span class="pill">ran here</span>' if competed else "",
                           m["size_gb"]))
        body.append("</div>")
        body.append('<p class="hint">Each one loads once and ranks every output. '
                    'A judge that competed here can see its own entry — blind, '
                    'but its own prose.</p>')
    body.append("</div>")

    body.append('<div class="field"><label class="check">'
                '<input type="checkbox" name="meta_summary" value="1" checked> '
                'Round 3 &mdash; have the new panel&rsquo;s top pick write a '
                'final synthesis</label><p class="hint">The old synthesis, if '
                'there is one, is archived with the old verdicts: it summarised '
                'standings that no longer hold.</p></div>')

    body.append('<div class="actions">'
                '<button type="submit">Queue re-judge</button>'
                '<a class="quiet" href="/">Cancel</a></div>')
    body.append("</div></form>")
    return _page("Re-judge %s — Roundtable" % name, "\n".join(body))


def render_trash(root):
    """The bin, and the one button in the app that destroys anything."""
    rows = trash_list(root)
    body = ["<h1>Trash</h1>"]
    if not rows:
        body += ['<div class="card"><p class="note">Nothing in the trash.</p></div>',
                 '<p class="note"><a href="/">All sessions</a></p>']
        return _page("Trash — Roundtable", "\n".join(body))
    body.append('<p class="sub">%d removed session(s), still on disk in %s/</p>'
                % (len(rows), html.escape(TRASH)))
    body.append('<div class="card">')
    for r in rows:
        body.append('<div class="row"><span>%s</span>'
                    '<span class="meta">%d runs · %s</span></div>'
                    % (html.escape(r["name"]), r["runs"],
                       time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))))
    body.append("</div>")
    body.append('<div class="card"><p class="note">Emptying the trash deletes '
                'these for good — there is no second copy. To keep one, move it '
                'back out of <code>%s/</code> first.</p></div>' % html.escape(TRASH))
    body.append('<p><form method="post" action="/trash/empty" style="display:inline">'
                '<button type="submit" class="trash" style="margin-left:0">'
                'Empty trash (%d)</button>'
                '</form> <a href="/" class="quiet">Leave it</a></p>' % len(rows))
    return _page("Trash — Roundtable", "\n".join(body))


def render_index(root, sp=None, notice=None, tab=None, form_values=None,
                 form_error=None, models_root=None, chain_error=None,
                 chain_values=None):
    """The whole app in four tabs: Results, New run, Multi-Prompt, Queue.

    One page, four panels, rendered together and switched client-side. The
    form is not a separate page any more: queuing a run is the main thing this
    app does, and it used to cost a navigation away from the results.
    """
    counts = spool.counts(sp)
    beat = spool.read_heartbeat(sp) or {}
    rows = list_sessions(root)
    jobs = active_jobs(sp)
    chains = list_chains(root)
    # A session is live because a worker says so, never because the last report
    # written into it happens to carry a refresh tag -- that tag outlives the
    # run that wrote it, and a session killed mid-run keeps it forever.
    live_dirs = {os.path.basename(str(j["session"]).rstrip("/"))
                 for j in jobs if j["session"] and not j["stale"]}
    if beat.get("state") == "running" and beat.get("session"):
        live_dirs.add(os.path.basename(str(beat["session"]).rstrip("/")))
    for r in rows:
        r["stalled"] = r["live"] and r["name"] not in live_dirs
        r["live"] = r["live"] and r["name"] in live_dirs

    # Stale claims are excluded deliberately: a page that reloads every 15
    # seconds forever is exactly the symptom of the bug this repairs.
    reloading = any(r["live"] for r in rows) or any(not j["stale"] for j in jobs)

    tab = tab if tab in ("results", "new", "chain", "queue") else "results"

    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">']
    # No <meta refresh>: the New run tab holds a half-typed prompt, and a
    # declarative reload would throw it away mid-sentence with no way to hold
    # it off. The timer at the foot of this page does the same job and can.
    parts.append("<title>Roundtable</title>%s<style>%s</style></head><body>"
                 "<div class=\"wrap\">" % (FAVICON_HEAD, INDEX_CSS))
    parts.append(LOGO_HTML)
    parts.append("<h1>Roundtable</h1>")

    worker_state = beat.get("state", "never started")
    bits = ["worker %s" % worker_state,
            "queue %d" % counts["queue"],
            "done %d" % counts["done"]]
    if counts["failed"]:
        bits.append("failed %d" % counts["failed"])
    parts.append('<p class="sub">%s · %d session(s) in %s</p>'
                 % (html.escape(" · ".join(bits)), len(rows), html.escape(root)))

    binned = len(trash_list(root))

    if notice:
        # Above the tabs, not inside one: a notice is usually the result of an
        # action taken on a different tab from the one now showing.
        parts.append('<div class="notice">%s</div>' % html.escape(notice))

    # The tab bar. Buttons, not links: switching tabs is not a navigation, and
    # the New run tab holds unsaved text that a real page load would discard.
    waiting = len(jobs)
    parts.append('<nav class="tabs" role="tablist">')
    for key, label, count in (("results", "Results", len(rows)),
                              ("new", "New run", None),
                              ("chain", "Multi-Prompt", len(chains) or None),
                              ("queue", "Queue", waiting)):
        parts.append('<button type="button" class="tab%s" role="tab" '
                     'data-tab="%s" aria-selected="%s">%s%s</button>'
                     % (" on" if key == tab else "", key,
                        "true" if key == tab else "false", label,
                        '<span class="tabn">%d</span>' % count if count else ""))
    parts.append("</nav>")

    parts.append('<div class="panel%s" id="tab-results" role="tabpanel">'
                 % ("" if tab == "results" else " off"))

    if not rows:
        parts.append('<div class="card"><p class="note">No sessions yet. '
                     'Start one on the <b>New run</b> tab.</p></div>')
    else:
        parts.append('<div class="card">')
        for r in rows:
            link = ("/s/%s/report.html" % urllib.parse.quote(r["name"])
                    if r["report"] else "/s/%s/" % urllib.parse.quote(r["name"]))
            if r["live"]:
                pill = '<span class="pill live">running</span>'
            elif r["stalled"]:
                # Its report still reloads itself, but nothing is feeding it.
                pill = ('<span class="pill stale" title="This run stopped part '
                        'way through — its report was last written mid-run">'
                        'stopped</span>')
            elif r["chain"]:
                pill = '<span class="pill">%d/%d stages</span>' % (r["runs"], r["stage_total"])
            else:
                pill = '<span class="pill">%d judges</span>' % r["judges"]
            name = urllib.parse.quote(r["name"])
            if r["chain"]:
                # A chain has no single system-prompt.txt/user-prompt.txt at its
                # root to rerun or rejudge from -- each stage has its own, one
                # click into the report. Only removal applies here.
                acts = ('<span class="acts">'
                        '<form method="post" action="/delete">'
                        '<input type="hidden" name="session" value="%s">'
                        '<button type="submit" class="del" title="Move this chain '
                        'to the trash folder">Remove</button></form></span>'
                        % html.escape(r["name"]))
                meta = "%d/%d stages · %s" % (r["runs"], r["stage_total"],
                                             time.strftime("%Y-%m-%d %H:%M",
                                                           time.localtime(r["mtime"])))
            else:
                # Same settings is a POST: it queues a run. Prompts only is a
                # plain link -- it just opens the form, nothing happens until
                # you submit.
                acts = ('<span class="acts">'
                        '<form method="post" action="/rerun">'
                        '<input type="hidden" name="session" value="%s">'
                        '<button type="submit" title="Queue this exact run again — '
                        'same prompts, same models, same settings">Rerun</button>'
                        '</form>'
                        '<a class="go" href="/?tab=new&amp;from=%s" title="Open the form with '
                        'these prompts filled in, so you can change the models and '
                        'settings before running">Rerun prompts only</a>'
                        # No outputs are regenerated, so this is cheap next to a
                        # rerun -- and it is the only way to ask whether the
                        # standings hold up under a panel that isn't the field.
                        '<a class="go" href="/rejudge?session=%s" title="Judge these '
                        'same outputs again with a panel you choose">Re-judge</a>'
                        # A POST, not a link: this acts on the first click, and a
                        # prefetcher following a GET must never be able to bin a
                        # session. The undo is the trash folder, not a confirm step.
                        '<form method="post" action="/delete">'
                        '<input type="hidden" name="session" value="%s">'
                        '<button type="submit" class="del" title="Move this session '
                        'to the trash folder">Remove</button></form>%s</span>'
                        % (html.escape(r["name"]), name, name, html.escape(r["name"]),
                           # Rewrites the report without the reload tag, so a run
                           # that died stops pretending it is still going.
                           ('<a href="/rebuild/%s" title="Rewrite this report as a '
                            'finished one, so it stops reloading itself">Settle'
                            '</a>' % name) if r["stalled"] else ""))
                meta = "%d runs · %s" % (r["runs"],
                                        time.strftime("%Y-%m-%d %H:%M",
                                                      time.localtime(r["mtime"])))
            parts.append('<div class="row"><a href="%s">%s</a>%s'
                         '<span class="meta">%s</span>%s</div>'
                         % (link, html.escape(r["name"]), pill, meta, acts))
        parts.append("</div>")

    if binned:
        # Only shown when there is something in it: an "Empty trash (0)" sitting
        # there permanently is a button that does nothing. It lives at the foot
        # of the results, next to the sessions it holds, rather than earning a
        # tab of its own for something done twice a year. And it opens the
        # trash page rather than emptying on click: everything else here is
        # undoable and this is not, so the confirmation page is the action.
        parts.append('<p class="trash-line"><a href="/trash" class="trash">'
                     'Empty trash (%d)</a></p>' % binned)
    parts.append("</div>")                                     # /tab-results

    parts.append('<div class="panel%s" id="tab-new" role="tabpanel">'
                 % ("" if tab == "new" else " off"))
    parts.append(form_fragment(error=form_error, values=form_values,
                               models_root=models_root))
    parts.append("</div>")                                     # /tab-new

    parts.append('<div class="panel%s" id="tab-chain" role="tabpanel">'
                 % ("" if tab == "chain" else " off"))
    cv = chain_values or {}
    parts.append(chain_fragment(error=chain_error, stage_values=cv.get("stages"),
                                chain_name=cv.get("chain_name"),
                                manuscript=cv.get("manuscript"),
                                models_root=models_root,
                                think_on=cv.get("think_on"), think_off=cv.get("think_off"),
                                extra_models=cv.get("extra_models")))
    parts.append("</div>")                                     # /tab-chain

    parts.append('<div class="panel%s" id="tab-queue" role="tabpanel">'
                 % ("" if tab == "queue" else " off"))
    card = _job_card(jobs)
    if card:
        parts.append(card)
    else:
        parts.append('<div class="card"><p class="note">Nothing queued or '
                     'running. A run you queue lands here until the worker '
                     'picks it up — they are run one at a time, oldest '
                     'first.</p></div>')
    parts.append('<p class="hint">Worker %s · %d done%s since it started.</p>'
                 % (html.escape(worker_state), counts["done"],
                    ", %d failed" % counts["failed"] if counts["failed"] else ""))
    parts.append("</div>")                                     # /tab-queue

    parts.append("<footer>Reports are static files. A running session&rsquo;s report "
                 "reloads itself until it finishes.</footer>")
    parts.append(_TABS_JS % json.dumps(15000 if reloading else 0))
    return "\n".join(parts) + "\n</div></body></html>\n"


# Tab switching, plus the reload that <meta refresh> used to do. The reload is
# held off while the New run tab is showing: it is a form, and a page that
# replaces itself every 15 seconds cannot be typed into. The tab is remembered
# in the URL so the reload -- and the browser's back button -- come back to it.
_TABS_JS = """<script>
(function () {
  var tabs = document.querySelectorAll('.tab'), reloadMs = %s;
  function show(name, push) {
    var found = false;
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      found = found || on;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      document.getElementById('tab-' + t.dataset.tab).classList.toggle('off', !on);
    });
    if (!found) { return show('results', push); }
    if (push) {
      var url = new URL(location.href);
      if (name === 'results') { url.searchParams.delete('tab'); }
      else { url.searchParams.set('tab', name); }
      // Replace, not push: the tabs are one screen, and making each click a
      // back-button step would bury the page you arrived from.
      history.replaceState({tab: name}, '', url);
    }
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () { show(t.dataset.tab, true); });
  });
  window.showTab = show;
  if (reloadMs) {
    setInterval(function () {
      var on = document.querySelector('.tab.on');
      // do not eat a draft -- neither tab's textarea should vanish mid-edit.
      if (on && (on.dataset.tab === 'new' || on.dataset.tab === 'chain')) { return; }
      location.reload();
    }, reloadMs);
  }
})();
</script>"""


def _page(title, body, refresh=None):
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">']
    if refresh:
        parts.append('<meta http-equiv="refresh" content="%s">' % refresh)
    parts.append("<title>%s</title>%s<style>%s</style></head><body>"
                 '<div class="wrap">' % (html.escape(title), FAVICON_HEAD, INDEX_CSS))
    return "\n".join(parts + [body]) + "\n</div></body></html>\n"


def _card_norm(text):
    return "".join(c for c in (text or "").lower() if c.isalnum())


def _history_for_card(card, history):
    """-> {mode: entry} of history rows whose profile text names this card.

    History is keyed by the free-text ``sampler_profile`` string written into
    each run's frontmatter (see benchmarks.py), not by card id, so this
    matches loosely: the card's title, normalised, has to appear in the
    profile text. "thinking" vs the rest sorts a two-profile card's rows
    under the right column; a card with one shared profile gets everything
    under "thinking".
    """
    needle = _card_norm(card.get("title") or card["id"])
    out = {}
    for profile, entry in history.items():
        if needle and needle in _card_norm(profile):
            mode = "thinking" if "thinking" in profile.lower() else "nothinking"
            out[mode] = entry
    return out


_FIELD_LABELS = (("temp", "Temp"), ("top_p", "Top-p"), ("top_k", "Top-k"),
                 ("min_p", "Min-p"), ("repeat", "Repeat"), ("presence", "Presence"))


def _mc_fields(card_id, mode, profile):
    parts = ['<div class="mc-fields">']
    for key, label in _FIELD_LABELS:
        value = profile.get(key)
        parts.append('<label>%s <input type="number" step="any" class="mc-field" '
                     'data-card="%s" data-mode="%s" data-key="%s" value="%s">'
                     '</label>'
                     % (label, html.escape(card_id), mode, key,
                        "" if value is None else html.escape(str(value))))
    parts.append("</div>")
    return "".join(parts)


def _mc_hist_line(entry):
    if not entry:
        return '<p class="mc-hist">No scored runs recorded yet.</p>'
    return ('<p class="mc-hist">mean percentile %.2f over %d run(s), '
           '%d session(s) &middot; last %s</p>'
           % (entry["mean_score"], entry["runs"], entry["sessions"],
              html.escape(entry["last_session"])))


def _render_model_cards(cards, history):
    if not cards:
        return ""
    parts = ['<div class="field mc-section"><label>Model sampler settings</label>'
            '<p class="hint">Per-family sampler values pulled from each '
            'model&rsquo;s HuggingFace card. Edited here, they apply to the next '
            'bench run that matches — see <code>--no-card-settings</code> to '
            'bypass them for a controlled run.</p>']
    for card in cards:
        same = card["thinking"] == card["nothinking"]
        hist = _history_for_card(card, history)
        parts.append('<div class="mc-card" data-card-id="%s" data-mirror="%s">'
                     % (html.escape(card["id"]), "1" if same else "0"))
        parts.append('<div class="mc-head"><b>%s</b><span class="hint">%s</span></div>'
                     % (html.escape(card.get("title", card["id"])),
                        html.escape(card.get("source", ""))))
        if card.get("note"):
            parts.append('<p class="hint">%s</p>' % html.escape(card["note"]))
        parts.append('<div class="mc-profiles">')
        parts.append('<div class="mc-profile"><div>%s</div>%s%s</div>'
                     % ("Sampler settings" if same else "Thinking",
                        _mc_fields(card["id"], "thinking", card["thinking"]),
                        _mc_hist_line(hist.get("thinking") or hist.get("nothinking"))))
        if not same:
            parts.append('<div class="mc-profile"><div>No thinking</div>%s%s</div>'
                         % (_mc_fields(card["id"], "nothinking", card["nothinking"]),
                            _mc_hist_line(hist.get("nothinking"))))
        parts.append("</div>")
        parts.append('<div class="preset-actions" style="margin-top:10px">'
                     '<button type="button" class="mc-save" data-card-id="%s">Save</button>'
                     '<button type="button" class="mc-reset" data-card-id="%s">'
                     'Reset to card default</button>'
                     '<span class="mc-status hint"></span></div>'
                     % (html.escape(card["id"]), html.escape(card["id"])))
        parts.append("</div>")
    parts.append("</div>")
    parts.append("""<script>
(function () {
  document.querySelectorAll('.mc-save').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.dataset.cardId;
      var cardEl = btn.closest('.mc-card');
      var thinking = {}, nothinking = {};
      cardEl.querySelectorAll('.mc-field[data-mode="thinking"]').forEach(function (inp) {
        thinking[inp.dataset.key] = inp.value === '' ? null : parseFloat(inp.value);
      });
      if (cardEl.dataset.mirror === '1') {
        nothinking = thinking;
      } else {
        cardEl.querySelectorAll('.mc-field[data-mode="nothinking"]').forEach(function (inp) {
          nothinking[inp.dataset.key] = inp.value === '' ? null : parseFloat(inp.value);
        });
      }
      var status = cardEl.querySelector('.mc-status');
      fetch('/model-cards/save', {method: 'POST', body: new URLSearchParams({
        id: id, thinking: JSON.stringify(thinking), nothinking: JSON.stringify(nothinking)
      })})
        .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
        .then(function (pair) {
          var ok = pair[0], data = pair[1];
          status.textContent = ok ? 'Saved.' : (data.error || 'Could not save.');
          status.style.color = ok ? 'var(--muted)' : 'var(--pos)';
        })
        .catch(function () {
          status.textContent = 'Could not reach the server.';
          status.style.color = 'var(--pos)';
        });
    });
  });
  document.querySelectorAll('.mc-reset').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (!confirm('Reset this card to its bundled default?')) { return; }
      fetch('/model-cards/reset', {method: 'POST',
        body: new URLSearchParams({id: btn.dataset.cardId})})
        .then(function () { location.reload(); })
        .catch(function () {
          btn.closest('.mc-card').querySelector('.mc-status').textContent =
            'Could not reach the server.';
        });
    });
  });
})();
</script>""")
    return "\n".join(parts)


def form_fragment(error=None, values=None, models_root=None, presets=None,
                  cards=None, history=None):
    """The submit form: pick a role, write a prompt, tick some models.

    A fragment, not a page: it is one of the three tabs on the index, so it
    carries no logo, no page title and no link back to the sessions list.
    """
    values = values or {}
    presets = presets if presets is not None else presets_mod.load()
    found = models_mod.discover(models_root)
    bundled_ids = presets_mod.bundled_ids()
    cards = cards if cards is not None else model_cards_mod.load()
    history = history if history is not None else benchmarks.load_history()

    def val(key, default=""):
        return html.escape(str(values.get(key, default)))

    # A preset chosen via ?preset=id (after saving, or a direct link) has no
    # system_prompt in `values` yet -- the browser never round-tripped it, so
    # fill it server-side rather than leaving the textarea blank until the
    # dropdown's onchange fires.
    selected = presets_mod.find(values.get("preset"), presets) if values.get("preset") else None
    if selected and not values.get("system_prompt"):
        values = dict(values, system_prompt=selected["system_prompt"])

    body = ['<p class="sub">Round 1: every model answers the same prompt. '
            'Round 2 (optional): they judge each other, blind. Round 3 '
            '(optional): the panel&rsquo;s top pick writes a final synthesis.</p>']
    if error:
        body.append('<div class="err">%s</div>' % html.escape(error))

    body.append('<form method="post" action="/submit"><div class="card">')

    # Role preset -> fills the system prompt, which stays editable.
    body.append('<div class="field"><label for="preset">Role</label>'
                '<select id="preset" name="preset">'
                '<option value="">Custom — write your own system prompt</option>')
    for p in presets:
        body.append('<option value="%s"%s>%s</option>'
                    % (html.escape(p["id"]),
                       " selected" if values.get("preset") == p["id"] else "",
                       html.escape(p["title"])))
    body.append('</select><p class="hint" id="expects">Choosing a role fills the '
                'system prompt below. You can edit it afterwards.</p></div>')

    body.append('<div class="field preset-actions">'
                '<input type="text" id="preset_title" placeholder="Name this role, '
                'to save it" value="%s">'
                '<button type="button" id="save_preset">Save as preset</button>'
                '<button type="button" id="delete_preset"%s>Delete preset</button>'
                '<a href="#" id="reset_presets">Reset to factory presets</a>'
                '<span id="preset_status" class="hint"></span></div>'
                % (html.escape(selected["title"] if selected else ""),
                   "" if selected else ' style="display:none"'))

    body.append('<div class="field"><label for="system_prompt">System prompt</label>'
                '<textarea id="system_prompt" name="system_prompt" rows="7" '
                'placeholder="Who the model should be. Leave empty for none.">%s'
                '</textarea></div>' % val("system_prompt"))

    body.append('<div class="field"><label for="user_prompt">Prompt</label>'
                '<textarea id="user_prompt" name="user_prompt" rows="6" required '
                'placeholder="The task every model gets.">%s</textarea>'
                '<p class="hint">This is what the models actually answer.</p>'
                '</div>' % val("user_prompt"))

    # Models -- each row picks the model AND its thinking mode at once: check
    # "on", "off", or both (= two runs). Checking neither excludes the model.
    body.append('<div class="field"><label>Models</label>')
    body.append(_model_checks_html(found, "think_on", "think_off",
                                   values.get("think_on"), values.get("think_off"),
                                   models_root))
    body.append('</div>')

    body.append('<div class="field"><label for="extra_models">Extra model '
                'patterns</label><input type="text" id="extra_models" '
                'name="extra_models" value="%s" placeholder="comma-separated '
                'substrings, e.g. Qwen3.6, Gemma4"><p class="hint">Matched as '
                'substrings against model paths (no per-model checkboxes for '
                'these — they run with thinking on). Optional.</p></div>'
                % val("extra_models"))

    body.append('<div class="grid">')
    body.append('<div class="field"><label for="max_tokens">Max tokens</label>'
                '<input type="number" id="max_tokens" name="max_tokens" min="64" '
                'step="64" value="%s"></div>' % val("max_tokens", "16384"))
    body.append('<div class="field"><label for="seed">Seed</label>'
                '<input type="text" id="seed" name="seed" value="%s" '
                'placeholder="random"><p class="hint">Shared by every run.</p>'
                "</div>" % val("seed"))
    body.append("</div>")

    body.append(_render_model_cards(cards, history))

    body.append('<div class="field"><label class="check">'
                '<input type="checkbox" id="summarize" name="summarize" value="1"%s> '
                'Round 2 &mdash; have the models judge each other, blind</label>'
                '<p class="hint">Doubles the model loads, and is what produces the '
                'standings, agreement and self-preference charts.</p></div>'
                % ("" if values and not values.get("summarize") else " checked"))
    body.append('<div class="field"><label class="check">'
                '<input type="checkbox" id="meta_summary" name="meta_summary" '
                'value="1"%s> Round 3 &mdash; have the panel&rsquo;s top pick write a '
                'final synthesis</label><p class="hint">One more model load, by '
                'whichever entry the panel rated highest. Needs Round 2.</p></div>'
                % ("" if values and not values.get("meta_summary") else " checked"))
    body.append("""<script>
(function () {
  var s = document.getElementById('summarize'), m = document.getElementById('meta_summary');
  function sync() { m.disabled = !s.checked; if (!s.checked) m.checked = false; }
  s.addEventListener('change', sync); sync();
})();
</script>""")

    # Cancel goes back to the results tab rather than loading a page: nothing
    # has been submitted, so there is nothing to navigate away from.
    body.append('<div class="actions"><button type="submit">Queue run</button>'
                '<button type="button" class="ghost" onclick="showTab('
                "'results', true)\">Cancel</button></div>")
    body.append("</div></form>")

    # The dropdown fills the system prompt and shows what the Prompt box wants.
    lookup = {p["id"]: {"title": p["title"], "system_prompt": p["system_prompt"],
                        "expects": p.get("expects", "")} for p in presets}
    body.append("<script>\nvar PRESETS = %s;\nvar BUNDLED_IDS = %s;\n"
               % (json.dumps(lookup), json.dumps(sorted(bundled_ids))))
    body.append("""
var sel = document.getElementById('preset'),
    sys = document.getElementById('system_prompt'),
    expects = document.getElementById('expects'),
    user = document.getElementById('user_prompt'),
    title = document.getElementById('preset_title'),
    delBtn = document.getElementById('delete_preset'),
    saveBtn = document.getElementById('save_preset'),
    status = document.getElementById('preset_status'),
    resetLink = document.getElementById('reset_presets'),
    baseHint = expects.textContent, dirty = false;

function showDeleteFor(id) { delBtn.style.display = id && PRESETS[id] ? '' : 'none'; }

sel.addEventListener('change', function () {
  var p = PRESETS[sel.value];
  if (!p) { expects.textContent = baseHint; title.value = ''; showDeleteFor(null); return; }
  if (sys.value.trim() && dirty &&
      !confirm('Replace the system prompt you have written?')) { return; }
  sys.value = p.system_prompt;
  dirty = false;
  title.value = p.title;
  expects.textContent = p.expects || baseHint;
  if (p.expects) { user.placeholder = p.expects; }
  showDeleteFor(sel.value);
});
sys.addEventListener('input', function () { dirty = true; });

function say(msg, isError) {
  status.textContent = msg;
  status.style.color = isError ? 'var(--pos)' : 'var(--muted)';
}

saveBtn.addEventListener('click', function () {
  var name = title.value.trim();
  if (!name) { say('Give it a name first.', true); title.focus(); return; }
  var id = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  if (BUNDLED_IDS.indexOf(id) !== -1 &&
      !confirm('"' + name + '" is a built-in preset. Save anyway and replace it?')) {
    return;
  }
  var body = new URLSearchParams({title: name, system_prompt: sys.value,
                                  id: sel.value,
                                  expects: user.placeholder === baseHint ? '' : user.placeholder});
  fetch('/presets/save', {method: 'POST', body: body})
    .then(function (r) { return r.json().then(function (d) { return [r.ok, d]; }); })
    .then(function (pair) {
      var ok = pair[0], data = pair[1];
      if (!ok) { say(data.error || 'Could not save.', true); return; }
      location.href = '/?tab=new&preset=' + encodeURIComponent(data.id) +
        '&notice=' + encodeURIComponent('Saved "' + data.title + '".' +
          (data.overwrote_bundled ? ' This replaces the built-in preset of the same name.' : ''));
    })
    .catch(function () { say('Could not reach the server.', true); });
});

delBtn.addEventListener('click', function () {
  if (!sel.value) { return; }
  var name = (PRESETS[sel.value] || {}).title || sel.value;
  if (!confirm('Delete "' + name + '"?' +
      (BUNDLED_IDS.indexOf(sel.value) !== -1 ? ' This restores the built-in version.' : ''))) {
    return;
  }
  fetch('/presets/delete', {method: 'POST',
    body: new URLSearchParams({id: sel.value})})
    .then(function (r) { return r.json(); })
    .then(function (data) {
      location.href = '/?tab=new&notice=' + encodeURIComponent(
        data.reverted ? 'Deleted your edit — the built-in preset is back.' : 'Deleted.');
    })
    .catch(function () { say('Could not reach the server.', true); });
});

resetLink.addEventListener('click', function (e) {
  e.preventDefault();
  if (!confirm('Discard every saved and edited preset, back to only the '
              + 'built-in ones? This cannot be undone.')) {
    return;
  }
  fetch('/presets/reset', {method: 'POST'})
    .then(function () { location.href = '/?tab=new&notice=' + encodeURIComponent(
      'All custom presets cleared — back to factory defaults.'); })
    .catch(function () { say('Could not reach the server.', true); });
});
</script>""")
    return "\n".join(body)


# The five prompts from the manuscript-editing workflow this tab was built
# for -- also in examples/manuscript-edit-chain.json as a plain chain spec.
# "Start from a template" fills a stage's fields from one of these; nothing
# forces a stage to keep them, and typing over a field is the "or write your
# own" half of that button.
STAGE_TEMPLATES = [
    {"key": "analyze", "label": "Analyze", "meta_summary": False,
     "system_prompt": "You are an expert Developmental Editor specializing in "
                      "Romance and Erotica fiction. You excel at mapping narrative "
                      "structure, heat progression, emotional-physical anchoring, "
                      "and sensory layering.",
     "user_prompt": "Analyze the provided manuscript through the lens of the "
                    "Fichtean Curve (escalating crises leading directly to climax). "
                    "Deliver a concise, actionable diagnostic report. No rewriting.\n\n"
                    "1. Fichtean Alignment: initial spark, whether rising action is "
                    "true escalating complications, where stakes plateau.\n"
                    "2. Heat & Emotional Curve: physical escalation against "
                    "emotional progression; note mismatches.\n"
                    "3. POV & Transitions: head-hopping, timeline jumps, weak "
                    "transitions.\n"
                    "4. Sensory Layering Audit: scenes lacking multi-sensory detail "
                    "during intimate beats.\n"
                    "5. One Critical Intervention: the single section needing a "
                    "complication, heat escalation, or emotional anchor before "
                    "the climax.\n\n**THE STORY TO EDIT:**\n{{MANUSCRIPT}}"},
    {"key": "plan", "label": "Editorial Plan", "meta_summary": False,
     "system_prompt": "You are an Executive Editor translating developmental "
                      "diagnostics into a precise, executable edit roadmap.",
     "user_prompt": "Convert the diagnostic below into a section-by-section "
                    "editorial plan. Prioritize high-impact structural changes, "
                    "heat pacing targets, and voice preservation rules. No rewriting.\n\n"
                    "1. Section-by-Section Beat Adjustments: Fichtean function, heat "
                    "target, emotional anchor, sensory priority per scene.\n"
                    "2. Priority Matrix: Critical / High / Low.\n"
                    "3. Bracketing & Voice Protocol.\n"
                    "4. Heat Escalation Targets per section.\n\n"
                    "**THE STORY TO EDIT:**\n{{MANUSCRIPT}}\n\n"
                    "**DIAGNOSTIC:**\n{{PREVIOUS}}"},
    {"key": "rewrite", "label": "Rewrite", "meta_summary": False,
     "system_prompt": "You are a Senior Developmental Editor executing a precise, "
                      "structure-first integration pass for Romance/Erotica fiction.",
     "user_prompt": "Rewrite the full manuscript by applying the editorial plan "
                    "below. Preserve the client's voice, escalate tension using the "
                    "Fichtean Curve, layer sensory/emotional beats, and bracket all "
                    "significant structural additions with [square brackets].\n\n"
                    "1. Preserve voice — do not alter sentences unless they break "
                    "grammar or stall pacing.\n2. Each scene raises stakes "
                    "immediately.\n3. Weave in sensory/emotional layering where the "
                    "plan specifies.\n4. [Bracket] every addition.\n5. Follow the "
                    "heat escalation targets exactly.\n\n"
                    "**THE STORY TO EDIT:**\n{{MANUSCRIPT}}\n\n"
                    "**EDITORIAL PLAN:**\n{{PREVIOUS}}"},
    {"key": "qc", "label": "Consistency / QC", "meta_summary": False,
     "system_prompt": "You are a Developmental Editor + Line Editor hybrid "
                      "auditing an edited manuscript against its editorial plan.",
     "user_prompt": "Review the integrated draft below for structural consistency, "
                    "voice drift, heat pacing adherence, sensory layering, bracket "
                    "cleanliness, and unresolved tension. Flag issues and recommend "
                    "targeted fixes. No rewriting, report only.\n\n"
                    "**ORIGINAL MANUSCRIPT:**\n{{MANUSCRIPT}}\n\n"
                    "**INTEGRATED EDITOR'S DRAFT:**\n{{PREVIOUS}}"},
    {"key": "judge", "label": "Judge", "meta_summary": True,
     "system_prompt": "You are an Acquisitions Editor & Final Gatekeeper "
                      "evaluating a romance/erotica manuscript for publication "
                      "readiness.",
     "user_prompt": "Apply a weighted rubric to each QC report and its underlying "
                    "draft, and give a final verdict per candidate.\n\n"
                    "1. Fichtean Structural Integrity (25%)\n"
                    "2. Heat & Emotional Pacing (20%)\n3. Voice Preservation (15%)\n"
                    "4. Sensory Immersion (15%)\n5. Climax Payoff (15%)\n"
                    "6. Readiness for Publication (10%)\n\n"
                    "State GREENLIGHT / YELLOW LIGHT / RED LIGHT, and list the top "
                    "3 micro-fixes that would push a YELLOW to GREEN.\n\n"
                    "**ORIGINAL MANUSCRIPT:**\n{{MANUSCRIPT}}\n\n"
                    "**QC REPORTS (one per candidate draft):**\n{{PREVIOUS}}"},
]


def _stage_defaults():
    """The starter set of stages a fresh Multi-Prompt tab loads with --
    the workflow the tab was designed around, ready to queue as-is or edit."""
    return [{"name": t["label"], "system_prompt": t["system_prompt"],
            "user_prompt": t["user_prompt"], "meta_summary": t["meta_summary"]}
           for t in STAGE_TEMPLATES]


def _stage_block_html(idx, values, first):
    """One stage's fields, under names namespaced ``stage_<idx>_*`` so a
    posted form can be regrouped by stage regardless of add/remove order.

    No per-stage models or "carry forward" picker: the roster is chosen once,
    at the top of the form, for the whole chain, and every stage after the
    first automatically continues each of those models' own previous answer
    (``use_previous: "own"``). Picking a different subset, or a shared
    top-N handoff, per stage is a real thing to want later -- not built yet.
    """
    v = values or {}

    def val(key, default=""):
        return html.escape(str(v.get(key, default)))

    tpl_opts = "".join('<option value="%s">%s</option>' % (t["key"], html.escape(t["label"]))
                       for t in STAGE_TEMPLATES)

    parts = ['<div class="card stage" data-idx="%d">' % idx]
    parts.append('<div class="grid">')
    parts.append('<div class="field"><label>Stage name</label>'
                '<input type="text" name="stage_%d_name" value="%s" '
                'placeholder="e.g. Analyze"></div>' % (idx, val("name")))
    parts.append('<div class="field"><label>Start from a template</label>'
                '<select class="stage-template-pick" data-idx="%d">'
                '<option value="">— pick one —</option>%s</select>'
                '<p class="hint">Fills this stage&rsquo;s fields below. Everything '
                'stays editable afterwards — write your own instead, or on top of '
                'it.</p></div>' % (idx, tpl_opts))
    parts.append('</div>')

    parts.append('<div class="field"><label>System prompt</label>'
                '<textarea name="stage_%d_system_prompt" rows="4" '
                'placeholder="Who the model should be for this stage.">%s'
                '</textarea></div>' % (idx, val("system_prompt")))
    parts.append('<div class="field"><label>Prompt</label>'
                '<textarea name="stage_%d_user_prompt" rows="8" '
                'placeholder="The task for this stage. Use {{MANUSCRIPT}} for the '
                'shared source text%s.">%s</textarea>%s</div>'
                % (idx, "" if first else " and {{PREVIOUS}} for what this model "
                                        "wrote last stage",
                   val("user_prompt"),
                   '' if first else '<p class="hint">Each model here works from '
                                    'its own previous answer.</p>'))

    parts.append('<div class="field"><label class="check">'
                '<input type="checkbox" name="stage_%d_meta_summary" value="1"%s> '
                'Have the top-ranked result write a final summary</label>'
                '<p class="hint">Usually only checked on the last stage.</p></div>'
                % (idx, " checked" if v.get("meta_summary") else ""))

    parts.append('<div class="actions"><button type="button" class="ghost '
                'remove-stage">Remove this stage</button></div>')
    parts.append('</div>')                                     # /card.stage
    return "\n".join(parts)


def chain_fragment(error=None, stage_values=None,
                   chain_name=None, manuscript=None, models_root=None,
                   think_on=None, think_off=None, extra_models=None):
    """The Multi-Prompt tab: run several prompts in sequence -- fields and
    checkboxes, not a JSON blob, the same shape as the single-prompt New run
    form repeated per stage.

    The model roster is picked once, at the top, for the whole chain: every
    stage after the first automatically has each of those models continue
    its own previous answer. Per-stage rosters or a shared top-N handoff are
    a real thing to want later; not built into this form yet (see
    ``roundtable chain`` / a hand-written spec for that in the meantime).
    """
    stage_values = stage_values if stage_values is not None else _stage_defaults()
    found = models_mod.discover(models_root)

    body = ['<p class="sub">Run several prompts in sequence. Pick your models '
            'once below — every stage after the first has each of them continue '
            'its own previous answer.</p>']
    if error:
        body.append('<div class="err">%s</div>' % html.escape(error))

    body.append('<form method="post" action="/chain/submit"><div class="card">')
    body.append('<div class="field"><label for="chain_name">Name</label>'
                '<input type="text" id="chain_name" name="chain_name" value="%s" '
                'placeholder="e.g. my-story"><p class="hint">Names the output '
                'folder; not shown to the models.</p></div>'
                % html.escape(chain_name or ""))
    body.append('<div class="field"><label for="chain_manuscript">Manuscript</label>'
                '<textarea id="chain_manuscript" name="chain_manuscript" rows="14" '
                'placeholder="Paste your story here.">%s</textarea>'
                '<p class="hint">Fills <code>{{MANUSCRIPT}}</code> in every stage '
                'below that uses it. Optional — leave blank if no stage needs it.</p>'
                '</div>' % html.escape(manuscript or ""))
    body.append('<div class="field"><label>Models</label>')
    body.append(_model_checks_html(found, "chain_think_on", "chain_think_off",
                                   think_on, think_off, models_root))
    body.append('</div>')
    body.append('<div class="field"><label>Extra model patterns</label>'
                '<input type="text" name="chain_extra_models" value="%s" '
                'placeholder="comma-separated substrings"></div>'
                % html.escape(extra_models or ""))
    body.append('</div>')                                      # /card

    body.append('<div id="stages">')
    for idx, values in enumerate(stage_values):
        body.append(_stage_block_html(idx, values, idx == 0))
    body.append('</div>')                                      # /stages

    next_idx = len(stage_values)
    body.append('<div class="actions"><button type="button" id="add_stage">'
                '+ Add stage</button></div>')
    body.append('<template id="stage-template">%s</template>'
               % _stage_block_html(0, {}, False).replace(
                   'data-idx="0"', 'data-idx="__IDX__"').replace(
                   'name="stage_0_', 'name="stage___IDX___').replace(
                   'stage_%d_' % 0, 'stage___IDX___'))

    body.append('<div class="actions"><button type="submit">'
                'Queue chain</button>'
                '<button type="button" class="ghost" onclick="showTab('
                "'results', true)\">Cancel</button></div>")
    body.append("</form>")

    body.append("<script>\nvar STAGE_TEMPLATES = %s;\nvar nextStageIdx = %d;\n"
               % (json.dumps({t["key"]: t for t in STAGE_TEMPLATES}), next_idx))
    body.append("""
(function () {
  var stages = document.getElementById('stages');
  var tpl = document.getElementById('stage-template');
  var addBtn = document.getElementById('add_stage');

  function wire(block) {
    var idx = block.dataset.idx;
    var removeBtn = block.querySelector('.remove-stage');
    removeBtn.addEventListener('click', function () {
      if (stages.querySelectorAll('.stage').length <= 1) {
        alert('A chain needs at least one stage.'); return;
      }
      block.remove();
    });
    var pick = block.querySelector('.stage-template-pick');
    pick.addEventListener('change', function () {
      var t = STAGE_TEMPLATES[pick.value];
      pick.value = '';
      if (!t) { return; }
      block.querySelector('[name="stage_' + idx + '_name"]').value = t.label;
      block.querySelector('[name="stage_' + idx + '_system_prompt"]').value = t.system_prompt;
      block.querySelector('[name="stage_' + idx + '_user_prompt"]').value = t.user_prompt;
      block.querySelector('[name="stage_' + idx + '_meta_summary"]').checked = !!t.meta_summary;
    });
  }

  Array.prototype.forEach.call(stages.querySelectorAll('.stage'), wire);

  addBtn.addEventListener('click', function () {
    var idx = nextStageIdx++;
    var html = tpl.innerHTML.split('__IDX__').join(idx);
    var holder = document.createElement('div');
    holder.innerHTML = html;
    var block = holder.firstElementChild;
    stages.appendChild(block);
    wire(block);
  });
})();
</script>""")

    # Past chains are not re-listed here -- they show up in the ordinary
    # Results list (list_sessions includes them), same as every plain
    # session; this tab is only for starting a new one.
    body.append('<p class="note"><a href="/">&larr; See finished runs, chains '
               "included, on the Results tab.</a></p>")

    return "\n".join(body)


def _models_from_checks(on, off, extra=""):
    """Checked model names + a mode each, plus freeform patterns. -> [pattern,...]

    Shared by the single-prompt form and each Multi-Prompt stage: a model in
    both the "on" and "off" lists runs twice (``:both``); freeform patterns
    from the extra-patterns field run with thinking on, since they have no
    per-model checkboxes of their own.
    """
    on, off = {m for m in on if m.strip()}, {m for m in off if m.strip()}
    checked = []
    for name in sorted(on | off):
        if name in on and name in off:
            checked.append("%s:both" % name)
        elif name in on:
            checked.append("%s:thinking" % name)
        else:
            checked.append("%s:nothinking" % name)
    extras = [p.strip() for p in (extra or "").split(",") if p.strip()]
    return checked + [p for p in extras if p not in on and p not in off]


def _model_checks_html(found, name_on, name_off, on_default=None, off_default=None,
                       models_root=None):
    """The two-checkbox-per-model grid (thinking on/off), as its own fragment.

    Factored out of the single-prompt form so a Multi-Prompt stage can embed
    the identical picker under its own field names -- one call per stage,
    each with a distinct ``name_on``/``name_off`` so the checked boxes land
    back under the right stage on submit.
    """
    if not found:
        return ('<p class="hint">No .gguf files found under <code>%s</code>. '
                'Set <code>ROUNDTABLE_MODELS</code>, or list patterns below.</p>'
                % html.escape(models_root or models_mod.DEFAULT_ROOT))
    on_default = set(on_default or ())
    off_default = set(off_default or ())
    has_defaults = bool(on_default or off_default)
    parts = ['<div class="checks models">'
            '<div class="check head"><span>Run w/o Think</span>'
            '<span>Run w/ Think</span><span>Model</span><span>Size</span></div>']
    for m in found:
        if has_defaults:
            on, off = m["name"] in on_default, m["name"] in off_default
        else:
            off = models_mod.thinking_off_by_default(m["name"])
            on = not off
        parts.append(
            '<label class="check"><input type="checkbox" name="%s" '
            'value="%s"%s><input type="checkbox" name="%s" value="%s"%s>'
            '<code>%s</code><span class="sz">%.1f GB</span></label>'
            % (html.escape(name_off), html.escape(m["name"]), " checked" if off else "",
               html.escape(name_on), html.escape(m["name"]), " checked" if on else "",
               html.escape(m["name"]), m["size_gb"]))
    parts.append("</div>")
    parts.append('<p class="hint">%s Defaults follow past results: thinking off '
                'for models that scored better that way, on otherwise. Check both '
                'boxes for a model to run it twice.</p>' % html.escape(models_mod.sizes_note(found)))
    return "\n".join(parts)


_STAGE_FIELD_RE = re.compile(
    r"^stage_(\d+)_(name|system_prompt|user_prompt|meta_summary)$")


def _stages_from_form(parsed):
    """A posted Multi-Prompt form -> (stage_values, spec_stages).

    ``parsed`` is ``parse_qs()``-style (every value a list). Stages are
    grouped by the index in their field names (``stage_<i>_...``), not by
    position in the form, so adding and removing stage blocks client-side
    needs no renumbering. ``stage_values`` is what redraws the form (kept on
    a validation failure); ``spec_stages`` is what goes into the chain spec.

    There is one roster for the whole chain (see ``chain_think_on`` etc. in
    ``_do_chain_submit``), not one per stage: the first stage in index order
    gets it as ``models``; every later stage gets ``use_previous: "own"`` and
    no ``models`` of its own, so each of those models continues its own
    previous answer.
    """
    matches = [_STAGE_FIELD_RE.match(k) for k in parsed]
    indices = sorted({int(m.group(1)) for m in matches if m})
    stage_values, spec_stages = [], []
    for pos, i in enumerate(indices):
        def get(field, default=""):
            return (parsed.get("stage_%d_%s" % (i, field)) or [default])[-1]
        meta_summary = bool(get("meta_summary"))
        name = get("name").strip()
        system_prompt = get("system_prompt")
        user_prompt = get("user_prompt")

        stage_values.append({"name": name, "system_prompt": system_prompt,
                            "user_prompt": user_prompt, "meta_summary": meta_summary})

        stage = {"name": name or "stage-%d" % (pos + 1), "system_prompt": system_prompt,
                "user_prompt": user_prompt}
        if meta_summary:
            stage["meta_summary"] = True
        if pos > 0:
            stage["use_previous"] = "own"
        spec_stages.append(stage)
    return stage_values, spec_stages


def job_from_form(fields, models_root=None):
    """Form fields -> (job dict, error).

    Validation lives here, not in the handler, so it is testable without a
    socket.
    """
    user_prompt = (fields.get("user_prompt") or "").strip()
    if not user_prompt:
        return None, "A prompt is required — that is what the models answer."

    mode = fields.get("mode", "thinking")
    if mode not in ("thinking", "nothinking", "both"):
        return None, "Thinking must be on, off, or both."
    patterns = _models_from_checks(fields.get("think_on", []), fields.get("think_off", []),
                                   fields.get("extra_models") or "")
    if not patterns:
        return None, "Pick at least one model, or give a pattern to match."

    # No temperature field on the page any more -- it's set per model-family
    # on the sampler cards instead. Left parseable here (and still checked
    # against 0-2) for scripted job submissions that do send one; the worker
    # only forwards --temp when this is present (see worker.build_command),
    # so leaving it out lets each run's card supply its own temperature.
    temperature = None
    temp_raw = (fields.get("temperature") or "").strip()
    if temp_raw:
        try:
            temperature = float(temp_raw)
        except ValueError:
            return None, "Temperature must be a number."
        if not 0 <= temperature <= 2:
            return None, "Temperature must be between 0 and 2."

    job = {
        "system_prompt": (fields.get("system_prompt") or "").strip(),
        "user_prompt": user_prompt,
        "mode": mode,
        "models": patterns,
        "summarize": bool(fields.get("summarize")),
        "meta_summary": bool(fields.get("summarize")) and bool(fields.get("meta_summary")),
        "blind": True,
        "preset": fields.get("preset") or None,
    }
    if temperature is not None:
        job["temperature"] = temperature
    seed = (fields.get("seed") or "").strip()
    if seed:
        if not seed.isdigit():
            return None, "Seed must be a whole number, or empty for random."
        job["seed"] = int(seed)
    max_tokens = (fields.get("max_tokens") or "").strip()
    if max_tokens:
        if not max_tokens.isdigit():
            return None, "Max tokens must be a whole number."
        job["env"] = {"MAX_TOKENS": max_tokens}
    return job, None


# Bookkeeping the worker adds to a finished job record. A rerun must not carry
# any of it: "id" and "created" would collide with the original in the spool,
# and the rest describes a run that has already happened.
_RECORD_ONLY = ("id", "created", "finished", "state", "session_dir", "exit_code",
                "log", "elapsed_sec", "meta_summary_requested")


def job_for_session(name, root, sp=None):
    """The job that would run this session again. -> (job, source) or (None, None).

    Prefers the exact job record the session came from: it holds what was asked
    for, including the parts the session dir cannot show (max tokens, the preset
    the system prompt came from, whether Round 3 was wanted at all). Falls back
    to reading the settings back off the session itself, so a session started
    from the command line -- with no job record anywhere -- reruns too.
    """
    job = _job_from_record(name, sp)
    if job:
        return job, "record"
    job = _job_from_session(os.path.join(root, name))
    return (job, "session") if job else (None, None)


def _job_from_record(name, sp=None):
    p = spool.paths(sp)
    newest = None
    for state in ("done", "failed"):
        directory = p[state]
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".json"):
                continue
            path = os.path.join(directory, entry)
            try:
                with open(path, encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            sdir = record.get("session_dir") or ""
            if os.path.basename(sdir.rstrip("/")) != name:
                continue
            if newest is None or record.get("finished", "") > newest[0]:
                newest = (record.get("finished", ""), record)
    if newest is None:
        return None
    record = newest[1]
    job = {k: v for k, v in record.items() if k not in _RECORD_ONLY}
    # "meta_summary" in a finished record is the *result* -- whether Round 3
    # actually ran -- which is False for a job that asked for it and had it
    # skipped. The request itself is what a rerun should repeat.
    if "meta_summary_requested" in record:
        job["meta_summary"] = bool(record["meta_summary_requested"])
    return job


def _job_from_session(sdir):
    """Read a job back off a session directory. -> job dict, or None.

    Everything here is recoverable from what the run left behind: the prompts
    from their own files, the queue from each result's frontmatter. Max tokens
    is not -- no file records it -- so a rerun built this way takes the default.
    """
    data = session_mod.load(sdir)
    if not data:
        return None
    modes = {}
    for run in data["runs"]:
        mode = "nothinking" if run["mode"] == "no thinking" else "thinking"
        modes.setdefault(run["model"], set()).add(mode)
    patterns = []
    for model in sorted(modes):
        found = modes[model]
        patterns.append("%s:%s" % (model, "both" if len(found) > 1
                                   else next(iter(found))))
    if not patterns:
        return None
    job = {
        "system_prompt": data["system_prompt"].strip(),
        "user_prompt": data["user_prompt"].strip(),
        "mode": "thinking",
        "models": patterns,
        "summarize": bool(data["judges"]),
        "meta_summary": bool(data["meta_summary"]),
        "blind": data["blind"],
    }
    if str(data["seed"]).strip().isdigit():
        job["seed"] = int(data["seed"])
    return job


def _safe_name(name):
    """Session names come from the URL: keep them to a single directory entry."""
    name = (name or "").strip().strip("/")
    return name if name and "/" not in name and not name.startswith(".") else ""


def _prompts_only(name, root, sp=None):
    """Form values for "rerun prompts only". -> (values, notice) or (None, _).

    Deliberately carries the prompts and nothing else: models, thinking modes
    and rounds stay at the page's own defaults, which is the point of the button
    -- you came here to change them.
    """
    name = _safe_name(name)
    if not name or not os.path.isdir(os.path.join(root, name)):
        return None, None
    job, _source = job_for_session(name, root, sp)
    if job is None:
        return None, None
    values = {"system_prompt": job.get("system_prompt", ""),
              "user_prompt": job.get("user_prompt", "")}
    if job.get("preset"):
        values["preset"] = job["preset"]
    return values, ("Prompts from %s. Models and settings are back at their "
                    "defaults — set them below." % name)


def find_job_session(job_id, sp=None):
    """Where has this job got to? -> (state, session_dir or None)."""
    p = spool.paths(sp)
    for state in ("done", "failed"):
        path = os.path.join(p[state], job_id + ".json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return state, json.load(f).get("session_dir")
            except (OSError, ValueError):
                return state, None
    claim = os.path.join(p["running"], job_id + ".json")
    if os.path.exists(claim):
        # Claimed, but by whom? A claim no live worker owns is not running --
        # calling it "running" is what made a rebooted run look alive for good.
        session_dir = spool.read_job(claim).get("session_dir")
        if claim in spool.orphans(sp):
            return "stopped", session_dir
        beat = spool.read_heartbeat(sp) or {}
        if beat.get("job") == job_id:
            return "running", beat.get("session") or session_dir
        return "running", session_dir
    if os.path.exists(os.path.join(p["queue"], job_id + ".json")):
        return "queued", None
    return "unknown", None


def cancel_job(job_id, root, sp=None):
    """Stop a queued or running job. -> (ok, message).

    Queued is settled here and now: the claim never happened, so moving the file
    to failed/ is the whole operation. Running is a request instead -- only the
    worker can kill the runner it started -- unless nothing is behind the claim,
    in which case this is really a reap and there is nobody to ask.
    """
    p = spool.paths(sp)
    name = job_id + ".json"
    queued = os.path.join(p["queue"], name)
    if os.path.exists(queued):
        spool.finish(queued, "failed",
                     {"error": "cancelled", "cancelled": True}, spool=sp)
        spool.clear_cancel(sp)
        return True, "Cancelled before it started."

    running = os.path.join(p["running"], name)
    if os.path.exists(running):
        if running in spool.orphans(sp):
            clean_up(root, sp)
            return True, "Cleared: no worker was running that job."
        spool.request_cancel(job_id, sp)
        return True, ("Asked the worker to stop. It kills the model within a "
                      "few seconds; finished runs are kept.")

    for state in ("done", "failed"):
        if os.path.exists(os.path.join(p[state], name)):
            return False, "That job already finished."
    return False, "No such job."


def clean_up(root, sp=None):
    """Fail every claim with no worker behind it. -> [job id].

    The button behind this exists because the failure it repairs is invisible:
    a reboot mid-run leaves a claim that nothing else will ever touch, and the
    session it belongs to keeps reloading itself as though it were still going.
    """
    reaped = spool.reap(sp)
    for _, session_dir in reaped:
        if session_dir:
            rebuild_session(root, os.path.basename(str(session_dir).rstrip("/")), sp)
    return [job_id for job_id, _ in reaped]


def active_jobs(sp=None):
    """Queued and unfinished jobs, for the index. -> [{id, state, stale, ...}].

    ``stale`` is the whole point: a claim in running/ that no live worker owns
    is not running, however much the directory layout says it is.
    """
    orphaned = set(spool.orphans(sp))
    beat = spool.read_heartbeat(sp) or {}
    out = []
    for path, job in spool.jobs("running", sp):
        stale = path in orphaned
        out.append({
            "id": job.get("id", ""),
            "state": "stale" if stale else "running",
            "stale": stale,
            "created": job.get("created", ""),
            "session": (job.get("session_dir")
                        or (beat.get("session") if not stale else None)),
            "models": len(job.get("models") or []),
            "chain": bool(job.get("chain_spec")),
        })
    for _, job in spool.jobs("queue", sp):
        out.append({"id": job.get("id", ""), "state": "queued", "stale": False,
                    "created": job.get("created", ""), "session": None,
                    "models": len(job.get("models") or []),
                    "chain": bool(job.get("chain_spec"))})
    return out


def job_failure(job_id, sp=None):
    """Why a job failed, in words. -> str.

    "See the job log under the spool directory" was the old answer: the app
    knows the exit code, holds the log, and can read the last error out of it,
    and instead sent the reader off to find a path it didn't even name. If this
    can't identify the cause it says so and quotes the tail, which is still
    strictly more than a filename.
    """
    p = spool.paths(sp)
    record = spool.read_job(os.path.join(p["failed"], job_id + ".json"))
    reason = str(record.get("error") or "The run did not finish.")
    if record.get("cancelled"):
        return "Cancelled before it finished. Whatever had completed is kept."
    if record.get("reaped"):
        return reason

    log_path = record.get("log") or os.path.join(p["logs"], job_id + ".log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f.read().splitlines() if l.strip()]
    except OSError:
        return "%s No log was written — the runner may not have started." % reason

    # A traceback's last line, or the last line the script complained about:
    # the one line worth putting in front of someone.
    cause = ""
    for line in reversed(lines):
        if (line.startswith(("Traceback", "  File ")) or line.startswith("    ")):
            continue
        if any(k in line for k in ("Error", "error:", "✗", "No such file",
                                   "not found", "Permission denied", "refus")):
            cause = line
            break
    tail = " · ".join(lines[-3:])[:400]
    if cause:
        return "%s\n\n%s\n\nLast lines: %s" % (reason, cause, tail)
    return "%s\n\nLast lines: %s" % (reason, tail)


def render_job(job_id, root, sp=None):
    """The waiting page: redirects itself to the report once one exists."""
    state, session_dir = find_job_session(job_id, sp)
    # A stopped job is the exception: its report still has the reload tag from
    # the last write before the worker died, so sending someone straight to it
    # hides the very thing they came here to deal with.
    if session_dir and state != "stopped":
        name = os.path.basename(session_dir.rstrip("/"))
        target = "/s/%s/report.html" % urllib.parse.quote(name)
        if os.path.exists(os.path.join(root, name, "report.html")):
            return None, target             # hand over to the report itself
    beat = spool.read_heartbeat(sp) or {}
    counts = spool.counts(sp)

    if state == "queued":
        headline, detail = "Queued", (
            "%d job(s) ahead of this one. The worker takes them in order."
            % max(0, counts["queue"] - 1))
        if beat.get("state") in (None, "stopped", "never started"):
            detail += (" The worker does not look like it is running — start it "
                       "with roundtable work.")
    elif state == "running":
        headline, detail = "Running", (
            "Loading the first model. This page will switch to the live report "
            "as soon as the first result lands.")
    elif state == "stopped":
        headline, detail = "Stopped", (
            "This job is still claimed, but no worker is running it — the "
            "machine or the worker went down mid-run. Clearing it files the job "
            "as failed and settles its report; whatever finished is kept.")
    elif state == "failed":
        headline, detail = "Failed", job_failure(job_id, sp)
    elif state == "done":
        headline, detail = "Finished", "No report was produced for this run."
    else:
        headline, detail = "Unknown job", "Nothing in the queue matches that id."

    body = ["<h1>%s</h1>" % html.escape(headline),
            '<p class="sub">job %s · worker %s</p>'
            % (html.escape(job_id), html.escape(str(beat.get("state", "unknown")))),
            '<div class="card"><p class="note" style="white-space:pre-wrap">%s'
            '</p></div>' % html.escape(detail)]
    if state in ("queued", "running", "stopped"):
        body.append(
            '<p><form method="post" action="/cancel" style="margin:0 0 16px">'
            '<input type="hidden" name="job" value="%s">'
            '<input type="hidden" name="back" value="job">'
            '<button type="submit" class="danger">%s</button></form></p>'
            % (html.escape(job_id),
               "Clear this job" if state == "stopped" else "Cancel this run"))
    if session_dir:
        name = os.path.basename(session_dir.rstrip("/"))
        if os.path.exists(os.path.join(root, name, "report.html")):
            body.append('<p class="note">Results so far: '
                        '<a href="/s/%s/report.html">%s</a></p>'
                        % (urllib.parse.quote(name), html.escape(name)))
    body.append('<p class="note"><a href="/">All sessions</a> · '
                '<a href="/?tab=new">Queue another</a></p>')
    refresh = "3" if state in ("queued", "running") else None
    return _page("%s — Roundtable" % headline, "\n".join(body), refresh), None


def rebuild_session(root, name, sp=None):
    """Regenerate one session's report.html on demand. -> True if it existed.

    For a session the worker has already finished with (no active job), this
    is the only way its report ever reflects a newer Roundtable version --
    the worker only rewrites a report while it owns that job, restarting it
    doesn't retroactively touch old sessions.
    """
    target = _resolve(root, name)
    if target is None or not os.path.isdir(target):
        return False
    data = session_mod.load(target)
    if not data:
        return False
    beat = spool.read_heartbeat(sp) or {}
    running = (beat.get("state") == "running"
              and os.path.basename(str(beat.get("session", "")).rstrip("/")) == name)
    result = consensus.score(data, ranks.extract_all(data))
    html_out = report.render(data, result, ranks.extract_all(data), running=running)
    report.write(os.path.join(target, "report.html"), html_out)
    return True


def make_handler(root, sp=None):
    root = os.path.realpath(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Roundtable"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):          # quiet by default
            if os.environ.get("ROUNDTABLE_ACCESS_LOG"):
                super().log_message(fmt, *args)

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            extra = extra or {}
            if "Cache-Control" not in extra:
                # A live report must never be served from cache; static
                # assets override this explicitly (see _serve_asset).
                self.send_header("Cache-Control", "no-store")
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, code, message):
            self._send(code, "<!doctype html><meta charset=utf-8>"
                             "<title>%s</title><body style='font:15px system-ui;"
                             "padding:40px'><h1>%d</h1><p>%s</p>"
                             "<p><a href='/'>Back to sessions</a></p>"
                       % (code, code, html.escape(message)))

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = posixpath.normpath(urllib.parse.urlparse(self.path).path)
            if path in ("/", "/index.html"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                notice = query.get("notice", [None])[0]
                tab = query.get("tab", [None])[0]
                selected = query.get("preset", [None])[0]
                values = {"preset": selected} if selected else None
                source = query.get("from", [None])[0]
                if source:
                    values, from_notice = _prompts_only(source, root, sp)
                    if values is None:
                        return self._error(404, "No such session.")
                    notice = notice or from_notice
                # Anything that pre-fills the form is asking for that tab,
                # whether or not the link that sent us here said so.
                if values and not tab:
                    tab = "new"
                return self._send(200, render_index(root, sp, notice=notice,
                                                    tab=tab, form_values=values))
            if path == "/health":
                return self._send(200, json.dumps({"ok": True}),
                                  "application/json; charset=utf-8")
            if path.startswith("/job/"):
                page, redirect = render_job(path[5:], root, sp)
                if redirect:
                    return self._send(302, b"", extra={"Location": redirect})
                return self._send(200, page)
            if path == "/rejudge":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                page = render_rejudge(root, query.get("session", [""])[0])
                if page is None:
                    return self._error(404, "No such session.")
                return self._send(200, page)
            if path == "/trash":
                return self._send(200, render_trash(root))
            if path.startswith("/rebuild/"):
                name = path[len("/rebuild/"):].strip("/")
                if not rebuild_session(root, name, sp):
                    return self._error(404, "No such session.")
                return self._send(302, b"", extra={
                    "Location": "/s/%s/report.html" % urllib.parse.quote(name)})
            if path.startswith("/s/"):
                return self._serve_session(path[3:])
            if path.startswith("/assets/"):
                return self._serve_asset(path[len("/assets/"):])
            return self._error(404, "No such page.")

        def _read_form(self):
            """-> parse_qs()-style multi-dict, or None if the body was rejected
            (an error has already been sent)."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error(400, "Bad Content-Length.")
                return None
            if length > 4 * 1024 * 1024:          # prompts, not file uploads
                self._error(413, "That request is too large.")
                return None
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return urllib.parse.parse_qs(raw, keep_blank_values=True)

        def _json(self, code, payload):
            self._send(code, json.dumps(payload), "application/json; charset=utf-8")

        def do_POST(self):
            path = posixpath.normpath(urllib.parse.urlparse(self.path).path)
            if path == "/submit":
                return self._do_submit()
            if path == "/chain/submit":
                return self._do_chain_submit()
            if path == "/rerun":
                return self._do_rerun()
            if path == "/rejudge":
                return self._do_rejudge()
            if path == "/cancel":
                return self._do_cancel()
            if path == "/cleanup":
                return self._do_cleanup()
            if path == "/delete":
                return self._do_delete()
            if path == "/trash/empty":
                return self._do_empty_trash()
            if path == "/presets/save":
                return self._do_preset_save()
            if path == "/presets/delete":
                return self._do_preset_delete()
            if path == "/presets/reset":
                return self._do_preset_reset()
            if path == "/model-cards/save":
                return self._do_model_card_save()
            if path == "/model-cards/reset":
                return self._do_model_card_reset()
            return self._error(404, "No such endpoint.")

        def _do_submit(self):
            parsed = self._read_form()
            if parsed is None:
                return
            fields = {k: v[-1] for k, v in parsed.items()}
            fields["think_on"] = parsed.get("think_on", [])
            fields["think_off"] = parsed.get("think_off", [])

            job, error = job_from_form(fields)
            if error:
                # Hand the form back with what they typed still in it, on the
                # tab they typed it into.
                return self._send(400, render_index(root, sp, tab="new",
                                                    form_values=fields,
                                                    form_error=error))
            job_id = spool.submit(job, sp)
            # 303: the browser must follow with GET, not repeat the POST.
            return self._send(303, b"", extra={"Location": "/job/%s"
                                               % urllib.parse.quote(job_id)})

        def _do_chain_submit(self):
            parsed = self._read_form()
            if parsed is None:
                return
            chain_name = (parsed.get("chain_name") or [""])[-1].strip()
            manuscript = (parsed.get("chain_manuscript") or [""])[-1].strip()
            think_on = [m for m in parsed.get("chain_think_on", []) if m.strip()]
            think_off = [m for m in parsed.get("chain_think_off", []) if m.strip()]
            extra_models = (parsed.get("chain_extra_models") or [""])[-1]
            stage_values, spec_stages = _stages_from_form(parsed)
            values = {"chain_name": chain_name, "manuscript": manuscript,
                     "stages": stage_values, "think_on": think_on,
                     "think_off": think_off, "extra_models": extra_models}

            def fail(message):
                return self._send(400, render_index(
                    root, sp, tab="chain", chain_values=values, chain_error=message))

            models = _models_from_checks(think_on, think_off, extra_models)
            if not models:
                return fail("Pick at least one model, or give a pattern to match.")
            if spec_stages:
                spec_stages[0]["models"] = models

            spec = {"stages": spec_stages}
            if manuscript:
                spec["manuscript_text"] = manuscript
            try:
                chain_mod.validate_spec(spec)
            except ValueError as exc:
                return fail(str(exc))
            for stage in spec_stages:
                if not stage.get("user_prompt", "").strip():
                    return fail("Stage %r needs a prompt." % stage.get("name", "?"))

            job = {"chain_spec": spec, "chain_name": _safe_name(chain_name) or "chain",
                  "sessions_root": root}
            job_id = spool.submit(job, sp)
            return self._send(303, b"", extra={"Location": "/job/%s"
                                               % urllib.parse.quote(job_id)})

        def _do_rerun(self):
            """Queue the same run again: same prompts, models and settings."""
            parsed = self._read_form()
            if parsed is None:
                return
            name = _safe_name((parsed.get("session") or [""])[-1])
            if not name or not os.path.isdir(os.path.join(root, name)):
                return self._error(404, "No such session.")
            job, source = job_for_session(name, root, sp)
            if job is None:
                return self._error(
                    409, "Nothing to rerun: this session has no job record and "
                         "no result files to read its settings back from.")
            if not (job.get("user_prompt") or "").strip():
                # A session whose prompt file was emptied would otherwise queue
                # a job the runner immediately rejects.
                return self._error(409, "That session has no prompt to rerun.")
            job["rerun_of"] = name
            job["rerun_from"] = source
            job_id = spool.submit(job, sp)
            return self._send(303, b"", extra={"Location": "/job/%s"
                                               % urllib.parse.quote(job_id)})

        def _do_rejudge(self):
            """Queue Round 2 again over a finished session, with a new panel."""
            parsed = self._read_form()
            if parsed is None:
                return
            name = _safe_name((parsed.get("session") or [""])[-1])
            sdir = os.path.join(root, name) if name else None
            if not sdir or not os.path.isdir(sdir):
                return self._error(404, "No such session.")
            judges = [j for j in parsed.get("judges", []) if j.strip()]
            if not judges:
                return self._send(400, render_rejudge(
                    root, name, error="Pick at least one judge."))
            data = session_mod.load(sdir)
            if not data:
                return self._error(409, "That session has no results to judge.")
            job = {"judge_only": sdir,
                   "judges": judges,
                   "summarize": True,
                   "meta_summary": bool(parsed.get("meta_summary")),
                   # Keep the session's own character: a run judged in the open
                   # must not silently become a blind one on the way back.
                   "blind": bool(data["blind"]),
                   # Same seed, so the same panel re-run reads the outputs in
                   # the same shuffled order both times.
                   "seed": data["seed"],
                   "rejudge_of": name,
                   "sessions_root": root}
            job_id = spool.submit(job, sp)
            return self._send(303, b"", extra={"Location": "/job/%s"
                                               % urllib.parse.quote(job_id)})

        def _do_cancel(self):
            """Stop one job: queued, running, or left claimed by a dead worker."""
            parsed = self._read_form()
            if parsed is None:
                return
            job_id = _safe_name((parsed.get("job") or [""])[-1])
            if not job_id:
                return self._error(400, "No job given.")
            ok, message = cancel_job(job_id, root, sp)
            if not ok:
                return self._error(409, message)
            back = (parsed.get("back") or [""])[-1]
            if back == "job":
                return self._send(303, b"", extra={
                    "Location": "/job/%s" % urllib.parse.quote(job_id)})
            return self._send(303, b"", extra={
                "Location": "/?notice=%s" % urllib.parse.quote(message)})

        def _do_delete(self):
            """Move a session to the trash folder. POST, so no link can do it."""
            parsed = self._read_form()
            if parsed is None:
                return
            ok, message = trash_session(root, (parsed.get("session") or [""])[-1], sp)
            if not ok:
                return self._error(409 if "right now" in message else 404, message)
            return self._send(303, b"", extra={
                "Location": "/?notice=%s" % urllib.parse.quote(message)})

        def _do_empty_trash(self):
            """Delete the binned sessions for good. No undo past this point."""
            if self._read_form() is None:
                return
            _n, message = empty_trash(root)
            return self._send(303, b"", extra={
                "Location": "/?notice=%s" % urllib.parse.quote(message)})

        def _do_cleanup(self):
            if self._read_form() is None:
                return
            reaped = clean_up(root, sp)
            notice = ("Cleared %d stopped job(s)." % len(reaped) if reaped
                      else "Nothing to clean up.")
            return self._send(303, b"", extra={
                "Location": "/?notice=%s" % urllib.parse.quote(notice)})

        def _do_preset_save(self):
            parsed = self._read_form()
            if parsed is None:
                return
            fields = {k: v[-1] for k, v in parsed.items()}
            try:
                preset, overwrote_bundled = presets_mod.save(
                    fields.get("title", ""), fields.get("system_prompt", ""),
                    fields.get("expects", ""), preset_id=fields.get("id") or None)
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "id": preset["id"],
                                    "title": preset["title"],
                                    "overwrote_bundled": overwrote_bundled})

        def _do_preset_delete(self):
            parsed = self._read_form()
            if parsed is None:
                return
            preset_id = (parsed.get("id") or [""])[0]
            if not preset_id:
                return self._json(400, {"ok": False, "error": "no preset id given"})
            was_bundled = preset_id in presets_mod.bundled_ids()
            deleted = presets_mod.delete(preset_id)
            return self._json(200, {"ok": True, "deleted": deleted,
                                    "reverted": deleted and was_bundled})

        def _do_preset_reset(self):
            presets_mod.reset()
            return self._json(200, {"ok": True})

        def _do_model_card_save(self):
            parsed = self._read_form()
            if parsed is None:
                return
            fields = {k: v[-1] for k, v in parsed.items()}
            card_id = fields.get("id", "")
            try:
                thinking = json.loads(fields.get("thinking") or "{}")
                nothinking = json.loads(fields.get("nothinking") or "{}")
            except ValueError:
                return self._json(400, {"ok": False, "error": "malformed settings"})
            try:
                card = model_cards_mod.save(card_id, thinking, nothinking)
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            return self._json(200, {"ok": True, "id": card["id"]})

        def _do_model_card_reset(self):
            parsed = self._read_form()
            if parsed is None:
                return
            card_id = (parsed.get("id") or [""])[0]
            if not card_id:
                return self._json(400, {"ok": False, "error": "no card id given"})
            reverted = model_cards_mod.reset(card_id)
            return self._json(200, {"ok": True, "reverted": reverted})

        def _serve_session(self, rel):
            target = _resolve(root, rel)
            if target is None:
                return self._error(403, "That path is outside the sessions directory.")
            if os.path.isdir(target):
                index = os.path.join(target, "report.html")
                if os.path.exists(index):
                    return self._send(302, b"", extra={
                        "Location": "/s/%s/report.html"
                                    % urllib.parse.quote(rel.strip("/"))})
                return self._error(404, "That session has no report yet.")
            if not os.path.isfile(target):
                return self._error(404, "No such file.")
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            # text/markdown makes a browser download the file instead of
            # showing it, which is useless for the judging documents the
            # report links to -- they are meant to be read, in place.
            if ctype == "text/markdown":
                ctype = "text/plain"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            try:
                with open(target, "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError as exc:
                return self._error(500, "Could not read that file: %s" % exc)

        def _serve_asset(self, name):
            """Logo/favicon files bundled with the package -- static, so a
            long cache lifetime is fine; there's no way for the browser to
            hold a stale one past a Roundtable upgrade that changes the path.
            """
            target = _resolve(ASSETS_DIR, name)
            if target is None or not os.path.isfile(target):
                return self._error(404, "No such asset.")
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            try:
                with open(target, "rb") as f:
                    return self._send(200, f.read(), ctype,
                                      extra={"Cache-Control": "public, max-age=86400"})
            except OSError as exc:
                return self._error(500, "Could not read that file: %s" % exc)

    return Handler


def _root(root):
    return os.path.realpath(root or session_mod.__dict__.get(
        "DEFAULT_SESSIONS", os.path.expanduser("~/Apps/creative-bench")))


def bind(root=None, host=DEFAULT_HOST, port=DEFAULT_PORT, sp=None):
    """Take the port. -> a server ready to serve, or raise AlreadyRunning.

    Separate from serve() so a caller that starts other things (the worker, in
    ``roundtable up``) can find out the port is taken BEFORE starting them.
    """
    root = _root(root)
    os.makedirs(root, exist_ok=True)
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(root, sp))
    except OSError as exc:
        # Starting it twice is the ordinary way to hit this, and a traceback
        # about socket.bind reads like the app broke rather than like it is
        # already up.
        if exc.errno != errno.EADDRINUSE:
            raise
        raise AlreadyRunning(
            "Roundtable is already serving on http://%s:%d/ — open that, or "
            "stop the other one first (Ctrl-C in its terminal, or: pkill -f "
            "'roundtable up'). Use --port to run a second copy alongside it."
            % (host, port)) from None
    httpd.daemon_threads = True
    httpd.roundtable_root = root
    return httpd


def serve(root=None, host=DEFAULT_HOST, port=DEFAULT_PORT, sp=None, log=print,
          open_browser=False, httpd=None):
    """Run the server until interrupted. -> None.

    ``open_browser`` is off by default because this same function backs the
    systemd service (no display to open anything on) as well as interactive
    use -- only the CLI's interactive entry points turn it on.
    """
    httpd = httpd or bind(root, host=host, port=port, sp=sp)
    root = getattr(httpd, "roundtable_root", _root(root))
    host = httpd.server_address[0]
    url = "http://%s:%d/" % (host, httpd.server_address[1])
    log("serving %s at %s" % (root, url))
    if open_browser:
        import webbrowser
        try:
            if not webbrowser.open(url):
                log("could not open a browser automatically — visit %s" % url)
        except Exception as exc:
            log("could not open a browser automatically (%s) — visit %s" % (exc, url))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        httpd.server_close()
