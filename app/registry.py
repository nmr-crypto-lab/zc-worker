"""Model registry: models.yaml (what should exist) + state/models.json (what was downloaded, where).

    import registry as R
    R.mains("zimage")            -> selectable models of a family (dicts with 'rel' = path for ComfyUI loaders)
    R.one("wan22_i2v", "te")     -> the text encoder of a family
    R.loras("h3_fl2va")          -> LoRAs of a family (Wan pairs have rel_high / rel_low)
"""
import json, os
import yaml

import zc_paths as P
ROOT = P.ROOT
MODELS_DIR = P.MODELS
REG_PATH = P.registry_path()
MANIFEST = P.MANIFEST


def load():
    reg = yaml.safe_load(open(REG_PATH))
    man = json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}
    for m in reg["models"]:
        fam = m.get("family")
        m["families"] = fam if isinstance(fam, list) else [fam]
        e = man.get(m["key"], {})
        paths = [p for p in ([e.get("path")] if e.get("path") else []) + (e.get("paths") or []) if p and os.path.exists(p)]
        parts = e.get("parts") or {}
        m["paths"] = paths
        m["ready"] = bool(paths) and (not ("high" in m.get("civitai", {})) or ("high" in parts and "low" in parts))
        top = m["dest"].split("/")[0]
        rel = lambda p: os.path.relpath(p, f"{MODELS_DIR}/{top}") if p else None
        m["rel"] = rel(paths[0]) if paths else None
        if parts:
            m["rel_high"], m["rel_low"] = rel(parts.get("high")), rel(parts.get("low"))
    return reg


_REG = None


def reg(reload=False):
    global _REG
    if _REG is None or reload:
        _REG = load()
    return _REG


def family(name):
    return reg()["families"][name]


def _of(fam, role):
    return [m for m in reg()["models"] if fam in m["families"] and m["role"] == role]


def mains(fam, ready_only=True):
    ms = [m for m in _of(fam, "main") if m.get("part") in (None, "high")]
    return [m for m in ms if m["ready"] or not ready_only]


def default_main(fam):
    ms = mains(fam)
    return next((m for m in ms if m.get("default")), ms[0] if ms else None)


def one(fam, role, part=None):
    for m in _of(fam, role):
        if part is None or m.get("part") == part:
            if m["ready"]:
                return m
    return None


def loras(fam):
    return [m for m in _of(fam, "lora") if m["ready"]]


def speed_loras(fam):
    return [m for m in _of(fam, "speed_lora") if m["ready"]]


def summary():
    out = []
    for name, f in reg()["families"].items():
        ms = [m for m in reg()["models"] if name in m["families"]]
        miss = [m["key"] for m in ms if not m["ready"] and not m.get("optional")]
        out.append(f"{name:12s} {f['status']:8s} models={len(ms)} missing={miss}")
    return "\n".join(out)


if __name__ == "__main__":
    print(summary())
