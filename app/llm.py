"""Text model for the everyday section (screenwriter now, the advisor later): Claude through the Anthropic API.

Needs ANTHROPIC_API_KEY in the pod template (never in the repo). ZC_LLM_MODEL picks the model.
Only the everyday section uses this; the 18+ section will use an open model on our own servers.
"""
import json, os, re, urllib.error, urllib.request

API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ZC_LLM_MODEL", "claude-sonnet-5-5")


class NotConfigured(RuntimeError):
    pass


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask(system, user, max_tokens=3000, timeout=90):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NotConfigured("the story writer is not switched on yet (no ANTHROPIC_API_KEY)")
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"story writer error {e.code}: {e.read()[:200].decode(errors='replace')}")
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def ask_json(system, user, **kw):
    t = ask(system, user, **kw)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise RuntimeError("the story writer did not return a plan")
    return json.loads(m.group(0))
