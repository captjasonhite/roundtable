"""Find the local models a bench run can choose from.

The form needs a list to tick boxes against. Discovery is a plain filesystem
walk over a models root (LM Studio's layout by default) rather than anything
clever, because that is what the bench runner itself matches against.
"""
import os

DEFAULT_ROOT = os.environ.get(
    "ROUNDTABLE_MODELS", os.path.expanduser("~/.lmstudio/models"))

# Projector files are companions to a vision model, never a model to run alone.
SKIP = ("mmproj",)


def discover(root=None):
    """-> [{name, path, size_gb, vision}] sorted by name.

    ``vision`` marks a model with a sibling ``mmproj-*.gguf``: it can be shown
    images, which matters for the vision bench but not the text one.
    """
    root = root or DEFAULT_ROOT
    found = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        ggufs = [f for f in filenames if f.endswith(".gguf")]
        has_proj = any(any(s in f.lower() for s in SKIP) for f in ggufs)
        for name in ggufs:
            if any(s in name.lower() for s in SKIP):
                continue
            path = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            found.append({
                "name": name[:-5],                 # drop the .gguf
                "path": path,
                "size_gb": round(size / 1e9, 1),
                "vision": has_proj,
            })
    # Multi-part GGUFs (…-00001-of-00003.gguf) are one model, not three.
    seen, out = set(), []
    for model in sorted(found, key=lambda m: m["name"].lower()):
        stem = model["name"]
        for marker in ("-00001-of-", "-00002-of-", "-00003-of-", "-00004-of-"):
            if marker in stem:
                stem = stem.split(marker)[0]
                break
        if stem in seen:
            continue
        seen.add(stem)
        model["name"] = stem
        out.append(model)
    return out


# Families where a real bench session showed thinking off scoring as well as
# or better than thinking on (2026-07-23/24, session 20260723-211143): Fable
# and Cydonia scored higher with thinking off (0.69 vs 0.61, 0.11 vs 0.06);
# Gemma4 tied exactly (0.47 both). All three: no quality reason to pay for the
# reasoning trace. Everything else in that session won clearly with thinking
# on and keeps that as the default. Re-derive rather than hand-edit this list
# if a new session changes the picture.
THINKING_OFF_DEFAULT = ("fable", "cydonia", "gemma")


def thinking_off_by_default(name):
    """Should this model's checkbox default to thinking OFF?

    Substring match against THINKING_OFF_DEFAULT, case-insensitive -- matches
    the same style of matching the bench script itself uses for --models.
    """
    lowered = name.lower()
    return any(family in lowered for family in THINKING_OFF_DEFAULT)


def sizes_note(models):
    """A one-line reminder that the queue is serial, not parallel."""
    if not models:
        return ""
    biggest = max(m["size_gb"] for m in models)
    return ("%d models found, largest %.1f GB — runs happen one at a time, so "
            "only one has to fit in VRAM." % (len(models), biggest))
