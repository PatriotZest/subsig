/*
  Subsigmoid client API. Every view talks to the backend only through window.Subsigmoid.
  Endpoint shapes are documented in ../API.md.

  Until the backend exists, each call is answered by the MOCK section below from ../data/.
  To use the real backend, set
    <meta name="subsigmoid-api" content="https://your-app.databricksapps.com">
  in the page head, or open the page with ?api=https://...
*/
(function () {
  const fromUrl = new URLSearchParams(location.search).get('api');
  const fromMeta = document.querySelector('meta[name="subsigmoid-api"]')?.content;
  // 'self' means the backend serves this page (app/server.py), so call it on the same origin
  const BASE = (fromUrl || fromMeta || '') === 'self' ? '' : (fromUrl || fromMeta || '').replace(/\/$/, '');
  const LIVE = (fromUrl || fromMeta || '').length > 0;

  async function http(method, path, body) {
    const init = { method, headers: {} };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    const res = await fetch(BASE + path, init);
    if (!res.ok) { let m = `${res.status}`; try { m = (await res.json()).error || m; } catch {} throw new Error(m); }
    return res.json();
  }
  const cache = {};
  const data = path => cache[path] || (cache[path] = fetch('data/' + path).then(r => { if (!r.ok) throw new Error('missing data/' + path); return r.json(); }));
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem('subsigmoid:' + k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem('subsigmoid:' + k, JSON.stringify(v)); } catch {} }
  };

  /* ======================= MOCK backend (front end only) ======================= */
  const mock = {
    sessions: new Map(),
    // Without a backend there is nothing to show: exams only come from real uploads.
    async courses() { return [{ id: 'main', name: 'Exams', term: '' }]; },
    async course() { return { id: 'main', name: 'Exams', term: '', exams: [], skills: [], students: [], gradescope: { connected: false, last_sync: null }, databricks: { connected: false } }; },
    async sync() { return { last_sync: null, new_exams: 0 }; },
    async report(id) { throw new Error('No backend connected'); },
    async student() { throw new Error('No backend connected'); },
    async tags() { throw new Error('No backend connected'); },
    async saveTags() { throw new Error('No backend connected'); },
    async analyze() { throw new Error('No backend connected'); },
    async upload() { throw new Error('No backend connected. Run python3 app/server.py, or open the Databricks App.'); },
    async quiz() {
      const q = await data('quiz.json');
      return { ...q, items: q.items.map(it => ({ ...it, options: it.options.map(({ id, text }) => ({ id, text })) })) };
    },
    async startSession() { const id = 'mock-' + Math.random().toString(36).slice(2, 8); this.sessions.set(id, { answers: {} }); return { session_id: id, participants: 41 }; },
    async answer(sid, item, option) { this.sessions.get(sid).answers[item] = option; return { ok: true }; },
    // MOCK predictor: the wrong option whose mistake this person already made most often. Replaced by IRT + DINA on the backend.
    async predict(sid, itemId) {
      await wait(500);
      const q = await data('quiz.json'), s = this.sessions.get(sid), counts = {};
      for (const it of q.items) {
        const pick = it.options.find(o => o.id === s.answers[it.id]);
        if (pick?.mistake) counts[pick.mistake] = (counts[pick.mistake] || 0) + 1;
        if (pick && !pick.correct && it.skills.includes('neg')) counts['neg-sub'] = (counts['neg-sub'] || 0) + .5;
      }
      const target = q.items.find(i => i.id === itemId);
      let best = target.options.find(o => o.correct), n = 0;
      for (const o of target.options) if (o.mistake && (counts[o.mistake] || 0) > n) { best = o; n = counts[o.mistake]; }
      return { item: itemId, option: best.id, correct: !!best.correct, mistake: best.mistake || null,
               mistake_text: best.mistake ? q.mistakes[best.mistake] : null, p: +(best.correct ? .71 : Math.min(.9, .48 + .14 * n)).toFixed(2) };
    },
    async result(sid) {
      await wait(300);
      const q = await data('quiz.json'), s = this.sessions.get(sid), seen = {};
      const answers = q.items.map(it => {
        const o = it.options.find(x => x.id === s.answers[it.id]) || {};
        if (o.mistake) seen[o.mistake] = (seen[o.mistake] || 0) + 1;
        return { item: it.id, option: o.id, right: !!o.correct, mistake: o.mistake || null, skills: it.skills };
      });
      for (const a of answers) Object.assign(a, a.right ? { label: 'knew', p: .8 } : a.mistake && seen[a.mistake] >= 2 ? { label: 'gap', p: .82 } : { label: 'slip', p: .64 });
      const mastery = q.skills.map(sk => { const rel = answers.filter(a => a.skills.includes(sk.id)); return { skill: sk.id, name: sk.name, p: +((rel.filter(a => a.right).length + .5) / (rel.length + 1)).toFixed(2) }; });
      return { answers, mastery, mistakes: q.mistakes, participants: 41 };
    },
    // MOCK live session: a simulated room, so the Live view can be designed before the backend exists
    liveT0: Date.now(),
    async live() {
      const q = await data('quiz.json');
      const t = (Date.now() - this.liveT0) / 1000, n = Math.min(41, 6 + Math.floor(t / 2.2));
      let seed = 3; const R = () => (seed = seed * 16807 % 2147483647) / 2147483647;
      const people = [], recent = [];
      for (let k = 0; k < n; k++) {
        const done = Math.max(1, Math.min(7, Math.floor(R() * 4) + Math.floor((t - k * 2.2) / 4)));
        const pred = done >= 7, hit = pred && R() < .62;
        people.push({ id: k + 1, done, pred, hit });
      }
      for (let k = n - 1; k >= Math.max(0, n - 7); k--) {
        const p = people[k], it = q.items[Math.max(0, p.done - 1)];
        recent.push({ participant: `Participant ${p.id}`, item: it.id, prompt: it.prompt, predicted: p.pred, hit: p.hit, done: p.done });
      }
      const made = people.filter(p => p.pred).length, hits = people.filter(p => p.hit).length;
      return { simulated: true, session_id: 'room', quiz_url: new URL('quiz.html', location.href).href,
        participants: n, finished: made, predictions: { made, hits }, recent,
        evaluation: [
          { id: 'raw', name: 'Raw score', uses: 'percent right overall', accuracy: null },
          { id: 'count', name: 'Skill counting', uses: 'percent right per skill', accuracy: null },
          { id: 'irt', name: 'IRT alone', uses: 'question curves', accuracy: null },
          { id: 'subsigmoid', name: 'Subsigmoid', uses: 'curves, skills, tagged wrong options', accuracy: null }] };
    }
  };

  /* ======================= Public API ======================= */
  const enc = encodeURIComponent;
  window.Subsigmoid = {
    live: LIVE, base: BASE,
    // The local server keeps Databricks credentials and detailed records off the client.
    getDashboard: async () => {
      const res = await fetch('/api/dashboard', { cache: 'no-store' });
      if (res.status === 404) return null; // Static-only preview.
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || 'Could not load the assessment.');
      return body.configured ? body : null;
    },
    getCourses:   ()            => LIVE ? http('GET', '/api/courses') : mock.courses(),
    getCourse:    (cid)         => LIVE ? http('GET', `/api/courses/${enc(cid)}`) : mock.course(cid),
    syncGradescope:(cid)        => LIVE ? http('POST', `/api/courses/${enc(cid)}/sync`) : mock.sync(cid),
    // files: one or more graded CSVs, merged into one exam on the server
    uploadExport: (cid, files, name) => { if (!LIVE) return mock.upload(); const f = new FormData(); for (const x of [].concat(files)) f.append('file', x, x.webkitRelativePath || x.name); if (name) f.append('name', name); return http('POST', `/api/courses/${enc(cid)}/uploads`, f); },
    getTags:      (eid)         => LIVE ? http('GET', `/api/exams/${enc(eid)}/tags`) : mock.tags(eid),
    saveTags:     (eid, tags)   => LIVE ? http('PUT', `/api/exams/${enc(eid)}/tags`, tags) : mock.saveTags(eid, tags),
    analyze:      (eid)         => LIVE ? http('POST', `/api/exams/${enc(eid)}/analyze`) : mock.analyze(eid),
    getReport:    (rid)         => LIVE ? http('GET', `/api/reports/${enc(rid)}`) : mock.report(rid),
    getStudent:   (cid, sid)    => LIVE ? http('GET', `/api/courses/${enc(cid)}/students/${enc(sid)}`) : mock.student(cid, sid),
    getLive:      ()            => LIVE ? http('GET', '/api/live') : mock.live(),
    getQuiz:      ()            => LIVE ? http('GET', '/api/quiz') : mock.quiz(),
    startSession: ()            => LIVE ? http('POST', '/api/quiz/sessions') : mock.startSession(),
    submitAnswer: (sid, i, o)   => LIVE ? http('POST', `/api/quiz/sessions/${enc(sid)}/answers`, { item: i, option: o }) : mock.answer(sid, i, o),
    getPrediction:(sid, i)      => LIVE ? http('GET', `/api/quiz/sessions/${enc(sid)}/prediction?item=${enc(i)}`) : mock.predict(sid, i),
    getResult:    (sid)         => LIVE ? http('GET', `/api/quiz/sessions/${enc(sid)}/result`) : mock.result(sid)
  };
})();
