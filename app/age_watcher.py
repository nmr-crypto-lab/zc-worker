#!/usr/bin/env python3
"""Background age gate: watches ComfyUI output + temp (previews) and checks every new image/video,
no matter where it came from (UI, API, notebook). Flagged or uncheckable files are deleted at once;
only metadata goes to the safety log. Until the gate model is loaded, new files are
held (not served from an approved state) and checked as soon as it is ready."""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zc_client as zc
import zc_paths as P

DIRS = [P.OUTPUT, f"{P.COMFY_DIR}/temp"]
EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov", ".mkv")
seen = {}  # path -> mtime already checked


def log(*a):
    print("[age-watch]", *a, flush=True)


def scan():
    for d in DIRS:
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(EXT):
                    p = os.path.join(root, f)
                    try:
                        st = os.stat(p)
                    except FileNotFoundError:
                        continue
                    yield p, st.st_mtime, st.st_size


def main():
    # files that existed before the watcher started were produced by earlier, already-gated runs
    for p, m, _ in scan():
        seen[p] = m
    t = time.time()
    gate = zc.gate()
    log(f"gate ready in {time.time()-t:.0f}s, watching {DIRS}")
    pending = {}
    STABLE_SECS = 6      # file must be unchanged this long (video encoders write slowly / at the end)
    RETRY_SECS = 90      # unreadable files are retried until this age, then deleted (fail closed)
    while True:
        now = time.time()
        for p, m, sz in scan():
            if seen.get(p) == m:
                continue
            if sz == 0 or now - m < STABLE_SECS:
                continue  # still being written
            if pending.get(p) != (m, sz):
                pending[p] = (m, sz)
                continue
            try:
                g = gate.check(p)
            except Exception as e:
                g = {"ok": False, "error": repr(e)[:200]}
            if not g.get("ok") and "error" in g and now - m < RETRY_SECS:
                continue  # probably not fully written yet: try again on a later pass
            pending.pop(p, None)
            entry = {"source": "watcher", "file_id": os.path.basename(p), "dir": os.path.dirname(p)[-40:], "gate": g}
            if not g.get("ok"):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
                entry["action"] = "deleted"
                log("deleted", os.path.basename(p), json.dumps(g)[:200])
            else:
                entry["action"] = "kept"
                seen[p] = m
            zc.safety_log(entry)
        time.sleep(1.5)


if __name__ == "__main__":
    main()
