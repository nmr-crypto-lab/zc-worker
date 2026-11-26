"""18+ section, phase 1 ("Meet someone"): heroines and the recipes behind the request buttons.

A heroine = an appearance text (`look`, English) + a style + one approved portrait (`anchor`).
Recipes, as tested on the pod (Nov 2026):
  photo        portrait: PornMaster (zimage). Everyday scenes: Qwen-Image 2.1 with the portrait as reference (same
               face, hair, figure, one simple tattoo). Revealing outfits: the everyday frame is made first, then
               Qwen Edit (Rapid-AIO NSFW) changes only the clothes, so face/pose/place stay.
  anime        WAI Illustrious from the description alone (holds the character well).
  illustration PornMaster with an illustration prefix (real illustration look, natural proportions).
What holds across frames: face, hair colour/cut, figure, ONE simple tattoo. Avoid beauty marks, freckles, piercings,
left/right placement and big detailed tattoos - they drift.

Safety: fictional adults only (age >= 21 is stored and written into every prompt), every user text goes through the
same minors filter as /api/generate, inputs are only our own generated portraits/frames, and every output goes
through the handler's age gate (flagged results are deleted).
"""
import random

MIN_AGE = 21
STYLES = ("photo", "anime", "ill")
NEG = "child, teen, loli, petite, flat chest, school uniform, logo, watermark, text, lowres, bad hands"
NEG_CLOTHED = NEG + ", nude, naked, topless, nipples"   # covers and everyday asks: the NSFW models undress by default
CASUAL = "casual everyday clothes"

ILL_PREFIX = "digital painting illustration in the style of a modern book cover, visible painterly brush strokes, rich colors, stylized, not a photo, of "

# everyday moments for "what are you up to?" - picked at random (or from the heroine's own list later)
NOW = [
    "reading a book on the sofa under a blanket, warm lamp light",
    "cooking pasta in her kitchen, wooden spoon in hand, evening light",
    "sitting on a windowsill with a cup of tea, rain on the glass",
    "stretching on a yoga mat in her living room, morning sun",
    "walking through a flower market, a bouquet in her arms",
    "getting ready in front of a mirror, doing her makeup",
    "sitting in a cafe with a laptop and a cappuccino",
    "lying on the bed scrolling her phone, smiling, soft light",
]

OUTFITS = {   # id: (prompt, needs the two-step edit for photo heroines)
    "evening": ("an elegant black evening dress", False),
    "sweater": ("an oversized cozy knit sweater", False),
    "leather": ("a black leather jacket over a white top and jeans", False),
    "office": ("a fitted white blouse and a pencil skirt", False),
    "sport": ("a sports bra and leggings", False),
    "swim": ("a bikini", True),
    "lace": ("a black lace lingerie set", True),
    "silk": ("a short silk slip nightdress", True),
}

PLACES = {
    "paris": "at a Paris cafe terrace with a cup of coffee, bright daylight",
    "beach": "on a beach at sunset, waves behind her, golden light",
    "tokyo": "on a neon-lit street in Tokyo at night",
    "yacht": "on the deck of a yacht, sea and sky behind her, sunny",
    "chalet": "by the fireplace in a wooden ski chalet, snow outside the window",
    "rooftop": "on a hotel rooftop bar at dusk, city lights behind",
    "jazz": "in a smoky 1920s jazz club, vintage glamour, warm light",
    "riviera": "on the 1960s French Riviera, vintage film photo look",
}

# a revealing outfit is first made as an everyday frame in this outfit, then edited
NEUTRAL_FOR_EDIT = "a simple casual summer dress"


def _age(h):
    return max(MIN_AGE, int(h.get("age") or MIN_AGE))


def _who(h):
    return f"a {_age(h)}-year-old adult woman, {h['look']}"


def _ref_lead(h):
    return (f"The same {_age(h)}-year-old adult woman from the reference image: same face, same hair, same figure"
            f"{', same tattoo' if 'tattoo' in h['look'].lower() else ''}, exactly as in the reference. ")


def portrait_inputs(h, n=4):
    """candidate portraits; the owner picks one as the anchor"""
    st, seeds = h["style"], random.sample(range(1, 10 ** 6), n)
    if st == "anime":
        p = f"1girl, solo, mature female, adult, {_age(h)} years old, {h['look']}, wearing a simple fitted top, upper body portrait, looking at viewer, simple light background, soft light"
        return [{"family": "illustrious", "prompt": p, "negative": NEG_CLOTHED, "seed": s, "width": 896, "height": 1152} for s in seeds]
    pre = ILL_PREFIX if st == "ill" else "photo, "
    p = (f"{pre}head and shoulders portrait of {_who(h)}, simple fitted top, collarbones visible, "
         "plain light grey background, soft daylight, looking at the camera")
    return [{"family": "zimage", "prompt": p, "negative": NEG_CLOTHED, "seed": s, "width": 896, "height": 1152} for s in seeds]


def scene_for(ask, opt):
    """(scene text, outfit text or None, needs_edit)"""
    if ask == "selfie":
        return "taking a selfie with her phone at home, relaxed and smiling, natural light", None, False
    if ask == "now":
        return random.choice(NOW), None, False
    if ask == "wear":
        o, edit = OUTFITS.get(opt.get("outfit") or "", OUTFITS["evening"])
        return "posing in her bedroom by a large mirror, soft window light", o, edit
    if ask == "go":
        place = PLACES.get(opt.get("place") or "") or (opt.get("place_text") or "").strip()[:160] or PLACES["paris"]
        return place, None, False
    raise ValueError("unknown request")


def ask_inputs(h, ask, opt):
    """first job input + optional follow-up template (its images are filled with the first job's output)"""
    scene, outfit, edit = scene_for(ask, opt or {})
    neg = NEG if (outfit and edit) else NEG_CLOTHED     # undressing only when the person asked for a revealing outfit
    outfit = outfit or CASUAL
    st = h["style"]
    if st == "anime":
        p = f"1girl, solo, mature female, adult, {_age(h)} years old, {h['look']}, {outfit}, {scene}"
        return {"family": "illustrious", "prompt": p, "negative": neg, "width": 832, "height": 1216}, None
    if st == "ill":
        p = f"{ILL_PREFIX}{_who(h)}, wearing {outfit}, {scene}"
        return {"family": "zimage", "prompt": p, "negative": neg, "width": 832, "height": 1216}, None
    # photo
    wear = NEUTRAL_FOR_EDIT if edit else outfit
    text = f"{scene}{', wearing ' + wear if wear else ''}, candid smartphone photo, natural skin, no logos"
    if h.get("anchor"):
        first = {"family": "qwen_image21", "prompt": _ref_lead(h) + "She is " + text,
                 "images": [{"path": h["anchor"]}], "width": 896, "height": 1152}
    else:
        first = {"family": "zimage", "prompt": f"photo of {_who(h)}, {text}", "negative": NEG_CLOTHED if not edit else NEG, "width": 832, "height": 1216}
    then = None
    if edit:
        then = {"family": "qwen_aio",
                "prompt": f"Keep the same {_age(h)}-year-old adult woman, the same face, hair, pose and place. "
                          f"Change only her clothes to {outfit}. Realistic photo."}
    return first, then


def look_from_form(b):
    """the 'new heroine' form -> appearance text; only features that hold across frames"""
    parts = [b.get("hair"), b.get("eyes"), b.get("build"), b.get("tattoo"), (b.get("extra") or "").strip()[:200]]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _L(en, ru, id_):
    return {"en": en, "ru": ru, "id": id_}


# the starting catalog (owner_id NULL). Portraits are made by the admin from the heroine page ("Make portraits").
SEED = [
    ("mira", "Mira", 24, "photo", "sweet", "wavy shoulder-length auburn hair with bangs, green eyes, slim build",
     _L("Barista by day, paints at night. Shy at first, then impossible to stop laughing with.",
        "Днём бариста, ночью рисует. Сначала стесняется, потом с ней невозможно перестать смеяться.",
        "Barista siang, melukis malam. Awalnya pemalu, lalu bikin tertawa terus.")),
    ("lena", "Lena", 27, "photo", "sport", "long straight light brown hair in a high ponytail, blue eyes, athletic toned build",
     _L("Yoga teacher, up at sunrise, always a little bit competitive.",
        "Тренер по йоге, встаёт с рассветом, всегда немного соревнуется.",
        "Guru yoga, bangun saat fajar, selalu sedikit kompetitif.")),
    ("sofia", "Sofia", 29, "photo", "myst", "long dark wavy hair, dark brown eyes, olive skin, curvy figure",
     _L("Sings jazz in a basement club. Says little, notices everything.",
        "Поёт джаз в подвальном клубе. Говорит мало, замечает всё.",
        "Penyanyi jazz di klub bawah tanah. Sedikit bicara, memperhatikan segalanya.")),
    ("ava", "Ava", 25, "photo", "sport", "sun-bleached long blonde beach waves, hazel eyes, tanned skin, slim athletic build",
     _L("Surfer who lives in a van and follows the waves.",
        "Сёрфер, живёт в фургоне и ездит за волнами.",
        "Peselancar yang tinggal di van dan mengejar ombak.")),
    ("nora", "Nora", 31, "photo", "bold", "sleek black chin-length bob with blunt bangs, grey eyes, pale skin, slim build, a small black swallow tattoo on her collarbone",
     _L("Architect with a very dry sense of humour.",
        "Архитектор с очень сухим чувством юмора.",
        "Arsitek dengan selera humor yang sangat kering.")),
    ("iris", "Iris", 26, "photo", "art", "long platinum blonde straight hair, brown eyes, slim build",
     _L("Travel photographer, never in one city for long.",
        "Фотограф-путешественница, нигде не задерживается надолго.",
        "Fotografer perjalanan, tak pernah lama di satu kota.")),
    ("hana", "Hana", 23, "anime", "sweet", "long silver hair with a side braid, violet eyes, slim figure, medium breasts",
     _L("Librarian at a magic academy who reads your fortune in tea leaves.",
        "Библиотекарь магической академии, гадает на чаинках.",
        "Pustakawan akademi sihir yang membaca nasib dari daun teh.")),
    ("rei", "Rei", 25, "anime", "bold", "short spiky red hair, golden eyes, athletic figure, medium breasts",
     _L("Mecha pilot. Has never lost a duel and will remind you.",
        "Пилот меха. Ни разу не проигрывала и обязательно напомнит.",
        "Pilot mecha. Tak pernah kalah duel dan akan mengingatkanmu.")),
    ("aiko", "Aiko", 24, "anime", "myst", "very long black hair with a red ribbon, red eyes, slim figure, medium breasts",
     _L("Keeper of a mountain shrine, shares it with a mischievous fox spirit.",
        "Хранительница горного храма, делит его с хитрым лисьим духом.",
        "Penjaga kuil gunung, berbagi dengan roh rubah yang jahil.")),
    ("vera", "Vera", 30, "ill", "myst", "long copper hair in loose curls, green eyes, slender figure",
     _L("Muse from an art nouveau poster who stepped out of the frame.",
        "Муза с постера в стиле модерн, вышедшая из рамы.",
        "Muse dari poster art nouveau yang keluar dari bingkainya.")),
    ("luna", "Luna", 26, "ill", "sweet", "long pastel pink hair, blue eyes, slim figure",
     _L("Heroine of a watercolour fairy tale, a little absent-minded.",
        "Героиня акварельной сказки, немного рассеянная.",
        "Tokoh dongeng cat air, sedikit pelupa.")),
    ("roxy", "Roxy", 28, "ill", "bold", "short platinum pixie cut, brown eyes, athletic figure, a small star tattoo on her shoulder",
     _L("Comic-book heroine with a big attitude and a bigger motorbike.",
        "Героиня комиксов с характером и ещё большим мотоциклом.",
        "Tokoh komik dengan sikap besar dan motor yang lebih besar.")),
]


def seed_catalog(ST):
    if ST.flag("heroines_seeded"):
        return
    for hid, name, age, st, vibe, look, story in SEED:
        if not ST.heroine("cat-" + hid):
            ST.add_heroine(None, name, age, st, look, vibe=vibe, story=story, hid="cat-" + hid)
    ST.flag("heroines_seeded", 1)
