"""System-prompt presets for the submit form.

Two sources, merged by ``id``:

* the bundled ``presets/system-prompts.json`` in this repo
* a user file, so nobody has to edit a checked-in file to add their own:
  ``$ROUNDTABLE_PRESETS``, else ``$XDG_CONFIG_HOME/roundtable/presets.json``,
  else ``~/.config/roundtable/presets.json``

A user preset with the same ``id`` as a bundled one replaces it; new ids are
appended. A malformed or unreadable user file is ignored rather than fatal --
losing your presets should not stop you queueing a run.
"""
import json
import os

from . import spool

BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "presets", "system-prompts.json")


def user_path():
    override = os.environ.get("ROUNDTABLE_PRESETS")
    if override:
        return override
    config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config, "roundtable", "presets.json")


def _read(path):
    """-> list of preset dicts. Accepts {"presets": [...]} or a bare list."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("presets", [])
    if not isinstance(data, list):
        return []
    return [p for p in data
            if isinstance(p, dict) and p.get("title") and p.get("system_prompt")]


def _slug(title):
    out = "".join(c.lower() if c.isalnum() else "-" for c in title)
    return "-".join(part for part in out.split("-") if part)


def load(bundled=None, user=None):
    """-> list of presets, bundled first, user additions after.

    Every preset is guaranteed an ``id``; one is derived from the title if the
    file omits it.
    """
    order, by_id = [], {}
    for path in (bundled or BUNDLED, user or user_path()):
        for preset in _read(path):
            preset = dict(preset)
            preset.setdefault("id", _slug(preset["title"]))
            if preset["id"] not in by_id:
                order.append(preset["id"])
            by_id[preset["id"]] = preset          # user file wins on collision
    return [by_id[i] for i in order]


def find(preset_id, presets=None):
    for preset in presets if presets is not None else load():
        if preset["id"] == preset_id:
            return preset
    return None


def bundled_ids(bundled=None):
    """-> set of ids the repo ships. Used to warn before a save silently
    shadows a built-in preset rather than adding a new one."""
    return {p.setdefault("id", _slug(p["title"])) for p in _read(bundled or BUNDLED)}


def _read_user_list(path):
    """Like _read(), but keeps whatever shape the file already used
    ({"presets": [...]} vs a bare list), so save/delete don't rewrite a
    hand-edited file into a different format than the user chose."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], True                       # (entries, wrap_in_presets_key)
    if isinstance(data, dict):
        entries = data.get("presets", [])
        return (entries if isinstance(entries, list) else []), True
    return (data if isinstance(data, list) else []), False


def _write_user_list(path, entries, wrapped):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"presets": entries} if wrapped else entries
    spool.write_atomic(path, json.dumps(payload, indent=2) + "\n")


def save(title, system_prompt, expects="", preset_id=None, path=None):
    """Create or overwrite a user preset. -> (preset dict, overwrote_bundled).

    Pass the id you're editing as ``preset_id`` (the value the form's Role
    dropdown already carries) so editing a preset whose id was hand-picked
    and doesn't match ``_slug(title)`` -- every bundled one, e.g. "red-team"
    for "Red Team / Devil's Advocate" -- overwrites that entry instead of
    silently creating a near-duplicate. Leave it out (new preset, "Custom"
    selected) and the id is derived from the title.
    """
    title = (title or "").strip()
    system_prompt = (system_prompt or "").strip()
    if not title or not system_prompt:
        raise ValueError("a preset needs both a title and a system prompt")
    path = path or user_path()
    preset_id = preset_id or _slug(title)
    entries, wrapped = _read_user_list(path)
    entries = [e for e in entries if isinstance(e, dict) and e.get("id", _slug(e.get("title", ""))) != preset_id]
    preset = {"id": preset_id, "title": title, "system_prompt": system_prompt}
    if expects:
        preset["expects"] = expects.strip()
    entries.append(preset)
    _write_user_list(path, entries, wrapped)
    return preset, preset_id in bundled_ids()


def delete(preset_id, path=None):
    """Remove one user preset. -> True if it existed.

    Only ever touches the user file. If ``preset_id`` also names a bundled
    preset, this reverts to that original rather than removing the entry
    entirely -- deleting an override is exactly restoring the shipped default.
    """
    path = path or user_path()
    entries, wrapped = _read_user_list(path)
    kept = [e for e in entries
           if not (isinstance(e, dict)
                   and e.get("id", _slug(e.get("title", ""))) == preset_id)]
    if len(kept) == len(entries):
        return False
    _write_user_list(path, kept, wrapped)
    return True


def reset(path=None):
    """Discard every saved/edited preset, back to exactly what's bundled.

    -> True if there was a user file to remove.
    """
    path = path or user_path()
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True
