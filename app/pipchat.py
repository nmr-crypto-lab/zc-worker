"""Pip, the chat helper on the everyday home screen: Claude reads the chat and answers with a short line plus UI actions
(offer a task with the answers filled in, show examples, quick-reply buttons). It never generates anything itself:
it only routes people to the tasks we have.

What Pip knows about the app comes from pip_catalog.json, generated from ui/app.html by tools/gen_pip_catalog.js
(re-run it after changing tasks). Needs ANTHROPIC_API_KEY; ZC_PIP_MODEL picks the model (fast and cheap by default).
Not used in the 18+ section."""
import json, os, re, time, urllib.error, urllib.request

import llm

MODEL = os.environ.get("ZC_PIP_MODEL", "claude-haiku-5-5")
CAT = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pip_catalog.json")))
BY_ID = {(t["section"], t["task"]): t for t in CAT}
LANG = {"en": "English", "ru": "Russian", "id": "Indonesian"}


def _catalog_text():
    out = []
    for t in CAT:
        qs = []
        for q in t["questions"]:
            if q["type"] == "choice":
                qs.append(f'{q["id"]} = index of [{", ".join(f"{i}:{o}" for i, o in enumerate(q["options"]))}]')
            else:
                qs.append(f'{q["id"]} = text ({q["label"]})')
        out.append(f'- {t["section"]}/{t["task"]}: {t["title"]} — {t["what"]}{" Needs the user’s photo." if t["photo"] else ""}'
                   + (f'\n    answers: {"; ".join(qs)}' if qs else ""))
    return "\n".join(out)


SYSTEM = """You are Pip, the friendly little helper of Wishly — an app where ordinary people (often not tech-savvy, often older)
make greeting cards, video greetings, photo restorations, comics and cartoons for their family and friends.
You chat on the home screen. Your only job: understand what the person wants and lead them to the right task, with the
answers already filled in, so they just tap "Let's make it". You do not make pictures yourself.

How you talk:
- Reply in {LANG}. Warm, simple, short: 1–2 short sentences, like a friendly shop assistant. No markdown, no lists.
- Ask at most ONE question at a time, and only if you really need it; otherwise guess sensibly and offer.
- Always end with an action: offer_task, show_examples or quick_replies (you may combine a line + one action).
- When you offer a task, pre-fill EVERY answer you can infer from the chat: who it is for, the occasion, names (into the field that asks for a name), the words, the idea. The fewer questions left for the person, the better.
- Never promise things the app can't do. If asked for something we don't have, say so kindly and offer the closest task.
- Never discuss adult content here; the app has a separate 18+ section you never mention or link.
- No real public figures, no hurtful or political content; offer a kind alternative instead.
- People's photos: only tasks marked "Needs the user's photo" use them; tell them they'll add the photo on the next screen.

The tasks you can offer (section/task — what it is; answers you may pre-fill):
{CATALOG}

Context about this person: {CONTEXT}"""

TOOLS = [
    {"name": "offer_task", "description": "Show a card that opens one task with answers pre-filled. Use the exact section/task ids and answer ids from the list; choice answers are option indexes.",
     "input_schema": {"type": "object", "properties": {
         "section": {"type": "string"}, "task": {"type": "string"},
         "answers": {"type": "object", "description": "answer id -> option index (choice) or short text"},
         "also": {"type": "array", "items": {"type": "string"}, "description": "up to 2 alternative tasks as section/task"}},
         "required": ["section", "task"]}},
    {"name": "show_examples", "description": "Show a strip of example results for up to 8 tasks, tappable.",
     "input_schema": {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "string"}, "description": "section/task ids"}}, "required": ["tasks"]}},
    {"name": "quick_replies", "description": "Show 2–4 short tap answers to your question (the user can also type).",
     "input_schema": {"type": "object", "properties": {"options": {"type": "array", "items": {"type": "string"}}}, "required": ["options"]}},
]


def _clean_answers(t, ans):
    out = {}
    for q in t["questions"]:
        v = (ans or {}).get(q["id"])
        if v is None:
            continue
        if q["type"] == "choice":
            try:
                i = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(q["options"]):
                out[q["id"]] = i
        else:
            v = str(v).strip()[:300]
            if v:
                out[q["id"]] = v
    return out


def _task(ref):
    s, _, t = (ref or "").partition("/")
    return BY_ID.get((s, t))


def chat(messages, lang="en", context=""):
    """messages: [{"me": bool, "t": str}] (newest last). Returns {"t": str, "offer"?, "ex"?, "opts"?}."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise llm.NotConfigured("Pip's brain is not switched on (no ANTHROPIC_API_KEY)")
    conv = []
    for m in messages[-14:]:
        role = "user" if m.get("me") else "assistant"
        text = str(m.get("t") or "").strip()[:600]
        if not text:
            continue
        if conv and conv[-1]["role"] == role:
            conv[-1]["content"] += "\n" + text
        else:
            conv.append({"role": role, "content": text})
    while conv and conv[0]["role"] != "user":
        conv.pop(0)
    if not conv:
        raise ValueError("empty chat")
    system = SYSTEM.replace("{LANG}", LANG.get(lang, "English")).replace("{CATALOG}", _catalog_text()).replace("{CONTEXT}", context or "first visit")
    body = {"model": MODEL, "max_tokens": 600, "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": conv, "tools": TOOLS, "tool_choice": {"type": "auto"}}
    req = urllib.request.Request(llm.API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"pip error {e.code}: {e.read()[:200].decode(errors='replace')}")
    out = {"t": ""}
    for b in d.get("content", []):
        if b.get("type") == "text":
            out["t"] += b["text"]
        elif b.get("type") == "tool_use":
            i, name = b.get("input") or {}, b.get("name")
            if name == "offer_task":
                t = _task(f'{i.get("section")}/{i.get("task")}')
                if t:
                    also = [x for x in (i.get("also") or [])[:2] if _task(x)]
                    out["offer"] = {"sec": t["section"], "task": t["task"], "pre": _clean_answers(t, i.get("answers")),
                                    "more": [x.split("/") + [_task(x)["title"]] for x in also]}
            elif name == "show_examples":
                ex = [x.split("/") for x in (i.get("tasks") or [])[:8] if _task(x)]
                if ex:
                    out["ex"] = ex
            elif name == "quick_replies":
                opts = [str(o).strip()[:40] for o in (i.get("options") or [])[:4] if str(o).strip()]
                if opts:
                    out["opts"] = opts
    out["t"] = re.sub(r"[*_#`]", "", out["t"]).strip()[:500]
    if not out["t"] and not (out.keys() - {"t"}):
        raise RuntimeError("pip gave no answer")
    return out


_seen = {}   # user id -> [timestamps]; in memory, resets on restart — enough to stop runaway use


def allow(uid, per_day=150):
    now = time.time()
    lst = [t for t in _seen.get(uid, []) if now - t < 86400]
    if len(lst) >= per_day:
        _seen[uid] = lst
        return False
    lst.append(now)
    _seen[uid] = lst
    return True
