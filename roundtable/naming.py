"""Short, readable, unique names for GGUF files.

A model name like

    Qwen3.6-27B-Fable-Fus-711-UnHeretic-NM-DAU-NEO-MAX-NEO-MTP-IQ4_XS

carries its family, size, lineage and quantisation in one string. Charts need
the first two or three of those and no more, but the shortening has to stay
*unique* -- two runs of the same model at different quants must not collapse
into one label, which is exactly what a naive prefix does.
"""
import re

QUANT = r"(?:I?Q\d[\w.]*|NVFP4|MXFP4|BF16|F16|FP8)"

HEAD = 3   # family-size-variant is enough to recognise a model


def split(name):
    """-> (segments without the quant tag, quant tag or '')."""
    if "_" in name.split("-", 1)[0]:
        name = name.split("_", 1)[-1]            # drop TheDrummer_ / bartowski_
    quant = ""
    m = re.search(r"-(%s(?:_\w+)*)$" % QUANT, name)      # -Q5_K_L, -IQ4_XS
    if m:
        quant, name = m.group(1), name[:m.start()]
    m = re.search(r"-(%s)$" % QUANT, name)               # the NVFP4 in -NVFP4-Q8_0
    if m:
        quant, name = m.group(1), name[:m.start()]
    return name.split("-"), quant


def short_names(models):
    """-> {model: short label}, unique within this set.

    Starts from a fixed-width head so a model reads the same from one session to
    the next, lengthens only on collision, and falls back to the quant tag when
    two entries are the same model quantised differently.
    """
    models = list(dict.fromkeys(models))
    parts = {m: split(m) for m in models}
    out = {}
    for m in models:
        segments, quant = parts[m]
        candidate = "-".join(segments[:HEAD])
        for n in range(HEAD, len(segments) + 1):
            candidate = "-".join(segments[:n])
            if not any(o != m and "-".join(parts[o][0][:n]) == candidate
                       for o in models):
                break
        else:
            candidate = "%s-%s" % (candidate, quant) if quant else candidate
        out[m] = candidate
    # Same base and same quant: keep them apart rather than silently merging.
    for m, label in list(out.items()):
        if list(out.values()).count(label) > 1:
            twins = sorted(k for k, v in out.items() if v == label)
            out[m] = "%s#%d" % (label, twins.index(m) + 1)
    return out


def clip(text, width):
    return text if len(text) <= width else text[:width - 1] + "…"
