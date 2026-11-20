"""Local NVMe cache for model files (pods).

The global volume is the single source of truth for models but reads at ~100 MB/s, so a cold H3 load takes minutes.
At pod start:
  1. build a "symlink farm" in ComfyUI's models folder: every file on the volume gets a symlink (instant);
  2. in the background copy files to local disk (ZC_CACHE, default /cache/models) in priority order and repoint each
     symlink to the local copy once it is complete. ComfyUI resolves the path at load time, so later loads come from
     NVMe. Files added to the volume later are still found through extra_model_paths.yaml (volume fallback).

Priority: families in $ZC_CACHE_FAMILIES (comma list) first, then the rest of the registry; per family the default main
model and its encoders/VAEs first, other models and LoRAs after. $ZC_CACHE_ALL=1 also copies files the registry does
not know. Stops when local free space would drop below $ZC_CACHE_RESERVE_GB (default 30).

    python cache_models.py --farm-only     # step 1 only (run before ComfyUI starts)
    python cache_models.py                 # steps 1 + 2
"""
import json, os, shutil, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P

SRC = os.path.realpath(P.MODELS)
FARM = os.environ.get("ZC_FARM", f"{P.COMFY_DIR}/models")
CACHE = os.environ.get("ZC_CACHE", "/cache/models")
RESERVE = float(os.environ.get("ZC_CACHE_RESERVE_GB", "30")) * 1e9
STATUS = f"{P.LOGS}/cache.json"
SKIP_DIRS = ("safety",)          # age-gate models are read by our own code straight from the volume


def log(*a):
    print(time.strftime("[cache %H:%M:%S]"), *a, flush=True)


def build_farm():
    n = 0
    if os.path.islink(FARM):
        os.unlink(FARM)
    os.makedirs(FARM, exist_ok=True)
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel_dir = os.path.relpath(root, SRC)
        os.makedirs(os.path.join(FARM, rel_dir), exist_ok=True)
        for f in files:
            if f.startswith(".") or f.endswith(".part"):
                continue
            dst = os.path.join(FARM, rel_dir, f)
            if not os.path.lexists(dst):
                os.symlink(os.path.join(root, f), dst)
                n += 1
    log(f"farm: {n} links in {FARM} -> {SRC}")


def priority():
    import registry as R
    reg = R.reg()
    fams = [f.strip() for f in os.environ.get("ZC_CACHE_FAMILIES", "").split(",") if f.strip()]
    fams += [f for f in reg["families"] if f not in fams]
    order = []

    def add(m):
        for p in (m or {}).get("paths") or []:
            p = os.path.realpath(p)
            if p.startswith(SRC + os.sep) and os.path.isfile(p) and p not in order:
                order.append(p)

    for f in fams:                                          # pass 1: what the default generation of a family needs
        dm = R.default_main(f)
        add(dm)
        if dm and dm.get("part") == "high":                 # Wan: the matching low-noise model
            for m in reg["models"]:
                if f in m["families"] and m["role"] == "main" and m.get("part") == "low" and m["ready"]:
                    add(m)
        for role in ("te", "vae", "aux", "speed_lora"):
            for m in reg["models"]:
                if f in m["families"] and m["role"] == role and m["ready"]:
                    add(m)
    for f in fams:                                          # pass 2: other models and LoRAs
        for m in reg["models"]:
            if f in m["families"] and m["ready"]:
                add(m)
    if os.environ.get("ZC_CACHE_ALL") == "1":
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in sorted(files):
                p = os.path.join(root, fn)
                if p not in order:
                    order.append(p)
    return [p for p in order if os.path.relpath(p, SRC).split(os.sep)[0] not in SKIP_DIRS]


def _repoint(src, local):
    link = os.path.join(FARM, os.path.relpath(src, SRC))
    tmp = link + ".zctmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    os.symlink(local, tmp)
    os.replace(tmp, link)


def cache():
    files = priority()
    total = sum(os.path.getsize(p) for p in files)
    st = {"files": len(files), "bytes": total, "done_files": 0, "done_bytes": 0, "skipped": [], "t0": time.time()}
    log(f"queue: {len(files)} files, {total / 1e9:.0f} GB -> {CACHE}")
    os.makedirs(CACHE, exist_ok=True)
    for src in files:
        rel = os.path.relpath(src, SRC)
        dst = os.path.join(CACHE, rel)
        size = os.path.getsize(src)
        if not (os.path.exists(dst) and os.path.getsize(dst) == size):
            if shutil.disk_usage(CACHE).free - size < RESERVE:
                st["skipped"].append(rel)
                log(f"skip (disk): {rel} {size / 1e9:.1f} GB")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            t = time.time()
            shutil.copyfile(src, dst + ".part")
            os.replace(dst + ".part", dst)
            log(f"cached {rel} {size / 1e9:.1f} GB in {time.time() - t:.0f}s")
        _repoint(src, dst)
        st["done_files"] += 1
        st["done_bytes"] += size
        try:
            json.dump(st, open(STATUS, "w"))
        except OSError:
            pass
    log(f"done: {st['done_files']}/{len(files)} files in {time.time() - st['t0']:.0f}s, skipped {len(st['skipped'])}")


def extra_paths():
    """volume fallback for files added after the farm was built"""
    cats = ["checkpoints", "diffusion_models", "unet", "text_encoders", "clip", "vae", "loras", "clip_vision",
            "upscale_models", "audio_encoders", "model_patches"]
    y = "zc_volume:\n  base_path: " + SRC + "\n" + "".join(f"  {c}: {c}\n" for c in cats)
    open(f"{P.COMFY_DIR}/extra_model_paths.yaml", "w").write(y)


if __name__ == "__main__":
    build_farm()
    extra_paths()
    if "--farm-only" not in sys.argv and os.environ.get("ZC_CACHE", "") != "off":
        cache()
