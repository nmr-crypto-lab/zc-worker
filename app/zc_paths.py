"""Where things live. Same code on a pod (/workspace) and on serverless (/runpod-volume).

ROOT      the zc folder on the global volume (models, registry overrides, state)
OUTPUT    where ComfyUI writes results: on the volume for pods, local disk for serverless (results go to R2)
LOGS      local disk (appends to the object-backed global volume can block)
CODE      this directory (baked into the image)
"""
import os

CODE = os.path.dirname(os.path.abspath(__file__))


def _root():
    if os.environ.get("ZC_ROOT"):
        return os.environ["ZC_ROOT"]
    for p in ("/runpod-volume/zc", "/workspace/zc"):
        if os.path.isdir(p):
            return p
    return "/workspace/zc"


ROOT = _root()
MODELS = f"{ROOT}/models"
STATE = f"{ROOT}/state"
MANIFEST = f"{STATE}/models.json"
MODE = os.environ.get("ZC_MODE", "serverless" if ROOT.startswith("/runpod-volume") else "pod")
OUTPUT = os.environ.get("ZC_OUTPUT") or (f"{ROOT}/output" if MODE == "pod" else "/tmp/zc_output")
LOGS = os.environ.get("ZC_LOGS", "/root/zclogs")
SAFETY_LOG = os.environ.get("ZC_SAFETY_LOG", f"{LOGS}/safety.jsonl")
COMFY_DIR = os.environ.get("COMFY_DIR", "/opt/ComfyUI")


def registry_path():
    """the registry on the volume wins (add models without rebuilding the image); the baked copy is the fallback"""
    for p in (os.environ.get("ZC_REGISTRY"), f"{ROOT}/boot/models.yaml", f"{CODE}/models.yaml"):
        if p and os.path.exists(p):
            return p
    return f"{CODE}/models.yaml"


for d in (OUTPUT, LOGS):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
