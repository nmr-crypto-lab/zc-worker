#!/usr/bin/env python3
"""Download everything listed in models.yaml into the global volume. Idempotent: anything already in
state/models.json with an existing file is skipped (no re-downloads). Results/timings -> state/models.json.

    python fetch_models.py                 # all entries
    python fetch_models.py h3_fl2va illustrious   # only these families (or keys)
"""
import json, os, re, sys, time, shutil, traceback
import requests, yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_paths as P
ROOT = P.ROOT
M = f"{ROOT}/models"
STATE = f"{ROOT}/state"
REG = P.registry_path()
os.makedirs(STATE, exist_ok=True)
MANIFEST = f"{STATE}/models.json"
manifest = json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}
CIV = os.environ.get("CIVITAI_TOKEN", "").strip()
if CIV.startswith("{{"):
    CIV = ""
CIVAPI = "https://civitai.com/api/v1"


def log(*a):
    print("[fetch]", *a, flush=True)


def save():
    tmp = MANIFEST + ".tmp"
    json.dump(manifest, open(tmp, "w"), indent=1)
    os.replace(tmp, MANIFEST)


def gb(n):
    return f"{n/1e9:.1f}GB"


def have(key):
    e = manifest.get(key) or {}
    ps = ([e["path"]] if e.get("path") else []) + (e.get("paths") or [])
    return bool(ps) and all(os.path.exists(p) for p in ps)


# ---------------------------------------------------------------- Hugging Face
from huggingface_hub import HfApi, hf_hub_download, snapshot_download
api = HfApi()


def _vkey(path):
    return [int(x) for x in re.findall(r"\d+", path.split("/")[0])] or [0]


def hf_find(repos, pattern, exclude=None, newest=False):
    rx, ex = re.compile(pattern), re.compile(exclude) if exclude else None
    for repo in repos:
        try:
            files = api.list_repo_files(repo)
        except Exception as e:
            log(f"  repo {repo} not listable: {e.__class__.__name__}")
            continue
        hits = [f for f in files if rx.search(f) and not (ex and ex.search(f))]
        if hits:
            hits.sort(key=(lambda f: _vkey(f)) if newest else len, reverse=newest)
            return repo, hits[0]
    return None, None


def fetch_hf(m):
    s, key = m["hf"], m["key"]
    dest = f"{M}/{m['dest']}"
    os.makedirs(dest, exist_ok=True)
    repo, fname = hf_find(s["repos"], s["rx"], s.get("exclude"), s.get("newest", False))
    if not repo:
        raise RuntimeError(f"no file /{s['rx']}/ in {s['repos']}")
    out = f"{dest}/{s.get('rename') or os.path.basename(fname)}"
    t = time.time()
    if not os.path.exists(out):
        p = hf_hub_download(repo, fname, local_dir=f"{ROOT}/hfdl")
        shutil.move(p, out)
    dt = time.time() - t
    manifest[key] = {"src": f"hf:{repo}/{fname}", "path": out, "bytes": os.path.getsize(out), "secs": round(dt, 1)}
    return out, dt


def fetch_hf_snapshot(m):
    d = f"{M}/{m['dest']}"
    t = time.time()
    snapshot_download(m["hf_snapshot"], local_dir=d,
                      allow_patterns=m.get("allow") or ["*.json", "*.safetensors", "*.bin", "*.txt", "*.csv", "*.onnx"])
    manifest[m["key"]] = {"src": f"hf:{m['hf_snapshot']}", "path": d, "secs": round(time.time() - t, 1)}
    return d, time.time() - t


# ---------------------------------------------------------------- Civitai
def civ_headers():
    return {"Authorization": f"Bearer {CIV}"} if CIV else {}


def civ_files(vid):
    r = requests.get(f"{CIVAPI}/model-versions/{vid}", headers=civ_headers(), timeout=60)
    r.raise_for_status()
    j = r.json()
    fs = [f for f in j.get("files", []) if f.get("name", "").endswith(".safetensors")
          and f.get("type") not in ("Training Data", "Config", "Archive")]
    return j, fs


def civ_download(url, dest_dir, fallback_name):
    os.makedirs(dest_dir, exist_ok=True)
    url_t = url + ("&" if "?" in url else "?") + f"token={CIV}" if CIV else url
    r = requests.get(url_t, headers=civ_headers(), stream=True, timeout=120, allow_redirects=True)
    if r.status_code in (401, 403):
        r.close()
        r = requests.get(url_t, stream=True, timeout=120, allow_redirects=True)
    with r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        mm = re.search(r'filename="?([^";]+)', r.headers.get("content-disposition", ""))
        out = f"{dest_dir}/{mm.group(1) if mm else fallback_name}"
        if os.path.exists(out):
            return out
        tmp = out + ".part"
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 22):
                f.write(chunk)
        os.rename(tmp, out)
    return out


def civ_one(vid, file_rx, dest, key):
    j, fs = civ_files(vid)
    if file_rx:
        fs = [f for f in fs if re.search(file_rx, f["name"], re.I)] or []
    if not fs:
        raise RuntimeError(f"version {vid}: no matching .safetensors")
    f = fs[0]
    existing = f"{dest}/{f['name']}"
    p = existing if os.path.exists(existing) else civ_download(f["downloadUrl"], dest, f"{key}.safetensors")
    return p, j


def fetch_civitai(m):
    s, key, dest = m["civitai"], m["key"], f"{M}/{m['dest']}"
    t = time.time()
    if "high" in s:
        ph, jh = civ_one(s["high"], s.get("file_rx"), dest, key + "_high")
        pl, jl = civ_one(s["low"], s.get("file_rx"), dest, key + "_low")
        paths, parts, ver = [ph, pl], {"high": ph, "low": pl}, f"{jh.get('name')} + {jl.get('name')}"
        base = jh.get("baseModel")
    else:
        p, j = civ_one(s["version"], s.get("file_rx"), dest, key)
        paths, parts, ver, base = [p], {}, j.get("name"), j.get("baseModel")
    dt = time.time() - t
    manifest[key] = {"src": f"civitai-version:{s.get('version') or str(s['high']) + '+' + str(s['low'])}",
                     "version": ver, "base": base, "paths": paths, "parts": parts,
                     "files": [{"file": os.path.basename(p), "bytes": os.path.getsize(p)} for p in paths], "secs": round(dt, 1)}
    return paths[0], dt


def reconcile_parts(m):
    """older downloads of Wan pairs have no high/low map: match the files we have to the pinned versions by name"""
    e, s = manifest.get(m["key"], {}), m["civitai"]
    if e.get("parts"):
        return
    try:
        names = {}
        for part in ("high", "low"):
            _, fs = civ_files(s[part])
            names[part] = {f["name"] for f in fs}
        parts = {}
        for p in e.get("paths", []):
            for part, ns in names.items():
                if os.path.basename(p) in ns:
                    parts[part] = p
        if len(parts) == 2:
            e["parts"] = parts
            log(f"  {m['key']}: mapped existing files to high/low")
        else:
            log(f"  {m['key']}: existing files {[os.path.basename(p) for p in e.get('paths', [])]} are not the pinned "
                f"versions -> downloading pinned pair")
            manifest.pop(m["key"], None)
    except Exception as ex:
        log(f"  {m['key']}: reconcile failed: {ex}")


# ---------------------------------------------------------------- main
def wanted(m, sel):
    if not sel:
        return True
    fam = m.get("family")
    fams = fam if isinstance(fam, list) else [fam]
    return m["key"] in sel or any(f in sel for f in fams)


def main(sel):
    reg = yaml.safe_load(open(REG))
    t0 = time.time()
    civ_ok = None
    for m in reg["models"]:
        if not wanted(m, sel):
            continue
        key = m["key"]
        if have(key) and "civitai" in m and "high" in m["civitai"] and not manifest[key].get("parts"):
            reconcile_parts(m)
        if have(key):
            log(f"skip {key}")
            continue
        if "civitai" in m:
            if civ_ok is None:
                civ_ok = bool(CIV)
                if not civ_ok:
                    log("CIVITAI_TOKEN is empty -> Civitai entries skipped")
            if not civ_ok:
                continue
        try:
            if "hf" in m:
                p, dt = fetch_hf(m)
            elif "hf_snapshot" in m:
                p, dt = fetch_hf_snapshot(m)
            elif "civitai" in m:
                p, dt = fetch_civitai(m)
            else:
                raise RuntimeError("entry has no source")
            sz = manifest[key].get("bytes") or sum(f["bytes"] for f in manifest[key].get("files", []))
            log(f"OK {key} -> {p} {gb(sz or 0)} in {dt:.0f}s")
        except Exception as e:
            manifest[key] = {"error": str(e)[:300]}
            log(f"FAIL {key}: {e}")
            if os.environ.get("ZC_DEBUG"):
                traceback.print_exc()
        save()
    save()
    fails = [k for k, v in manifest.items() if isinstance(v, dict) and "error" in v]
    log(f"DONE in {time.time()-t0:.0f}s; failed: {fails or 'none'}")
    shutil.rmtree(f"{ROOT}/hfdl", ignore_errors=True)


if __name__ == "__main__":
    main(set(sys.argv[1:]))
