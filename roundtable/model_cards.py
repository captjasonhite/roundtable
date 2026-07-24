"""Per-model-family sampler settings, pulled from HuggingFace model cards.

Same two-source, merge-by-id shape as ``presets.py``:

* the bundled ``presets/model-cards.json`` in this repo
* a user file, so an edit made on the web page survives a repo update:
  ``$ROUNDTABLE_MODEL_CARDS``, else ``$XDG_CONFIG_HOME/roundtable/model-cards.json``,
  else ``~/.config/roundtable/model-cards.json``

A user card with the same ``id`` as a bundled one replaces it; new ids are
appended. A malformed or unreadable user file is ignored rather than fatal.

``creative-bench.sh`` reads the merged result too (via ``card_for.py``, a thin
CLI wrapper around ``match()``), so a card edited on the web page changes what
the next bench run actually uses -- one source of truth, not two.
"""
import json
import os

from . import spool

BUNDLED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "presets", "model-cards.json")

PROFILE_KEYS = ("temp", "top_p", "top_k", "min_p", "repeat", "presence")


def user_path():
    override = os.environ.get("ROUNDTABLE_MODEL_CARDS")
    if override:
        return override
    config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(config, "roundtable", "model-cards.json")


def _matches(card):
    m = card.get("match", [])
    return [m] if isinstance(m, str) else list(m)


def _read(path):
    """-> list of card dicts. Accepts {"cards": [...]} or a bare list."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("cards", [])
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if isinstance(c, dict) and c.get("id") and _matches(c):
            out.append(c)
    return out


def load(bundled=None, user=None):
    """-> list of cards, bundled first, user edits/additions after."""
    order, by_id = [], {}
    for path in (bundled or BUNDLED, user or user_path()):
        for card in _read(path):
            card = dict(card)
            if card["id"] not in by_id:
                order.append(card["id"])
            by_id[card["id"]] = card              # user file wins on collision
    return [by_id[i] for i in order]


def find(card_id, cards=None):
    for card in cards if cards is not None else load():
        if card["id"] == card_id:
            return card
    return None


def match(model_name, cards=None):
    """-> the first card whose ``match`` substring is in ``model_name``, or None.

    Same substring-against-lowercased-name style ``creative-bench.sh`` and
    ``models.thinking_off_by_default`` already use, so a card matches exactly
    the runs it would have matched as a bash ``elif`` branch.
    """
    lowered = (model_name or "").lower()
    for card in cards if cards is not None else load():
        if any(s.lower() in lowered for s in _matches(card)):
            return card
    return None


def bundled_ids(bundled=None):
    return {c["id"] for c in _read(bundled or BUNDLED) if c.get("id")}


def _read_user_list(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], True                       # (entries, wrap_in_cards_key)
    if isinstance(data, dict):
        entries = data.get("cards", [])
        return (entries if isinstance(entries, list) else []), True
    return (data if isinstance(data, list) else []), False


def _write_user_list(path, entries, wrapped):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"cards": entries} if wrapped else entries
    spool.write_atomic(path, json.dumps(payload, indent=2) + "\n")


def _clean_profile(values):
    out = {}
    for key in PROFILE_KEYS:
        v = values.get(key)
        if v in (None, ""):
            out[key] = None
            continue
        try:
            out[key] = float(v)
        except (TypeError, ValueError):
            raise ValueError("%s must be a number or blank" % key)
    return out


def save(card_id, thinking, nothinking, path=None):
    """Overwrite one card's thinking/no-thinking sampler values.

    Only the numbers change -- title/match/source/note/thinks are carried over
    from whatever ``load()`` currently returns for this id (bundled or a prior
    edit), so editing settings on the page never has to re-type the card's
    metadata. -> the saved card dict.
    """
    existing = find(card_id) or {}
    if not existing:
        raise ValueError("unknown card %r" % card_id)
    path = path or user_path()
    entries, wrapped = _read_user_list(path)
    entries = [e for e in entries if isinstance(e, dict) and e.get("id") != card_id]
    card = dict(existing)
    card["thinking"] = _clean_profile(thinking)
    card["nothinking"] = _clean_profile(nothinking)
    entries.append(card)
    _write_user_list(path, entries, wrapped)
    return card


def reset(card_id, path=None):
    """Discard a user edit for one card, back to the bundled default.

    -> True if a user override existed and was removed.
    """
    path = path or user_path()
    entries, wrapped = _read_user_list(path)
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("id") == card_id)]
    if len(kept) == len(entries):
        return False
    _write_user_list(path, kept, wrapped)
    return True
