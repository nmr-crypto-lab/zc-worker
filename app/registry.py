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


def _here(p):
    """Manifest paths are absolute (/workspace/zc/models/... on pods). Map them onto this machine's volume mount so the
    same manifest works on serverless (/runpod-volume/zc/models/...)."""
    if not p or os.path.exists(p):
        return p
    if "/zc/models/" in p:
        q = f"{MODELS_DIR}/{p.split('/zc/models/', 1)[1]}"
        if os.path.exists(q):
            return q
    return p


def load():
    reg = yaml.safe_load(open(REG_PATH))
    man = json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}
    for m in reg["models"]:
        fam = m.get("family")
        m["families"] = fam if isinstance(fam, list) else [fam]
        e = man.get(m["key"], {})
        paths = [_here(p) for p in ([e.get("path")] if e.get("path") else []) + (e.get("paths") or [])]
        paths = [p for p in paths if p and os.path.exists(p)]
        parts = {k: _here(v) for k, v in (e.get("parts") or {}).items()}
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
