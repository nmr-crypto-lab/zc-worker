// Create Movie: projects = named references + a JSON plan of clips -> H3 clips -> montage.
// Uses $, api, fileUrl, fmtT from index.html.
(() => {
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const enc = p => p.split("/").map(encodeURIComponent).join("/");
const thumb = (p, m) => `/thumb/${enc(p)}?v=${m || 0}`;
const ASPECTS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"];
let cur = null, S = null, sig = "", timer = null, msg = "", msgErr = false;
const root = () => $("#movie");

// two-tap confirm for destructive buttons
function armed(btn, label) {
  if (btn.classList.contains("armed")) return true;
  const old = btn.textContent; btn.classList.add("armed"); btn.textContent = label || "Tap again to confirm";
  setTimeout(() => { btn.classList.remove("armed"); btn.textContent = old; }, 4000);
  return false;
}
function say(t, err) { msg = t; msgErr = !!err; const m = $("#mv-msg"); if (m) { m.textContent = t; m.className = "mv-msg" + (err ? " err" : ""); } }
async function run(fn, okText) { try { const r = await fn(); if (okText) say(typeof okText === "function" ? okText(r) : okText); await refresh(true); return r; } catch (e) { say(e.message, true); } }

// ------------------------------------------------------------------ project list
async function showList() {
  cur = null; S = null; sig = ""; stopPoll();
  const ps = await api("/api/projects").catch(() => []);
  root().innerHTML = `<div class="card mv-wrap">
    <div class="row" style="margin-bottom:12px"><h3 style="margin:0">Movie projects</h3></div>
    <div class="row mv-new"><input type="text" id="mv-title" placeholder="New project name, e.g. Zloumyshlennik">
      <button class="mv-btn pri" id="mv-create" style="flex:0 0 auto">Create</button></div>
    <div class="mv-msg" id="mv-msg"></div>
    <div class="mv-plist">${ps.map(p => `<div class="mv-pcard" data-s="${esc(p.slug)}"><b>${esc(p.title)}</b>
      <span>${p.clips ? `${p.done}/${p.clips * p.variants} clips` : "no plan yet"} · ${p.refs} refs</span></div>`).join("") ||
      `<div class="hint">No projects yet.</div>`}</div></div>`;
  $("#mv-create").onclick = async () => {
    try { const r = await api("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: $("#mv-title").value }) }); open_(r.slug); }
    catch (e) { say(e.message, true); }
  };
  root().querySelectorAll(".mv-pcard").forEach(c => c.onclick = () => open_(c.dataset.s));
}

// ------------------------------------------------------------------ project page
function open_(slug) { cur = slug; sig = ""; msg = ""; refresh(true); startPoll(); }
function startPoll() { stopPoll(); timer = setInterval(() => { if (!document.hidden && root().style.display !== "none") refresh(); }, 4000); }
function stopPoll() { if (timer) clearInterval(timer); timer = null; }

async function refresh(force) {
  if (!cur) return;
  let st; try { st = await api("/api/projects/" + encodeURIComponent(cur)); } catch (e) { say(e.message, true); return; }
  const s2 = JSON.stringify(st);
  if (s2 === sig && !force) return;
  sig = s2; S = st; render();
}

function clipTile(c, v) {
  const sel = S.selection[c.i] === v.v;
  const inner = v.path ? `<img loading="lazy" decoding="async" src="${thumb(v.path, v.mtime)}">` : `<div class="vph">${v.status === "missing" ? "+" : "…"}</div>`;
  const badge = { done: "", queued: "queued", running: "generating", missing: "not made", error: "error" }[v.status];
  return `<div class="mv-tile ${sel ? "sel" : ""} st-${v.status}" data-i="${c.i}" data-v="${v.v}">${inner}
    <span class="b">${v.name}${badge ? " · " + badge : ""}</span>${v.outdated ? `<span class="o" title="prompt, length or a reference changed since this clip was made">changed</span>` : ""}</div>`;
}

function render() {
  const p = S.plan, nv = p ? p.variants : 1;
  const nSel = Object.keys(S.selection).length;
  const done = S.clips.reduce((a, c) => a + c.variants.filter(v => v.status === "done").length, 0);
  const total = p ? p.clips.length * p.variants : 0;
  const busy = S.clips.reduce((a, c) => a + c.variants.filter(v => v.status === "queued" || v.status === "running").length, 0);
  root().innerHTML = `<div class="mv-wrap">
  <div class="card mv-head"><div class="row" style="flex-wrap:wrap;gap:8px">
    <button class="mv-btn" id="mv-back" style="flex:0 0 auto">← Projects</button>
    <h3 style="margin:0;flex:1;color:var(--text)">${esc(S.title)}</h3>
    <button class="mv-btn" id="mv-export" style="flex:0 0 auto">Export for LLM</button>
    <button class="mv-btn danger" id="mv-delproj" style="flex:0 0 auto">Delete project</button></div>
    <div class="mv-msg${msgErr ? " err" : ""}" id="mv-msg">${esc(msg)}</div></div>

  <div class="card"><div class="row"><h3 style="margin:0">References</h3>
    <button class="mv-btn" id="mv-addref" style="flex:0 0 auto">+ Add from gallery</button></div>
    <div class="hint">Clips refer to references by name. Adding an image under an existing name replaces it everywhere.</div>
    <div class="mv-refs">${S.refs.map(r => `<div class="mv-ref"><img loading="lazy" src="${thumb(r.path, Math.round(r.updated || 0))}" data-full="${esc(r.path)}">
      <div class="n">${esc(r.name)}</div><div class="d">${esc(r.desc || "")}</div>
      <div class="u">${r.used_by.length ? "used in " + r.used_by.length + " clip" + (r.used_by.length > 1 ? "s" : "") : "not used"}</div>
      <button class="mv-btn danger sm" data-delref="${esc(r.name)}">Delete</button></div>`).join("") || `<div class="hint">No references yet.</div>`}</div></div>

  <div class="card"><div class="row" style="flex-wrap:wrap;gap:8px"><h3 style="margin:0;flex:1">Plan</h3>
    <label class="mv-btn" style="flex:0 0 auto;margin:0">Upload JSON<input type="file" id="mv-file" accept=".json,application/json" hidden></label>
    <button class="mv-btn" id="mv-paste" style="flex:0 0 auto">Paste JSON</button>
    ${p ? `<button class="mv-btn" id="mv-edit" style="flex:0 0 auto">Edit settings</button>` : ""}</div>
    ${p ? `<div class="hint">${p.clips.length} clips × ${p.variants} variant${p.variants > 1 ? "s" : ""} · ${p.aspect || "default aspect"} · ${p.megapixels || 0.5} MP · ${done}/${total} done${busy ? ` · ${busy} in the queue` : ""}</div>
      <button class="go" id="mv-gen">${done ? "Generate missing clips" : "Start generation"}</button>` :
      `<div class="hint">Upload or paste the JSON plan: {"variants": 3, "clips": [{"length": 10, "prompt": "…", "refs": ["denis", "room"]}]}. "Export for LLM" gives a ready brief for ChatGPT / Claude / GLM.</div>`}
    ${S.orphans.length ? `<div class="hint">Clips not in the current plan: ${S.orphans.join(", ")}</div>` : ""}</div>

  ${p ? `<div class="card"><h3>Clips <span class="hint">· tap a clip to watch it · green = chosen for the custom movie (${nSel})</span></h3>
    <div class="mv-clips">${S.clips.map(c => `<div class="mv-clip"><div class="mv-ch"><b>${c.name}</b> · ${c.length}s · ${c.refs.map(r => `<span class="${c.missing_refs.includes(r) ? "miss" : ""}">${esc(r)}</span>`).join(", ")}</div>
      <div class="mv-cp">${esc(c.prompt)}</div><div class="mv-vs">${c.variants.map(v => clipTile(c, v)).join("")}</div>
      ${c.variants.filter(v => v.error).map(v => `<div class="err">${v.name}: ${esc(v.error)}</div>`).join("")}</div>`).join("")}</div></div>

  <div class="card"><h3>Movies</h3>
    <div class="row" style="flex-wrap:wrap;gap:8px">
      <select id="mv-var" style="flex:0 0 120px">${Array.from({ length: Math.max(nv, ...S.clips.flatMap(c => c.variants.map(v => v.v)), 1) }, (_, k) => `<option value="${k + 1}">Variant ${k + 1}</option>`).join("")}</select>
      <button class="mv-btn pri" id="mv-mont" style="flex:0 0 auto">Montage</button>
      <label style="flex:0 0 auto;margin:0;display:flex;gap:6px;align-items:center"><input type="checkbox" id="mv-fb"> fill gaps with another variant</label>
      <button class="mv-btn pri" id="mv-cust" style="flex:0 0 auto" ${nSel ? "" : "disabled"}>Custom montage (${nSel})</button></div>
    <div class="hint">Uses the clips made so far, from 000 without gaps. Running it again overwrites the movie.</div>
    <div class="mv-movies">${S.movies.map(m => `<div class="mv-movie"><div class="vph" data-play="${esc(m.path)}"><img loading="lazy" src="${thumb(m.path, m.mtime)}"><span>▶</span></div>
      <div><b>${m.name}</b><div class="hint">${(m.info.clips || []).length}/${m.info.total_clips || "?"} clips · ${(m.size / 1e6).toFixed(1)} MB${m.info.stopped_at ? ` · stopped at ${m.info.stopped_at}` : ""}${(m.info.substituted || []).length ? ` · ${m.info.substituted.length} gap(s) filled` : ""}</div>
      <div class="row" style="gap:6px;margin-top:6px"><a class="mv-btn sm" href="${fileUrl(m.path)}?dl=1">Download</a><button class="mv-btn sm" data-play="${esc(m.path)}">Play</button><button class="mv-btn danger sm" data-delmovie="${m.name}">Delete</button></div></div></div>`).join("") || `<div class="hint">No movies yet.</div>`}</div></div>` : ""}
  </div>`;
  bind();
}

function bind() {
  $("#mv-back").onclick = showList;
  $("#mv-export").onclick = () => { location.href = `/api/projects/${encodeURIComponent(cur)}/export`; };
  $("#mv-delproj").onclick = async e => { if (!armed(e.target, "Tap again: delete the whole project")) return;
    try { await api(`/api/projects/${cur}`, { method: "DELETE" }); showList(); } catch (er) { say(er.message, true); } };
  $("#mv-addref").onclick = pickRef;
  root().querySelectorAll("[data-delref]").forEach(b => b.onclick = e => { if (armed(e.target)) run(() => api(`/api/projects/${cur}/refs/${encodeURIComponent(b.dataset.delref)}`, { method: "DELETE" }), "Reference deleted"); });
  root().querySelectorAll(".mv-ref img").forEach(i => i.onclick = () => modal(`<img src="${fileUrl(i.dataset.full)}">`, ""));
  $("#mv-file").onchange = async e => { const f = e.target.files[0]; if (f) savePlan(await f.text()); e.target.value = ""; };
  $("#mv-paste").onclick = () => modal(`<div class="mv-form"><h3>Paste the JSON plan</h3><textarea id="mv-json" style="min-height:50vh">${S.plan ? esc(JSON.stringify(S.plan, null, 2)) : ""}</textarea>
    <button class="go" id="mv-jsave">Save plan</button></div>`, "", () => { $("#mv-jsave").onclick = () => savePlan($("#mv-json").value); });
  const ed = $("#mv-edit"); if (ed) ed.onclick = editPlan;
  const g = $("#mv-gen"); if (g) g.onclick = () => run(() => api(`/api/projects/${cur}/generate`, { method: "POST" }), r => r.queued ? `${r.queued} clips added to the queue` : "Nothing to add: every clip is made or already queued");
  root().querySelectorAll(".mv-tile").forEach(t => t.onclick = () => clipModal(+t.dataset.i, +t.dataset.v));
  const mo = $("#mv-mont"); if (mo) mo.onclick = () => montage({ variant: +$("#mv-var").value, fallback: $("#mv-fb").checked });
  const cu = $("#mv-cust"); if (cu) cu.onclick = () => montage({ custom: true });
  root().querySelectorAll("[data-play]").forEach(b => b.onclick = () => modal(`<video src="${fileUrl(b.dataset.play)}" controls autoplay playsinline preload="auto"></video>`, ""));
  root().querySelectorAll("[data-delmovie]").forEach(b => b.onclick = e => { if (armed(e.target)) run(() => api(`/api/projects/${cur}/movies/${b.dataset.delmovie}`, { method: "DELETE" }), "Movie deleted"); });
}

async function savePlan(text) {
  let data; try { data = JSON.parse(text); } catch (e) { say("Not valid JSON: " + e.message, true); return; }
  const r = await run(() => api(`/api/projects/${cur}/plan`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan: data }) }),
    r => "Plan saved" + (r.warnings.length ? ". Warnings: " + r.warnings.join("; ") : ""));
  if (r) closeModal();
}

async function montage(body) {
  say("Joining clips…");
  const r = await run(() => api(`/api/projects/${cur}/montage`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    r => `${r.name}: ${r.clips.length}/${r.total_clips} clips` + (r.stopped_at ? `, stopped at ${r.stopped_at} (not made yet)` : "") + (r.substituted.length ? `, gaps filled: ${r.substituted.join(", ")}` : ""));
  return r;
}

// ------------------------------------------------------------------ edit settings
function editPlan() {
  const p = S.plan;
  modal(`<div class="mv-form"><h3>Edit settings</h3>
    <div class="row"><div><label>Variants per clip</label><select id="ep-var">${[1, 2, 3, 4, 5, 6, 7, 8].map(n => `<option ${n === p.variants ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div><label>Aspect</label><select id="ep-asp"><option value="">default</option>${ASPECTS.map(a => `<option ${a === p.aspect ? "selected" : ""}>${a}</option>`).join("")}</select></div>
      <div><label>Quality</label><select id="ep-mp"><option value="">default (0.5 MP)</option><option value="0.5" ${p.megapixels === 0.5 ? "selected" : ""}>0.5 MP</option><option value="0.9" ${p.megapixels === 0.9 ? "selected" : ""}>0.9 MP</option></select></div></div>
    ${p.clips.map((c, i) => `<div class="ep-clip"><div class="row"><b style="flex:0 0 auto">${String(i).padStart(3, "0")}</b>
      <div style="flex:0 0 90px"><input type="number" min="5" max="15" value="${c.length}" data-k="length" data-i="${i}" title="seconds"></div>
      <div><input type="text" value="${esc(c.refs.join(", "))}" data-k="refs" data-i="${i}" placeholder="references, comma separated"></div></div>
      <textarea data-k="prompt" data-i="${i}">${esc(c.prompt)}</textarea></div>`).join("")}
    <button class="go" id="ep-save">Save</button></div>`, "", () => {
    $("#ep-save").onclick = () => {
      const np = JSON.parse(JSON.stringify(p));
      np.variants = +$("#ep-var").value;
      const a = $("#ep-asp").value, mp = $("#ep-mp").value;
      if (a) np.aspect = a; else delete np.aspect;
      if (mp) np.megapixels = +mp; else delete np.megapixels;
      document.querySelectorAll("#mv-lbm [data-k]").forEach(el => {
        const c = np.clips[+el.dataset.i], k = el.dataset.k;
        c[k] = k === "length" ? +el.value : k === "refs" ? el.value.split(",").map(x => x.trim()).filter(Boolean) : el.value;
      });
      savePlan(JSON.stringify(np));
    };
  });
}

// ------------------------------------------------------------------ add reference from the general gallery
async function pickRef() {
  const g = await api("/api/gallery?kind=image&limit=200").catch(() => []);
  modal(`<div class="mv-form"><h3>Pick an image from the gallery</h3>
    <div class="mv-pick">${g.map(i => `<div class="item" data-p="${esc(i.path)}"><img loading="lazy" src="${thumb(i.path, i.mtime)}"></div>`).join("") || `<div class="hint">The gallery has no images yet.</div>`}</div>
    <div id="pr-form" style="display:none"><div class="row"><div><label>Name (used in the JSON)</label><input type="text" id="pr-name" placeholder="denis"></div>
      <div><label>Description (optional, helps the LLM)</label><input type="text" id="pr-desc" placeholder="Denis, old peasant, grey beard"></div></div>
      <div class="hint" id="pr-hint"></div><button class="go" id="pr-add">Add reference</button></div></div>`, "", () => {
    let chosen = null;
    document.querySelectorAll("#mv-lbm .mv-pick .item").forEach(it => it.onclick = () => {
      document.querySelectorAll("#mv-lbm .mv-pick .item").forEach(x => x.classList.toggle("pick", x === it));
      chosen = it.dataset.p; $("#pr-form").style.display = "block"; $("#pr-name").focus();
    });
    $("#pr-name").oninput = e => { const n = e.target.value.trim().toLowerCase(); $("#pr-hint").textContent = S.refs.some(r => r.name === n) ? `"${n}" exists: its picture will be replaced in every clip.` : ""; };
    $("#pr-add").onclick = async () => {
      const r = await run(() => api(`/api/projects/${cur}/refs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#pr-name").value, desc: $("#pr-desc").value, path: chosen }) }), r => `Reference "${r.name}" saved`);
      if (r) closeModal();
    };
  });
}

// ------------------------------------------------------------------ clip viewer
function clipModal(i, v) {
  const c = S.clips[i], e = c.variants.find(x => x.v === v); if (!e) return;
  const sel = S.selection[i] === v, busy = e.status === "queued" || e.status === "running";
  const media = e.path ? `<video src="${fileUrl(e.path)}" controls autoplay playsinline preload="auto"></video>` : `<div class="hint" style="padding:40px">${{ missing: "Not made yet", queued: "In the queue", running: "Generating…", error: "Failed: " + esc(e.error || "") }[e.status] || ""}</div>`;
  const acts = [];
  if (e.path) acts.push(`<a href="${fileUrl(e.path)}?dl=1">Download</a>`, `<button data-a="sel" class="${sel ? "on" : ""}">${sel ? "✓ In the custom movie" : "Add to movie"}</button>`);
  if (!busy) acts.push(`<button data-a="regen">${e.path ? "Regenerate" : "Generate"}</button>`);
  if (e.path) acts.push(`<button data-a="del" class="danger">Delete</button>`);
  acts.push(`<button data-a="close">Close</button>`);
  modal(`<div class="mv-cap"><b>${e.name}</b>${e.outdated ? ` · <span style="color:var(--warn)">changed since it was made</span>` : ""}<div class="hint">${esc(c.prompt)}</div></div>${media}`, acts.join(""), () => {
    $("#mv-lba").onclick = ev => {
      const a = ev.target.dataset.a; if (!a) return;
      if (a === "close") return closeModal();
      if (a === "sel") return run(() => api(`/api/projects/${cur}/select/${i}/${v}`, { method: "POST" })).then(() => clipModal(i, v));
      if (a === "regen") { if (e.path && !armed(ev.target, "Tap again: replace this take")) return; return run(() => api(`/api/projects/${cur}/clips/${i}/${v}/regenerate`, { method: "POST" }), `${e.name} added to the queue`).then(closeModal); }
      if (a === "del") { if (!armed(ev.target)) return; return run(() => api(`/api/projects/${cur}/clips/${i}/${v}`, { method: "DELETE" }), `${e.name} deleted`).then(closeModal); }
    };
  });
}

// ------------------------------------------------------------------ modal
function modal(body, actions, after) {
  let lb = $("#mv-lb");
  if (!lb) { lb = document.createElement("div"); lb.id = "mv-lb"; lb.innerHTML = `<div class="m" id="mv-lbm"></div><div class="a" id="mv-lba"></div>`; document.body.appendChild(lb);
    lb.addEventListener("click", e => { if (e.target === lb) closeModal(); }); }
  $("#mv-lbm").innerHTML = body; $("#mv-lba").innerHTML = actions || `<button data-a="close">Close</button>`;
  $("#mv-lba").onclick = e => { if (e.target.dataset.a === "close") closeModal(); };
  lb.style.display = "flex"; if (after) after();
}
function closeModal() { const lb = $("#mv-lb"); if (lb) { lb.style.display = "none"; $("#mv-lbm").innerHTML = ""; } }

// ------------------------------------------------------------------ view switch (Studio / Create Movie)
window.showView = v => {
  document.querySelectorAll("header .nav").forEach(n => n.classList.toggle("on", n.dataset.v === v));
  $("#studio").style.display = v === "studio" ? "" : "none";
  root().style.display = v === "movie" ? "" : "none";
  try { localStorage.setItem("zc_view", v); } catch (_) {}
  if (v === "movie") { if (cur) { refresh(true); startPoll(); } else showList(); } else stopPoll();
};
document.querySelectorAll("header .nav").forEach(n => n.onclick = () => window.showView(n.dataset.v));
let v0 = "studio"; try { v0 = localStorage.getItem("zc_view") || "studio"; } catch (_) {}
if (v0 === "movie") window.showView("movie");
})();
