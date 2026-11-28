// Writes app/pip_catalog.json — what Pip (the Claude chat helper) knows about the app — straight from the task list
// in ui/app.html, so it never drifts. Re-run after adding or changing tasks:  node app/tools/gen_pip_catalog.js
// (needs Playwright; opens app.html from disk, no server).
const { chromium } = require('playwright');
const path = require('path'), fs = require('fs');
(async () => {
  const b = await chromium.launch(), p = await b.newPage();
  await p.route('**/api/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
  await p.goto('file://' + path.resolve(__dirname, '../ui/app.html'));
  await p.waitForFunction(() => typeof SECTIONS === 'function');
  const cat = await p.evaluate(() => {
    S.lang = 'en';
    const out = [];
    for (const s of SECTIONS()) for (const k of s.tasks) {
      if (k.soon) continue;
      const story = typeof CKIND !== 'undefined' && (CKIND[k.id] || k.r === 'tale');
      if (k.album) { out.push({ section: s.id, task: k.id, title: k.t, what: k.line + ' (4–30 photos)', photo: true, questions: [{ id: 'title', type: 'text', label: 'album title, e.g. the family name' }] }); continue; }
      const r = !story && R[k.r];
      if (!story && !r) continue;
      const qs = story ? [{ id: 'idea', type: 'text', label: k.r === 'tale' ? 'child name, age and what they love' : 'the idea in one line (or the joke/tale/poem)' }]
        : r.qs.filter((q) => q !== 'text' && q !== 'mood' && Q[q]).map((q) => Q[q].type === 'input' || Q[q].type === 'area' ? { id: q, type: 'text', label: Q[q].l() } : { id: q, type: 'choice', label: Q[q].l(), options: Q[q].opts() });
      if (!story && r.qs.includes('text')) qs.push({ id: 'textOwn', type: 'text', label: 'the words written on it (optional, short)' });
      out.push({ section: s.id, task: k.id, title: k.t, what: k.line, photo: !!(k.upload || (r && (r.ups || r.multi))), questions: qs });
    }
    return out;
  });
  fs.writeFileSync(path.resolve(__dirname, '../pip_catalog.json'), JSON.stringify(cat, null, 1));
  console.log('tasks:', cat.length); await b.close();
})();
