/* Subsigmoid dashboard. Three places: the course (#/), one exam (#/exams/:id), one student (#/students/:id).
   Plus #/live for the judging demo and #/import for a CSV. All data comes through window.Subsigmoid (js/api.js). */
(function () {
  const API = window.Subsigmoid;
  const view = document.getElementById('view');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const pct = p => Math.round(p * 100) + '%';
  const date = d => new Date(d + (d.length === 10 ? 'T12:00:00' : '')).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const ago = iso => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 60 ? Math.max(m, 1) + 'm ago' : m < 1440 ? Math.round(m / 60) + 'h ago' : Math.round(m / 1440) + 'd ago'; };
  const LABEL = { knew: 'Knew it', guess: 'Probably a guess', slip: 'Probably a slip', gap: 'Skill missing', unclear: 'Can’t tell yet' };
  let course, liveTimer, connected = false;

  const status = e => e.status === 'analyzed' ? '<span class="st ok">Analyzed</span>' : e.status === 'needs_tags' ? '<span class="st warn">Needs skill tags</span>' : e.status === 'analyzing' ? '<span class="st warn">Running on Databricks</span>' : e.status === 'failed' ? '<span class="st">Failed</span>' : `<span class="st">${esc(e.status)}</span>`;
  const bar = p => `<span class="tb${p < .3 ? ' lo' : ''}"><i style="--w:${Math.max(4, p * 100)}%"></i>${pct(p)}</span>`;
  let curSkills = null;   // skills of the report on screen (uploaded exams have their own AI-tagged list)
  const skillName = id => (curSkills || []).find(s => s.id === id)?.name || course.skills.find(s => s.id === id)?.name || id;
  const analyzed = () => course.exams.filter(e => e.status === 'analyzed' && e.report_id && !e.report_note).sort((a, b) => a.date.localeCompare(b.date));
  const head = (title, meta, right = '') => `<div class="head"><div><h1>${title}</h1>${meta ? `<div class="meta">${meta}</div>` : ''}</div>${right ? `<div class="actions" style="margin:0">${right}</div>` : ''}</div>`;
  const sec = (title, body, extra = '') => `<section><div class="sh"><h2>${title}</h2>${extra ? `<span class="x">${extra}</span>` : ''}</div>${body}</section>`;
  const dots = parts => parts.filter(Boolean).join('<span class="d">·</span>');
  const nameList = (ids, R, max = 10) => { const n = ids.map(id => R.students.find(s => s.id === id)?.name || id); return n.length ? esc(n.slice(0, max).join(', ')) + (n.length > max ? ` and ${n.length - max} more` : '') : 'Nobody'; };

  async function loadCourse() {
    course = await API.getCourse('prealg-3');
    document.getElementById('crumb').textContent = course.name;
    document.getElementById('mode').hidden = API.live;
  }

  async function render() {
    clearInterval(liveTimer);
    if (connected) return dashboard();
    curSkills = null;
    const [a, b] = location.hash.replace(/^#\/?/, '').split('?')[0].split('/').filter(Boolean);
    scrollTo(0, 0);
    try {
      if (!a) await home();
      else if (a === 'exams' && b) await exam(b);
      else if (a === 'students' && b) await student(b);
      else if (a === 'live') await live();
      else if (a === 'import') importView();
      else location.hash = '#/';
    } catch (e) { console.error(e); view.innerHTML = `<div class="empty">Could not load this page: ${esc(e.message)}</div>`; }
  }

  /* ---------- compact Databricks assessment ---------- */
  async function dashboard() {
    document.querySelector('.top-r').hidden = true;
    document.getElementById('crumb').textContent = 'Assessment';
    document.getElementById('mode').hidden = true;
    view.innerHTML = '<div class="empty">Loading assessment…</div>';
    try {
      const D = await API.getDashboard();
      if (!D) throw new Error('The assessment connection is no longer configured.');
      const S = D.summary;
      const mode = document.getElementById('mode');
      mode.textContent = 'Synthetic data'; mode.hidden = !S.synthetic;
      const completed = new Date(D.completed_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      view.innerHTML = head(esc(D.name), dots(['Latest successful assessment', `Completed ${esc(completed)}`, S.synthetic ? '<span class="st warn">Synthetic data</span>' : '']), '<button class="btn" id="refresh">Refresh</button>')
        + `<div class="strip"><div><span>Students</span><b>${esc(S.students)}</b></div><div><span>Questions</span><b>${esc(S.questions)}</b></div><div><span>Responses</span><b>${esc(S.responses)}</b></div><div><span>Model</span><b style="font-size:20px">${S.model_fitted ? 'Fitted' : 'Not fitted'}</b></div></div>`
        + (!S.model_fitted ? `<p class="note" style="margin-top:16px">${esc(D.model_note || 'The model was not fitted for this assessment.')}</p>` : '')
        + (D.questions.length ? sec('Questions to review', `<div class="list">${D.questions.map(q => `<div class="row r-review"><span class="t">${esc(q.id)}</span><span class="s">${esc(q.text)}</span><span class="num">${pct(q.average)}</span></div>`).join('')}</div>`, 'Lowest average scores · up to 3 questions') : '<p class="note">No scored questions to review yet.</p>');
      document.getElementById('refresh').onclick = dashboard;
    } catch (err) {
      view.innerHTML = head('Assessment', 'Could not load the latest result') + `<p class="note" role="alert">${esc(err.message)}</p><button class="btn" id="retry">Try again</button>`;
      document.getElementById('retry').onclick = dashboard;
    }
  }

  /* ---------- course home ---------- */
  async function home() {
    const ex = analyzed();
    const reps = await Promise.all(ex.map(e => API.getReport(e.report_id)));
    const sname = (r, id) => r.skills.find(s => s.id === id)?.name || skillName(id);
    const biggest = r => [...r.groups].sort((x, y) => y.students.length - x.students.length)[0];
    // the sample course's quizzes share one skill list, so the term trend uses those reports
    const courseIdx = reps.map((r, i) => i).filter(i => course.skills.every(k => reps[i].skills.some(s => s.id === k.id)));
    const cr = courseIdx.map(i => reps[i]), cx = courseIdx.map(i => ex[i]);
    const R = reps[reps.length - 1], E = ex[ex.length - 1];
    const has = (r, k) => r.students.filter(s => s.mastery[k]?.p >= .5).length / Math.max(1, r.students.length);
    const big = R && biggest(R);
    const under = R ? R.students.filter(s => s.score_note === 'understated') : [];
    const todo = course.exams.filter(e => e.status === 'needs_tags');
    const running = course.exams.filter(e => e.status === 'analyzing');

    const att = [
      ...running.map(e => [`#/exams/${e.id}`, `${esc(e.name)} is being analyzed`, 'job running in Databricks']),
      ...todo.map(e => [`#/exams/${e.id}`, `${esc(e.name)} needs skill tags`, `${e.n_items} questions synced from Gradescope`]),
      ...(big && big.students.length ? [[`#/exams/${E.id}`, `Reteach ${esc(sname(R, big.skill).toLowerCase())}`, `${big.students.length} students after ${esc(E.name)}`]] : []),
      ...(under.length ? [[`#/exams/${E.id}`, `${under.length} scores lower than what students know`, `${esc(E.name)}`]] : [])
    ].map(([h, t, m]) => `<a class="row r-att" href="${h}"><span class="st warn"></span><span><span class="t">${t}</span> <span class="m">· ${m}</span></span><span class="chev">›</span></a>`).join('');

    const skills = cr.length ? course.skills.map(k => {
      const first = has(cr[0], k.id), last = has(cr[cr.length - 1], k.id), d = Math.round((last - first) * 100);
      return `<div class="row r-skill"><span class="t">${esc(k.name)}</span>${bar(last)}<span class="m">of students have it</span><span class="m num" style="justify-self:end">${d >= 0 ? '+' : ''}${d} since ${esc(cx[0].name)}</span></div>`;
    }).join('') : '';

    const exams = course.exams.map(e => {
      let sum = '';
      const i = ex.findIndex(x => x.id === e.id);
      if (e.status === 'analyzed' && i >= 0) {
        const r = reps[i], g = biggest(r);
        sum = g && g.students.length ? `${g.students.length} missing ${esc(sname(r, g.skill).toLowerCase())}` : `${r.test.n_students} students · no missing skill found`;
      } else if (e.status === 'needs_tags') sum = 'Review the tags to run the analysis';
      else if (e.status === 'analyzing') sum = 'Analysis job running in Databricks';
      else if (e.status === 'failed') sum = 'The analysis job failed';
      return `<a class="row r-exam" href="#/exams/${esc(e.id)}"><span><span class="t">${esc(e.name)}</span> <span class="m">${date(e.date)}</span></span>${status(e)}<span class="s">${sum}</span><span class="chev">›</span></a>`;
    }).join('');

    const conn = course.databricks?.connected ? 'Databricks connected' : `Gradescope synced ${ago(course.gradescope.last_sync)}`;
    view.innerHTML = head(esc(course.name), dots([esc(course.term), `${course.students.length} students`, conn, `<a class="btn" href="#/import" style="height:26px;padding:0 8px;font-size:13px">Upload CSV</a>`]))
      + (att ? sec('Needs attention', `<div class="list">${att}</div>`) : '')
      + sec('Exams', `<div class="list">${exams}</div>`)
      + (skills ? sec('Skills', `<div class="list">${skills}</div>`, `sample class · as of ${esc(cx[cx.length - 1].name)}`) : '');
    if (running.length) liveTimer = setInterval(async () => { if (location.hash.replace(/^#\/?/, '') !== '') return clearInterval(liveTimer); const before = running.map(e => e.id).join(); await loadCourse(); if (course.exams.filter(e => e.status === 'analyzing').map(e => e.id).join() !== before) render(); }, 4000);
  }

  async function analyzing(e, back, meta) {
    const started = e.started ? Math.round(Date.now() / 1000 - e.started) : 0;
    view.innerHTML = back + head(esc(e.name), meta) + `<section style="margin-top:32px"><div class="list">
      <div class="row" style="grid-template-columns:1fr auto"><span><span class="t">Uploaded to the Unity Catalog volume</span><br><span class="m" style="font-family:var(--mono)">${esc(e.volume_path || '')}</span></span><span class="st ok">Done</span></div>
      <div class="row" style="grid-template-columns:1fr auto"><span><span class="t">Running the assessment job</span><br><span class="m">Databricks job ${esc(course.databricks?.job_id || '')}${e.run_url ? ` · <a href="${esc(e.run_url)}" target="_blank" rel="noopener">open the run ↗</a>` : ''}</span></span><span class="st warn" id="el">${started}s</span></div>
      <div class="row" style="grid-template-columns:1fr auto"><span class="t">Tagging skills and labeling every answer</span><span class="m">next</span></div>
    </div><p class="note" style="margin-top:12px">This page updates on its own when the report is ready, usually in a minute or two.</p></section>`;
    liveTimer = setInterval(async () => {
      if (!location.hash.includes(e.id)) return clearInterval(liveTimer);
      const el = document.getElementById('el'); if (el && e.started) el.textContent = Math.round(Date.now() / 1000 - e.started) + 's';
      await loadCourse();
      const now = course.exams.find(x => x.id === e.id);
      if (now && now.status !== 'analyzing') { clearInterval(liveTimer); render(); }
    }, 3000);
  }

  /* ---------- one exam ---------- */
  async function exam(id) {
    const e = course.exams.find(x => x.id === id);
    if (!e) { view.innerHTML = `<div class="empty">No exam “${esc(id)}”. <a href="#/">Back</a></div>`; return; }
    const back = `<a class="back" href="#/">← ${esc(course.name)}</a>`;
    const meta = dots([date(e.date), `${e.n_items} questions`, `${e.n_students} students`, status(e), e.report_note ? `<span class="lo">${esc(e.report_note)}</span>` : '']);
    if (e.status === 'analyzing') return analyzing(e, back, meta);
    if (e.status === 'failed') { view.innerHTML = back + head(esc(e.name), meta) + `<section style="margin-top:32px"><div class="callout">The Databricks run failed: ${esc(e.error || 'unknown error')}${e.run_url ? ` · <a href="${esc(e.run_url)}" target="_blank" rel="noopener">open the run ↗</a>` : ''}</div></section>`; return; }
    if (!(e.status === 'analyzed' && e.report_id)) return tags(e, back, meta);

    const R = await API.getReport(e.report_id);
    curSkills = R.skills;
    const mean = R.students.reduce((t, s) => t + s.score / s.max_score, 0) / R.students.length;
    const withGap = R.students.filter(s => Object.values(s.mastery).some(m => m.p < .3));
    const under = R.students.filter(s => s.score_note === 'understated'), over = R.students.filter(s => s.score_note === 'overstated');
    const weak = R.items.filter(i => i.flags.length);

    const groups = [...R.groups.map(g => [`Reteach ${esc(skillName(g.skill).toLowerCase())}`, g.students]),
      ['Scored lower than they know', under.map(s => s.id)], ['Scored higher than they know', over.map(s => s.id)]]
      .map(([t, ids]) => `<div class="row r-grp"><span class="t">${t}</span><span class="m num">${ids.length} students</span><span class="s">${nameList(ids, R)}</span></div>`).join('');

    const studentRows = [...R.students].sort((a, b) => a.score - b.score).map(s => `
      <tr class="go" data-s="${esc(s.id)}" data-name="${esc(s.name.toLowerCase())}"><td class="w">${esc(s.name)}</td><td class="num">${s.score}/${s.max_score}</td>
        ${R.skills.map(k => `<td>${bar(s.mastery[k.id].p)}</td>`).join('')}
        <td class="s" style="color:var(--text-2)">${s.score_note === 'understated' ? 'Score too low' : s.score_note === 'overstated' ? 'Score too high' : ''}</td>
        <td class="r"><a href="#/students/${esc(s.id)}" style="color:var(--text-3)">Open ›</a></td></tr>
      <tr class="det" data-for="${esc(s.id)}" hidden><td colspan="${R.skills.length + 4}"><div class="alist">${s.answers.map(a => `<div><span>${esc(a.item)} · ${esc(R.items.find(i => i.id === a.item)?.prompt)}</span><span class="${a.label === 'gap' || a.label === 'guess' ? 'lo' : ''}">${LABEL[a.label]}</span></div>`).join('')}</div></td></tr>`).join('');

    view.innerHTML = back + head(esc(e.name), meta) + `
      <div class="strip"><div><span>Average score</span><b>${pct(mean)}</b></div><div><span>Missing a skill</span><b class="${withGap.length ? 'lo' : ''}">${withGap.length}</b></div>
        <div><span>Score too low</span><b>${under.length}</b></div><div><span>Weak questions</span><b>${weak.length}</b></div></div>`
      + sec('Groups', `<div class="list">${groups}</div>`)
      + sec('Students', `<div class="list tscroll"><table class="tbl"><thead><tr><th>Name</th><th>Score</th>${R.skills.map(k => `<th>${esc(k.name)}</th>`).join('')}<th>Note</th><th></th></tr></thead><tbody id="srows">${studentRows}</tbody></table></div>`,
        '<input class="search" id="find" placeholder="Search students" aria-label="Search students">')
      + sec('Questions', `<div class="list tscroll"><table class="tbl"><thead><tr><th></th><th>Question</th><th>Skills</th><th class="r">Right</th><th class="r">Discrimination</th></tr></thead><tbody>
          ${R.items.map(i => `<tr><td class="q">${esc(i.id)}</td><td class="w">${esc(i.prompt)}${i.flags.length ? `<div class="lo" style="font-weight:400;font-size:13px">${esc(i.flags[0])}</div>` : ''}</td><td style="color:var(--text-2)">${i.skills.map(skillName).map(esc).join(', ')}</td><td class="r num">${pct(i.percent_correct)}</td><td class="r num${i.flags.length ? ' lo' : ''}">${i.discrimination.toFixed(2)}</td></tr>`).join('')}
        </tbody></table></div>`, 'Discrimination: how well a question separates students who know it from those who don’t')
      + (R.warnings.filter(w => !w.skill && !w.item).length ? sec('Notes from the analysis', `<div class="list">${R.warnings.filter(w => !w.skill && !w.item).map(w => `<div class="row" style="grid-template-columns:1fr"><span class="s">${esc(w.text)}</span></div>`).join('')}</div>`, R.tag_source === 'ai' ? 'skills tagged by AI from question text · stats from the Databricks job' : '') : '');

    const rows = document.getElementById('srows');
    rows.addEventListener('click', ev => {
      if (ev.target.closest('a')) return;
      const r = ev.target.closest('tr.go'); if (!r) return;
      const d = rows.querySelector(`tr.det[data-for="${CSS.escape(r.dataset.s)}"]`); d.hidden = !d.hidden; r.classList.toggle('open', !d.hidden);
    });
    document.getElementById('find').addEventListener('input', ev => {
      const q = ev.target.value.trim().toLowerCase();
      rows.querySelectorAll('tr.go').forEach(r => { const show = r.dataset.name.includes(q); r.hidden = !show; if (!show) { rows.querySelector(`tr.det[data-for="${CSS.escape(r.dataset.s)}"]`).hidden = true; r.classList.remove('open'); } });
    });
  }

  async function tags(e, back, meta) {
    const T = await API.getTags(e.id);
    const draw = () => {
      view.innerHTML = back + head(esc(e.name), meta) + `<section style="margin-top:32px"><div class="callout">AI suggested which skills each question tests, from the question text only. It never sees students. Fix anything wrong, then run the analysis.</div></section>`
        + sec('Skill tags', `<div class="list tscroll"><table class="tbl"><thead><tr><th></th><th>Question</th>${T.skills.map(s => `<th class="c">${esc(s.name)}</th>`).join('')}</tr></thead><tbody>
          ${T.items.map((it, r) => `<tr><td class="q">${esc(it.id)}</td><td class="w">${esc(it.prompt)}</td>${T.skills.map(s => `<td class="c"><button class="cb${it.skills.includes(s.id) ? ' on' : ''}" data-r="${r}" data-s="${esc(s.id)}" aria-pressed="${it.skills.includes(s.id)}" aria-label="${esc(it.id)} tests ${esc(s.name)}"></button></td>`).join('')}</tr>`).join('')}
          </tbody></table></div><div class="actions"><button class="btn dark" id="run">Approve and run analysis</button><button class="btn" id="save">Save draft</button><span class="note" id="st"></span></div>`, `${T.items.length} questions`);
    };
    draw();
    view.onclick = async ev => {
      const b = ev.target.closest('button.cb');
      if (b) { const it = T.items[+b.dataset.r], s = b.dataset.s; it.skills = it.skills.includes(s) ? it.skills.filter(x => x !== s) : [...it.skills, s]; it.source = 'edited'; draw(); return; }
      if (ev.target.id === 'save') { await API.saveTags(e.id, T); document.getElementById('st').textContent = 'Saved'; }
      if (ev.target.id === 'run') {
        ev.target.disabled = true; const st = document.getElementById('st'); st.textContent = 'Running…';
        try { await API.saveTags(e.id, T); await API.analyze(e.id); await loadCourse(); view.onclick = null; render(); }
        catch (err) { st.textContent = 'Failed: ' + err.message; ev.target.disabled = false; }
      }
    };
  }

  /* ---------- one student ---------- */
  async function student(sid) {
    const H = await API.getStudent(course.id, sid);
    if (!H.student) { view.innerHTML = `<div class="empty">No student “${esc(sid)}”. <a href="#/">Back</a></div>`; return; }
    const want = new URLSearchParams(location.hash.split('?')[1] || '').get('exam');
    const cur = H.exams.find(x => x.exam_id === want) || H.exams[H.exams.length - 1], last = H.exams[H.exams.length - 1];
    const missing = H.skills.filter(s => last.mastery[s.id].p < .3).map(s => s.name.toLowerCase());
    view.innerHTML = `<a class="back" href="#/exams/${esc(last.exam_id)}">← ${esc(last.name)}</a>`
      + head(esc(H.student.name), dots([missing.length ? `<span class="lo">Probably missing ${esc(missing.join(' and '))}</span>` : 'No missing skill', last.score_note === 'understated' ? 'Latest score lower than what they know' : '']))
      + sec('Skills', `<div class="list tscroll"><table class="tbl"><thead><tr><th>Skill</th>${H.exams.map(x => `<th>${esc(x.name)}</th>`).join('')}</tr></thead><tbody>
          ${H.skills.map(s => `<tr><td class="w">${esc(s.name)}</td>${H.exams.map(x => `<td>${bar(x.mastery[s.id].p)}</td>`).join('')}</tr>`).join('')}
          <tr><td style="color:var(--text-2)">Score</td>${H.exams.map(x => `<td class="num" style="color:var(--text-2)">${x.score}/${x.max_score}</td>`).join('')}</tr></tbody></table></div>`, 'chance of having each skill')
      + sec('Answers', `<div class="list tscroll"><table class="tbl"><tbody>${cur.answers.map(a => `<tr><td class="q">${esc(a.item)}</td><td class="w">${esc(a.prompt)}</td><td style="color:var(--text-2)">${a.points === a.max_points ? 'Correct' : esc(a.rubric)}</td><td class="r ${a.label === 'gap' || a.label === 'guess' ? 'lo' : ''}" style="white-space:nowrap">${LABEL[a.label]} <span style="color:var(--text-3)">${pct(a.p)}</span></td></tr>`).join('')}</tbody></table></div>`,
        `<span class="seg">${H.exams.map(x => `<a href="#/students/${esc(sid)}?exam=${esc(x.exam_id)}"${x === cur ? ' aria-current="page"' : ''}>${esc(x.name)}</a>`).join('')}</span>`);
  }

  /* ---------- live session ---------- */
  async function live() {
    const draw = async () => {
      if (!location.hash.startsWith('#/live')) return clearInterval(liveTimer);
      const L = await API.getLive(), rate = L.predictions.made ? L.predictions.hits / L.predictions.made : null;
      view.innerHTML = `<a class="back" href="#/">← ${esc(course.name)}</a>` + head('Live session', L.simulated ? '<span class="lo">Simulated room until the backend is connected</span>' : 'Results update every few seconds')
        + `<div class="join"><span>Join on your phone: <code>${esc(L.quiz_url.replace(/^https?:\/\//, ''))}</code></span><a class="btn" href="${esc(L.quiz_url)}" target="_blank" rel="noopener">Open quiz ↗</a></div>
        <div class="strip"><div><span>Joined</span><b>${L.participants}</b></div><div><span>Finished</span><b>${L.finished}</b></div><div><span>Predictions checked</span><b>${L.predictions.made}</b></div><div><span>Predicted exactly</span><b>${rate == null ? '—' : pct(rate)}</b></div></div>`
        + sec('Hide-one-answer test', `<div class="list"><table class="tbl"><thead><tr><th>Method</th><th>Uses</th><th class="r">Accuracy</th></tr></thead><tbody>${L.evaluation.map(m => `<tr><td class="w">${esc(m.name)}</td><td style="color:var(--text-2)">${esc(m.uses)}</td><td class="r num">${m.accuracy == null ? '—' : pct(m.accuracy)}</td></tr>`).join('')}</tbody></table></div>`, 'one answer per person is hidden and predicted')
        + sec('Activity', `<div class="list">${L.recent.map(r => `<div class="row" style="grid-template-columns:1fr auto"><span class="t">${esc(r.participant)}</span><span class="${r.predicted && r.hit ? 'st ok' : 'm'}">${r.predicted ? (r.hit ? 'Prediction right' : 'Prediction missed') : `Question ${r.done} of 7`}</span></div>`).join('')}</div>`);
    };
    await draw();
    liveTimer = setInterval(draw, 2000);
  }

  /* ---------- import ---------- */
  function importView() {
    view.innerHTML = `<a class="back" href="#/">← ${esc(course.name)}</a>` + head('Import an exam', 'Upload a graded CSV, for example from the PDF extraction notebook or a Gradescope export.')
      + `<label class="drop" id="drop"><input type="file" id="file" accept=".csv,text/csv"><b>Drop a graded CSV here or click to choose</b><span>One row per student per question: Student, Question, Question text, Points earned, Points possible</span></label>
        <div class="list" style="margin-top:16px"><div class="row" style="grid-template-columns:24px 1fr"><span class="m">1</span><span class="s">The file goes to your Unity Catalog volume.</span></div><div class="row" style="grid-template-columns:24px 1fr"><span class="m">2</span><span class="s">The assessment job runs on Databricks and scores every question and student.</span></div><div class="row" style="grid-template-columns:24px 1fr"><span class="m">3</span><span class="s">AI tags the skills each question tests (question text only), then every answer gets a label.</span></div></div>
        <p class="note" style="margin-top:12px">No file handy? <a href="data/sample-graded.csv" download>Download a sample graded CSV</a> (38 made-up students) and drop it in.</p><p class="note" id="up" style="margin-top:12px"></p>`;
    const handle = async f => {
      document.getElementById('up').textContent = 'Uploading to Databricks and starting the job…';
      try { const { exam: x } = await API.uploadExport(course.id, f); await loadCourse(); location.hash = `#/exams/${x.id}`; }
      catch (err) { document.getElementById('up').textContent = 'Upload failed: ' + err.message; }
    };
    const drop = document.getElementById('drop');
    document.getElementById('file').onchange = ev => ev.target.files[0] && handle(ev.target.files[0]);
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, ev => { ev.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', ev => ev.dataTransfer.files[0] && handle(ev.dataTransfer.files[0]));
  }

  window.addEventListener('hashchange', () => { view.onclick = null; render(); });
  async function start() {
    try {
      // Explicit full-backend configuration continues to use the original course views.
      if (!API.live) {
        const result = await API.getDashboard();
        if (result) { connected = true; await dashboard(); return; }
      }
      await loadCourse(); await render();
    } catch (err) {
      // A failing configured connection must never silently show made-up class data.
      document.querySelector('.top-r').hidden = true;
      view.innerHTML = head('Assessment', 'Could not load the dashboard') + `<p class="note" role="alert">${esc(err.message)}</p><button class="btn" id="retry">Try again</button>`;
      document.getElementById('retry').onclick = start;
    }
  }
  start();
})();
