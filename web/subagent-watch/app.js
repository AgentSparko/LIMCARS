/* Subagent Watch · LIMCARS — Hermes delegation live transcripts
 * Data: limcars hub endpoints (server.js) under the /subagent-watch/ slug,
 * served from the hermes delegation cache (~/.hermes/cache/delegation):
 *   batches                        → {generated, cache_present, batches, summaries}
 *   manifest/<deleg_id>            → live/<deleg_id>/manifest.json
 *   transcript/<deleg_id>/task-N.log  → live/<deleg_id>/task-N.log (HTTP Range works → tail poll)
 *   summary/subagent-summary-*.txt → final per-task summaries
 * Mobile rules: no canvas rAF loops, no backdrop-filter, polling pauses when tab hidden.
 */
(function () {
'use strict';

/* All fetches are relative to this page (/subagent-watch/index.html),
   so they hit the hub's /subagent-watch/* API routes. */
const API = '';

// LIMCARS-NEXT password gate: the parent dashboard embeds this page with
// ?pw=<password>; attach it to every data fetch (server accepts the header).
const LC_PW = new URLSearchParams(location.search).get('pw') || '';
// audit hygiene: once the pw is captured into LC_PW, drop it from the address
// bar so history/back-button/shared URLs don't keep the cleartext password
if (LC_PW) { try { history.replaceState(null, '', location.pathname); } catch (e) {} }
if (LC_PW) {
  const _lcFetch = window.fetch.bind(window);
  let lcBad = false;
  window.fetch = async (u, o = {}) => {
    const r = await _lcFetch(u, Object.assign({}, o, {
      headers: Object.assign({}, o.headers, { 'X-Limcars-Pw': LC_PW }) }));
    // wrong pw in the URL: bounce back to the masked prompt (once — the
    // retry param has no pw, so the else-branch prompt cannot loop)
    if ((r.status === 401 || r.status === 423) && !lcBad) {
      lcBad = true;
      setTimeout(() => { location.href = location.pathname + '?pwretry=1'; }, 500);
    }
    return r;
  };
} else {
  // opened standalone without ?pw= (bookmark, shared link): the data routes
  // 401 and the page would sit EMPTY (user-caught). Prompt for the password
  // once — masked input, never echo — then reload authenticated.
  const _lcRawFetch = window.fetch.bind(window);
  let lcAsked = false;
  window.fetch = async (u, o = {}) => {
    const r = await _lcRawFetch(u, o);
    if ((r.status === 401 || r.status === 423) && !lcAsked) {
      lcAsked = true;
      const box = document.createElement('div');
      box.id = 'lc-pwbox';
      box.style.cssText = 'position:fixed;inset:0;background:rgba(8,12,20,.88);' +
        'display:flex;align-items:center;justify-content:center;z-index:9999';
      box.innerHTML = '<div style="background:#141c2b;border:1px solid #2b3a55;' +
        'border-radius:12px;padding:22px 24px;font:14px system-ui;color:#dce6f5;' +
        'box-shadow:0 12px 40px rgba(0,0,0,.5)"><div style="margin-bottom:12px">' +
        '🔒 Hermes Traces are password-protected</div><input id="lc-pwin" type="password" ' +
        'placeholder="Password" autocomplete="current-password" style="width:220px;' +
        'padding:8px 10px;border-radius:8px;border:1px solid #33456a;background:#0b111c;' +
        'color:#dce6f5;font:14px system-ui"><div style="margin-top:12px;text-align:right">' +
        '<button id="lc-pwgo" style="padding:7px 16px;border-radius:8px;border:0;' +
        'background:#ff8c3a;color:#0b111c;font-weight:700;cursor:pointer">Open</button>' +
        '</div><div id="lc-pwerr" style="color:#ff6b6b;margin-top:8px;display:none">' +
        'Wrong password — try again</div></div>';
      document.body.appendChild(box);
      const go = () => {
        const v = document.getElementById('lc-pwin').value;
        if (!v) return;
        location.href = location.pathname + '?pw=' + encodeURIComponent(v);
      };
      box.querySelector('#lc-pwgo').addEventListener('click', go);
      box.querySelector('#lc-pwin').addEventListener('keydown', e => {
        if (e.key === 'Enter') go(); });
      box.querySelector('#lc-pwin').focus();
      if (location.search.includes('pwretry')) {
        box.querySelector('#lc-pwerr').style.display = 'block';
      }
      throw new Error('locked');   // stop the calling render loop; overlay rules
    }
    if (r.status === 401 || r.status === 423) throw new Error("locked");
    return r;
  };
}
const POLL_MS = 3000;            // auto-follow poll interval (running task open)
const SLOW_POLL_MS = 10000;      // no-growth fallback interval
const STALE_STOP_MS = 60000;     // stop when file stopped growing this long AND summary found
const RECENT_WINDOW_H = 72;      // only probe batches this recent for running tasks
const SUMMARY_WINDOW_S = 300;    // summary ts must be within 5 min of task final ts
const PROBE_TAIL = 600;          // bytes fetched to detect "final | end status="

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};
const followBtn = $('followBtn');

const state = {
  batches: [],          // from /subagent-watch/batches
  mainSessions: [],     // from /subagent-watch/main-sessions (live main agents)
  generated: null,      // generation ts of that response
  cachePresent: true,   // false → hub can't see the delegation cache dir
  summaries: [],        // summary file names from /batches
  selectedBatch: null,
  selectedTask: null,   // {idx}
  runningOnly: false,
  query: '',
  logCache: {},         // 'id:idx' -> full text (loaded so far, kept growing)
  manifest: {},         // 'id' -> manifest object
  expanded: new Set(),
  followOn: false,      // keep-latest-in-view toggle for the open transcript
};

/* search index: 'id:idx' -> full log text, built in the background so the
   top-bar search works across ALL transcripts, not just the open one */
const searchIndex = {};
let searchIndexStale = true;
let searchGen = 0;   // generation guard: a rebuild aborts the previous loop
async function buildSearchIndex() {
  if (!state.batches.length) return;
  const gen = ++searchGen;
  let i = 0, budget = 30e6;                       // audit bound: max ~30 MB
  for (const b of state.batches.slice(0, 80)) {   // and only newest 80 batches
    if (gen !== searchGen || budget <= 0) return; // stale build or budget spent
    for (let idx = 0; idx < b.tasks; idx++) {
      const k = probeKey(b.id, idx);
      i++;
      try {
        const t = await fetchText(API + 'transcript/' + b.id + '/task-' + idx + '.log');
        searchIndex[k] = t; budget -= t.length;
      }
      catch (e) {}
      if (gen !== searchGen || budget <= 0) return;
      if (i % 4 === 0) await new Promise(r => setTimeout(r, 0)); // keep UI responsive
    }
  }
  if (gen === searchGen) searchIndexStale = false;
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && searchIndexStale && state.batches.length) buildSearchIndex();
});

/* running-task probe: 'id:idx' -> true|false (undefined = unknown) */
const running = {};
const probeKey = (id, idx) => id + ':' + idx;

async function fetchText(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
  return r.text();
}

async function fetchTail(url, bytes) {
  const r = await fetch(url, { cache: 'no-store', headers: { Range: 'bytes=-' + bytes } });
  if (r.status === 200) return { rangeOk: false, text: await r.text() };
  if (r.status !== 206) throw new Error(url + ' → HTTP ' + r.status);
  return { rangeOk: true, text: await r.text() };
}

const FINAL_RE = /final\s+\|\s*end status=/;

async function probeTask(id, idx, manifestTask) {
  const key = probeKey(id, idx);
  if (manifestTask) {
    // manifest is authoritative — the log itself can ECHO a "final | end
    // status=" line when a subagent tails another transcript, so a pure
    // tail probe false-positives on that.
    running[key] = manifestTask.status === 'running';
    return;
  }
  try {
    const { text } = await fetchTail(API + 'transcript/' + id + '/task-' + idx + '.log', PROBE_TAIL);
    running[key] = !FINAL_RE.test(text);
  } catch (e) { running[key] = false; }
}

async function loadBatches() {
  let data;
  try {
    data = JSON.parse(await fetchText(API + 'batches'));
  } catch (e) {
    $('phHint').textContent = 'hub /subagent-watch/batches unreachable — ' + e.message;
    return;
  }
  state.cachePresent = data.cache_present !== false;
  state.batches = data.batches || [];
  state.generated = data.generated;
  state.summaries = data.summaries || [];

  if (!state.cachePresent) {
    $('phHint').textContent =
      'No hermes delegation cache found on this machine (~/.hermes/cache/delegation). ' +
      'Subagent transcripts appear here the first time Hermes runs delegate_task.';
  }

  const genMs = Date.parse(state.generated || '');
  for (const b of state.batches) {
    // only probe recent batches (mtime within window OR fresh index)
    if (!isFinite(genMs) || b.mtime * 1000 > Date.now() - RECENT_WINDOW_H * 3600 * 1000) {
      try {
        const man = JSON.parse(await fetchText(API + 'manifest/' + b.id));
        state.manifest[b.id] = man;
        for (const t of (man.tasks || [])) await probeTask(b.id, t.index, t);
      } catch (e) {
        /* manifest missing/corrupt — fall back to tail-probing the logs */
        for (let i = 0; i < b.tasks; i++) await probeTask(b.id, i, null);
      }
    }
  }
  // Main-agent sessions (default + every named profile) live in state.db —
  // fetch independently; a failure just hides the main-session cards.
  await loadMainSessions();
  renderBatches();
  buildSearchIndex();
}

/* Main agents are NOT delegate_task subagents: each profile's top-level
   session streams into its state.db. /main-sessions returns the ones still
   open or active within the last minute, marked live=true when they moved
   in the last 60 s — that's the "is an agent actually working right now"
   signal the Running filter needs. */
async function loadMainSessions() {
  try {
    const d = JSON.parse(await fetchText(API + 'main-sessions'));
    state.mainSessions = (d && d.sessions) || [];
  } catch (e) { state.mainSessions = []; }
}
function mainLiveCount() {
  return state.mainSessions.filter(s => s.live).length;
}

/* time label for a main-session card. Recent sessions read as relative
   ("just now", "42s ago", "3m ago"); anything older gets an absolute
   "MM-DD HH:MM" of its last activity so historical sessions (now that the
   list includes finished ones) stay identifiable. */
function mainSessionTime(s) {
  if (s.last_activity_s === null || s.last_activity_s === undefined) {
    if (s.started_at) return fmtStarted(new Date(s.started_at * 1000).toISOString().replace('T',' ').replace('Z',''));
    return '';
  }
  const sec = s.last_activity_s;
  if (sec < 5) return 'just now';
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.round(sec / 60) + 'm ago';
  // older: absolute local date+time of last activity
  const d = new Date((s.last_activity || s.started_at) * 1000);
  const p2 = n => String(n).padStart(2, '0');
  return p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
}
function batchRunningCount(b) {
  const man = state.manifest[b.id];
  if (!man || !man.tasks) return null;
  return man.tasks.filter(t => running[probeKey(b.id, t.index)]).length;
}

/* ── left panel: batches ─────────────────────────────────────────── */

function fmtStarted(s) {
  if (!s) return '';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  return m ? m[1].slice(2) + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : s;
}

const reprobeTries = {};
function renderBatches() {
  const list = $('batchList');
  list.innerHTML = '';
  const runningCount = state.batches.reduce((n, b) => n + (batchRunningCount(b) || 0), 0);
  const liveMain = mainLiveCount();
  const liveTotal = runningCount + liveMain;
  $('batchStats').textContent = state.batches.length + ' batches' + (state.mainSessions.length ? ' · ' + state.mainSessions.length + ' sessions' : '');
  $('liveCount').hidden = liveTotal === 0;
  $('liveCount').textContent = liveTotal + ' running';

  /* Main-agent sessions first: these are the top-level agents (default +
     named profiles), not delegate_task fan-outs. */
  let anyCard = false;
  for (const s of state.mainSessions) {
    if (state.runningOnly && !s.live) continue;
    anyCard = true;
    const mkey = 'main:' + s.profile + ':' + s.id;
    const sel = state.selectedBatch === mkey;
    const card = el('div', 'batch main' + (sel ? ' sel' : '') + (state.expanded.has(mkey) ? ' open' : ''));
    card.dataset.batch = mkey;
    card.dataset.key = mkey; // for follow-state re-render + per-card button lookup
    const head = el('div', 'batch-head');
    const left = el('div');
    const titleEl = el('div', 'b-title', s.profile === 'default' ? 'agent' : s.profile);
    const agentTag = el('span', 'b-agent ' + (s.profile === 'default' ? 'agent' : 'main'), s.profile === 'default' ? 'main agent' : 'sub-agent');
    titleEl.insertBefore(agentTag, titleEl.firstChild);
    left.appendChild(titleEl);
    // Row 1 (meta): model + msg count (no date here — the date is its own
    // full-width row further down, per the user's layout request).
    const sub = el('div', 'b-sub');
    if (s.model) sub.appendChild(el('span', 'b-model', s.model));
    sub.appendChild(el('span', '', (s.msg_count || 0) + ' msgs'));
    left.appendChild(sub);
    let pill;
    if (s.live) pill = el('span', 'pill running', 'live');
    else pill = el('span', 'pill done', s.ended_at ? 'ended' : 'idle');
    head.appendChild(left); head.appendChild(pill);
    head.appendChild(el('span', 'b-chevron', '▶'));
    head.addEventListener('click', () => openMainSession(s.profile, s.id));
    card.appendChild(head);
    if (s.title) card.appendChild(el('div', 'b-note', s.title.slice(0, 90)));
    // Per-session action row: follow / export, on their own line, ABOVE the
    // date row. stopPropagation so clicking a button doesn't also open the
    // transcript (the whole card is otherwise click-to-open).
    const actions = el('div', 'card-actions');
    const followB = el('button', 'ca-btn', '◉ follow');
    const followOn = isMainFollowOn(s.profile, s.id);
    followB.classList.toggle('on', followOn);
    followB.textContent = followOn ? '◉ following' : '◉ follow';
    followB.title = 'Keep the latest content of this session in view';
    followB.addEventListener('click', (e) => { e.stopPropagation(); toggleMainFollow(s.profile, s.id); });
    const exportB = el('button', 'ca-btn', '⭳ export');
    exportB.title = 'Download this transcript as a .txt file';
    exportB.addEventListener('click', (e) => { e.stopPropagation(); exportMainSession(s.profile, s.id); });
    actions.appendChild(followB); actions.appendChild(exportB);
    card.appendChild(actions);
    // Date row: its own full-width line, below the buttons.
    const t = mainSessionTime(s);
    if (t) card.appendChild(el('div', 'b-time row', t));
    list.appendChild(card);
  }

  /* batches whose manifest is missing or still shows 'running' (the live
     manifest is authoritative and may lag the page load — re-probe with cap) */
  for (const b of state.batches) {
    const staleRunning = state.manifest[b.id] &&
      (state.manifest[b.id].tasks || []).some(t => t.status === 'running');
    if ((!state.manifest[b.id] || staleRunning) && (reprobeTries[b.id] || 0) < 4 &&
        b.mtime * 1000 > Date.now() - 24 * 3600 * 1000) {
      reprobeTries[b.id] = (reprobeTries[b.id] || 0) + 1;
      (async () => {
        try {
          const man = JSON.parse(await fetchText(API + 'manifest/' + b.id));
          state.manifest[b.id] = man;
          for (const t of (man.tasks || [])) await probeTask(b.id, t.index, t);
          renderBatches();
        } catch (e) {
          for (let i = 0; i < b.tasks; i++) await probeTask(b.id, i, null);
          renderBatches();
        }
      })();
    }
  }

  for (const b of state.batches) {
    const man = state.manifest[b.id];
    const rCount = batchRunningCount(b);
    if (state.runningOnly && (rCount === null || rCount === 0)) continue;
    anyCard = true;
    const card = el('div', 'batch' + (state.selectedBatch === b.id ? ' sel' : '') + (state.expanded.has(b.id) ? ' open' : ''));
    card.dataset.batch = b.id;
    const head = el('div', 'batch-head');
    const left = el('div');
    left.appendChild(el('div', 'b-title', b.id.replace(/^deleg_/, '')));
    const sub = el('div', 'b-sub');
    if (b.model) sub.appendChild(el('span', 'b-model', b.model));
    sub.appendChild(el('span', '', b.task_count + ' task' + (b.task_count === 1 ? '' : 's')));
    if (b.started) sub.appendChild(el('span', 'b-time', fmtStarted(b.started)));
    left.appendChild(sub);

    let pill;
    if (rCount !== null && rCount > 0) pill = el('span', 'pill running', rCount + ' live');
    else if (b.completed || (man && man.completed)) pill = el('span', 'pill done', 'done');
    else if (man && man.tasks && man.tasks.length) pill = el('span', 'pill done', 'done');
    else if (man) pill = el('span', 'pill done', 'stale?');
    else pill = el('span', 'pill done', '—');
    head.appendChild(left); head.appendChild(pill);
    head.appendChild(el('span', 'b-chevron', '▶'));
    head.addEventListener('click', () => toggleBatch(b.id));
    card.appendChild(head);
    list.appendChild(card);
  }
  if (!anyCard) list.appendChild(el('div', 'stats', state.runningOnly ? 'no running agents or batches' : 'nothing here yet'));
}

/* Open a main-agent session transcript. mainSessions are single-shot
   (not fan-out batches), so expand just to open the transcript pane. */
async function openMainSession(profile, sid) {
  state.selectedBatch = 'main:' + profile + ':' + sid;
  state.selectedTask = { idx: -1, profile, sid };
  state.expanded.add(state.selectedBatch);
  document.body.classList.add('has-task');
  $('backBtn').hidden = false;
  stopPoll();
  setFollowBtn(false);
  renderBatches();
  await loadMainTranscript(profile, sid, true);
}

/* keep-latest-in-view button visual state (single source of truth) */
function setFollowBtn(on) {
  state.followOn = on;
  if (!followBtn) return;
  followBtn.classList.toggle('on', on);
  followBtn.textContent = on ? '◉ keeping latest in view' : '◉ keep latest in view';
}

/* Per-card follow state: true only when THIS session is the open one AND
   follow mode is on. Only one transcript is open at a time, so at most one
   card's follow button is "on". */
function isMainFollowOn(profile, sid) {
  return !!(state.followOn && state.selectedTask &&
    state.selectedTask.idx === -1 &&
    state.selectedTask.profile === profile && state.selectedTask.sid === sid);
}

/* Clicking the "follow" button on a session card: open that session and keep
   its latest content in view. If it's already the open+followed session, just
   turn follow off. For a live session this also (re)arms the transcript
   poller; for a finished session it just parks the view at the bottom. */
async function toggleMainFollow(profile, sid) {
  if (isMainFollowOn(profile, sid)) {
    setFollowBtn(false); stopPoll();
    renderBatches(); // refresh this card's button back to "◉ follow"
    return;
  }
  await openMainSession(profile, sid);
  // openMainSession awaits loadMainTranscript, so the transcript is on screen
  // now. For a LIVE session loadMainTranscript already armed the poller and
  // turned follow on; for a FINISHED session force follow on + park at the
  // bottom of the transcript.
  if (!state.selectedTask || state.selectedTask.sid !== sid) return;
  setFollowBtn(true);
  scrollTimelineToBottom();
  if (mainFollow && mainFollow.sid === sid && mainFollow.live && !pollTimer) startMainFollow();
  renderBatches(); // refresh this card's button to "◉ following" (on)
}

/* Download a main session's full transcript as .txt, by (profile, sid) —
   independent of which session happens to be open (used by the per-card
   export button). Mirrors the header export button's main-session branch. */
async function exportMainSession(profile, sid) {
  try {
    const d = await (await fetch(API + 'main-transcript/' + encodeURIComponent(profile) + '/' + encodeURIComponent(sid) + '?limit=5000', { cache: 'no-store' })).json();
    const s = state.mainSessions.find(x => x.id === sid && x.profile === profile) || {};
    const text = '# ' + (profile === 'default' ? 'agent' : profile) + ' · ' + sid +
      '\n# ' + (s.title || '') +
      '\n# model: ' + (d.model || s.model || '?') + '   msgs: ' + (s.msg_count != null ? s.msg_count : (d.lines || []).length) +
      '\n# exported: ' + new Date().toISOString() +
      '\n' + (d.lines || []).join('\n');
    const name = 'main-' + profile + '-' + sid + '.txt';
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (e) { alert('Export failed: ' + e.message); }
}

async function toggleBatch(id) {
  if (state.expanded.has(id)) state.expanded.delete(id); else state.expanded.add(id);
  const card = document.querySelector('.batch[data-batch="' + id + '"]');
  card.classList.toggle('open', state.expanded.has(id));
  if (state.expanded.has(id)) {
    const man = state.manifest[id];
    await expandBatch(id, !man || man.tasks.some(t => t.status === 'running'));
  }
}

async function expandBatch(id, force) {
  let man = state.manifest[id];
  if (!man || force) {
    try {
      man = JSON.parse(await fetchText(API + 'manifest/' + id));
      state.manifest[id] = man;
      for (const t of (man.tasks || [])) running[probeKey(id, t.index)] = t.status === 'running';
      renderBatches();
    } catch (e) { if (!man) man = { tasks: [] }; }
  }
  const card = document.querySelector('.batch[data-batch="' + id + '"]');
  let tl = card.querySelector('.task-list');
  if (!tl) { tl = el('div', 'task-list'); card.appendChild(tl); }
  tl.innerHTML = '';

  const b = state.batches.find(x => x.id === id);
  let tasks = (man.tasks || []).slice().sort((a, c) => a.index - c.index);
  if (!tasks.length && b) {
    for (let i = 0; i < b.tasks; i++) tasks.push({ index: i, goal: '' });
  }
  for (const t of tasks.slice(0, 12)) tl.appendChild(taskRow(id, t));
}

function taskRow(id, t) {
  const idx = t.index;
  const key = probeKey(id, idx);
  const row = el('div', 'task-row' + (state.selectedTask && state.selectedBatch === id && state.selectedTask.idx === idx ? ' sel' : ''));
  row.appendChild(el('span', 't-idx', 'task ' + idx));
  const body = el('div', 't-body');
  body.appendChild(el('div', 't-goal', (t.goal || '').slice(0, 180) || 'task ' + idx));
  const foot = el('div', 't-foot');
  const r = running[key];
  foot.appendChild(el('span', 'pill ' + (r === true ? 'running' : 'done'), r === true ? 'live' : (r === false ? 'done' : 'done?')));
  body.appendChild(foot);
  row.appendChild(body);
  row.addEventListener('click', () => openTask(id, idx));
  if (r === false) attachSummarySnippet(body, id, idx);
  return row;
}

/* matched summary file for (batch, task idx): ts must match task's final ts */
function summaryFileFor(id, idx) {
  if (!state.summaries.length) return null;
  const txt = state.logCache[probeKey(id, idx)];
  let finalSecs = null, finalDay = null;
  if (txt) {
    const m = txt.match(/(\d{2}):(\d{2}):(\d{2})\s+final\s+\|\s*end status=/);
    if (m) finalSecs = +m[1] * 3600 + +m[2] * 60 + +m[3];
    const s = txt.match(/^started:\s(\d{4}-\d{2}-\d{2})/m);
    if (s) finalDay = s[1];
  }
  let best = null, bestDist = Infinity;
  for (const f of state.summaries) {
    const m = f.match(/^subagent-summary-(\d+)-(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_\d+\.txt$/);
    if (!m || +m[1] !== idx) continue;
    const secs = +m[5] * 3600 + +m[6] * 60 + +m[7];
    if (finalSecs !== null) {
      let dist = Math.abs(secs - finalSecs);
      if (dist > 43200) dist = 86400 - dist; // midnight wrap
      if (dist <= SUMMARY_WINDOW_S && dist < bestDist) { bestDist = dist; best = f; }
    } else if (!best) best = f;
  }
  return best;
}

function attachSummarySnippet(body, id, idx) {
  const f = summaryFileFor(id, idx);
  if (!f) return;
  fetchText(API + 'summary/' + encodeURIComponent(f)).then(s => {
    body.appendChild(el('div', 't-sum', s.replace(/\s+/g, ' ').slice(0, 140) + '…'));
  }).catch(() => {});
}

/* ── right panel: task transcript ───────────────────────────────── */

let pollTimer = null, lastText = '', lastGrow = 0, lastUrl = '', lastEvTs = null;

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  showPollDot(false);
}

async function openTask(id, idx) {
  state.selectedBatch = id;
  state.selectedTask = { idx };
  document.body.classList.add('has-task');
  $('backBtn').hidden = false;
  stopPoll();
  renderBatches();

  const url = API + 'transcript/' + id + '/task-' + idx + '.log';
  $('placeholder').hidden = true;
  const view = $('taskView');
  view.hidden = false;
  $('timeline').innerHTML = '';
  $('summaryBox').hidden = true;
  $('searchHits').hidden = true;
  $('taskTitle').textContent = id + ' · task ' + idx;
  $('taskStatus').className = 'pill';
  $('taskStatus').textContent = 'loading…';
  $('taskElapsed').textContent = '';
  $('taskMeta').textContent = '';

  let text;
  try { text = await fetchText(url); }
  catch (e) {
    $('taskStatus').textContent = 'load failed';
    $('taskStatus').className = 'pill failed';
    $('taskGoal').textContent = e.message;
    return;
  }
  state.logCache[probeKey(id, idx)] = text;
  paintTask(id, idx, text);
  if (running[probeKey(id, idx)] !== false) {
    startFollow(id, idx, url, text);
    setFollowBtn(true);
  } else setFollowBtn(false);
}

const LINE_RE = /^(\d{2}):(\d{2}):(\d{2})\s+(user|start|tool|result|think|final)\s+\|\s?([\s\S]*)$/;

/* parse complete lines from text; returns {events, consumed} where consumed =
   offset (in text) up to end of last complete line */
function eventLines(text, fromLen) {
  const out = [];
  const s = text.slice(fromLen);
  let start = 0;
  if (s.length && !LINE_RE.test(s)) {
    const nl = s.indexOf('\n');
    if (nl < 0) return { events: [], consumed: 0 };
    start = nl + 1;
  }
  const lines = s.slice(start).split('\n');
  let consumed = start;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const m = LINE_RE.exec(line);
    if (m) out.push({ ts: m[1] + ':' + m[2] + ':' + m[3], kind: m[4], text: m[5] });
    consumed += line.length + 1;
    if (i === lines.length - 1) consumed -= 1; // no trailing \n on last piece
  }
  return { events: out, consumed: consumed > fromLen ? consumed : 0 };
}

function parseHeader(text) {
  const g = text.match(/^goal:\s?([\s\S]*?)\nstarted:/m);
  const s = text.match(/^started:\s(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/m);
  return {
    goal: g ? g[1].trim().replace(/\n/g, ' ') : '',
    started: s ? s[1] : null,
  };
}

/* All log timestamps are WALL CLOCK in the server's local tz. The browser's
   tz can differ (it did — server EEST, browser LA), so NEVER mix Date.now()
   with parsed log times. Elapsed is computed inside the log's own frame:
   started(HH:MM:SS, full date in header) -> last event HH:MM:SS, with
   midnight-wrap handled against the known start date. */
function wallSecs(ts) { const p = ts.split(':').map(Number); return p[0] * 3600 + p[1] * 60 + p[2]; }

function elapsedFromLog(startedHdr, lastEventTs, batchStarted, done) {
  const hdr = parseHeaderLike(startedHdr, batchStarted);
  if (!hdr || !hdr.startSecs) return null;
  if (lastEventTs) {
    let s = wallSecs(lastEventTs) - hdr.startSecs;
    if (s < -43200) s += 86400;            // midnight wrap (event on day+1)
    return s < 0 ? 0 : s;
  }
  return null;
}

/* parse "YYYY-MM-DD HH:MM:SS" OR "HH:MM:SS" (+ batch date fallback) */
function parseHeaderLike(startedHdr, batchStarted) {
  if (!startedHdr) return null;
  let d;
  const full = startedHdr.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
  const hh = startedHdr.match(/^(\d{2}):(\d{2}):(\d{2})/);
  if (full) d = { date: full.slice(1, 4), startSecs: wallSecs(full[4] + ':' + full[5] + ':' + full[6]) };
  else if (hh && batchStarted) {
    const bd = batchStarted.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (bd) d = { date: bd.slice(1, 4), startSecs: wallSecs(hh[1] + ':' + hh[2] + ':' + hh[3]) };
  }
  return d;
}

function fmtDur(secs) {
  if (secs < 0) secs = 0;
  const h = Math.floor(secs / 3600), m = Math.floor(secs % 3600 / 60), s = Math.floor(secs % 60);
  return (h ? h + 'h ' : '') + m + 'm ' + String(s).padStart(2, '0') + 's';
}

function prettyJsonish(t) {
  const s = t.trim();
  if (s[0] === '{' || s[0] === '[') {
    try { return JSON.stringify(JSON.parse(s), null, 1); } catch (e) { /* not strict json */ }
  }
  return t;
}

function makeEvent(ev) {
  const d = el('div', 'ev');
  /* Title row: timestamp + type as a left-aligned caption ABOVE the content.
     Previously time/type sat in a fixed 58px+64px left gutter next to the
     content, so the text itself started ~132px in and wrapped with that dead
     gutter — on a narrow window that wasted a third of the width. A compact
     "02:21:28 · TOOL" line up top left-aligns with the content and lets the
     content use the full width below it. */
  const head = el('div', 'ev-title');
  head.appendChild(el('span', 'ts', ev.ts));
  head.appendChild(el('span', 'kind ' + ev.kind, ev.kind));
  d.appendChild(head);
  const body = el('div', 'body');
  const raw = ev.text;

  if (ev.kind === 'tool') {
    const m = raw.match(/^->\s*([A-Za-z_][\w.-]*)\s*(?:\(([\s\S]*)\))?$/);
    const box = el('div', 'call');
    const head = el('div', 'call-head');
    head.appendChild(el('span', 'name', m ? m[1] : raw.slice(0, 24)));
    const argSummary = m && m[2] ? m[2] : '';
    head.appendChild(el('span', 'arg', argSummary.length > 96 ? argSummary.slice(0, 96) + '…' : argSummary));
    head.appendChild(el('span', 'caret', '▶'));
    head.addEventListener('click', () => box.classList.toggle('open'));
    box.appendChild(head);
    if (argSummary) {
      const pre = el('pre', 'mono');
      pre.textContent = prettyJsonish(argSummary);
      const args = el('div', 'call-args');
      args.appendChild(pre);
      box.appendChild(args);
    }
    body.appendChild(box);
  } else if (ev.kind === 'result') {
    const m = raw.match(/^(.*?)\s+(ok|fail(?:ed)?|error)\s+([\d.]+)s:\s?([\s\S]*)$/i) ||
              raw.match(/^(.*?)\s+([\d.]+)s:\s?([\s\S]*)$/);
    let head, content;
    if (m) {
      head = m[1] + ' · ' + m[2] + ' · ' + m[3] + 's';
      content = m[4];
    } else { head = raw.slice(0, 60); content = raw; }
    const chip = el('div', 'res-chip', head);
    body.appendChild(chip);
    const capped = content.length > 1400;
    const pre = el('pre', 'mono res' + (capped ? ' capped' : ''));
    pre.textContent = capped ? content.slice(0, 1400) : content;
    body.appendChild(pre);
    if (capped) {
      const b = el('button', 'expand-btn', 'show all ' + content.length.toLocaleString() + ' chars');
      b.addEventListener('click', () => {
        pre.textContent = content;
        pre.classList.remove('capped');
        b.remove();
      }, { once: true });
      body.appendChild(b);
    }
  } else if (ev.kind === 'think') {
    const t = el('div', 'b-think');
    const capped = raw.length > 1600;
    t.textContent = capped ? raw.slice(0, 1600) : raw;
    if (capped) {
      const b = el('button', 'expand-btn', 'show all ' + raw.length.toLocaleString() + ' chars');
      b.addEventListener('click', () => { t.textContent = raw; b.remove(); }, { once: true });
      t.appendChild(document.createTextNode(' '));
      t.appendChild(b);
    }
    body.appendChild(t);
  } else if (ev.kind === 'final') {
    const pre = el('pre', 'mono');
    pre.style.cssText = 'max-height:420px;overflow:auto';
    pre.textContent = raw.length > 6000 ? raw.slice(0, 6000) + '\n…[truncated]' : raw;
    const f = el('div', 'b-final');
    f.appendChild(pre);
    body.appendChild(f);
  } else {
    const t = el('div', 'b-text');
    t.style.color = ev.kind === 'start' ? 'var(--faint)' : 'var(--ink)';
    t.textContent = raw.length > 4000 ? raw.slice(0, 4000) + '…' : raw;
    body.appendChild(t);
  }
  d.appendChild(body);
  return d;
}

function taskDoneState(id, idx, text) {
  // 1) manifest authoritative when present
  const t = (state.manifest[id] || {}).tasks &&
    (state.manifest[id].tasks.find(x => x.index === idx));
  if (t && t.status !== 'running') return t.status;
  // 2) log convention: a FINISHED log always ENDS with "HH:MM:SS final | end status=X".
  //    Match only the LAST line — a subagent may ECHO another transcript's final
  //    line inside a tool result mid-file, which must not count.
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue;
    const m = lines[i].match(/^(\d{2}:\d{2}:\d{2})\s+final\s+\|\s*end status=(\w+)/);
    if (m) return m[2];
    break;
  }
  return null;
}

function lastEvTsFrom(text) {
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue;
    const m = lines[i].match(/^(\d{2}:\d{2}:\d{2})\s+final\s+\|\s*end status=/);
    if (m) return m[1];
    break;
  }
  return null;
}

function paintTask(id, idx, text) {
  const tl = $('timeline');
  tl.innerHTML = '';
  const hdr = parseHeader(text);
  const man = state.manifest[id];
  const goal = hdr.goal || ((man && man.tasks && man.tasks.find(t => t.index === idx) || {}).goal) || '';
  $('taskGoal').textContent = goal;
  const b = state.batches.find(x => x.id === id);
  const metaBits = [];
  if (b && b.model) metaBits.push(b.model);
  if (hdr.started) metaBits.push('started ' + hdr.started);
  $('taskMeta').textContent = metaBits.join('  ·  ');

  const { events } = eventLines(text, 0);
  const frag = document.createDocumentFragment();
  for (const ev of events) frag.appendChild(makeEvent(ev));
  tl.appendChild(frag);

  const doneState = taskDoneState(id, idx, text);
  if (doneState) {
    $('taskStatus').className = 'pill ' + (doneState === 'completed' ? 'done' : 'failed');
    $('taskStatus').textContent = doneState;
  } else {
    $('taskStatus').className = 'pill running';
    $('taskStatus').textContent = 'running';
  }
  lastEvTs = lastEvTsFrom(text) || (events.length ? events[events.length - 1].ts : null);
  updateElapsed(hdr.started, b ? b.started : null);
  if (doneState) renderSummary(id, idx, text);
  state.logCache[probeKey(id, idx)] = text;
  lastText = text;
  lastGrow = Date.now();
}

function updateElapsed(startedHdr, batchStarted) {
  const secs = elapsedFromLog(startedHdr, lastEvTs, batchStarted, false);
  $('taskElapsed').textContent = secs === null ? '' : 'elapsed ' + fmtDur(secs);
}

function showPollDot(on) {
  let d = $('pollDot');
  if (!d) {
    d = el('span', null, '● live');
    d.id = 'pollDot';
    d.style.cssText = 'font-size:10px;font-weight:800;letter-spacing:.12em;color:var(--ok);animation:pulse 1.4s infinite;margin-left:10px';
    $('taskMeta').appendChild(d);
  }
  d.style.display = on ? '' : 'none';
}

async function renderSummary(id, idx, text) {
  const f = summaryFileFor(id, idx);
  let content, src;
  if (f) {
    try { content = await fetchText(API + 'summary/' + encodeURIComponent(f)); src = f; } catch (e) { content = null; }
  }
  if (!content) {
    content = text.split('\n').slice(-40).join('\n');
    src = 'last 40 lines of task-' + idx + '.log';
  }
  $('summaryBox').hidden = false;
  $('summaryPre').textContent = content;
  let srcEl = $('summaryBox').querySelector('.sum-src');
  if (!srcEl) { srcEl = el('div', 'sum-src'); $('summaryBox').appendChild(srcEl); }
  srcEl.textContent = 'source: ' + src;
}

/* ── main-agent transcripts (state.db) ───────────────────────────── */
/* /main-transcript returns {lines:[...]} in the SAME "HH:MM:SS kind | body"
   shape as subagent task logs, so the existing eventLines/makeEvent
   pipeline renders them unchanged. No "final | end status" line ever
   appears — liveness comes from the session's last_activity instead. */
let mainFollow = null; // {profile, sid, url, count, live}

async function loadMainTranscript(profile, sid, fresh) {
  const s = state.mainSessions.find(x => x.id === sid && x.profile === profile) || {};
  const url = API + 'main-transcript/' + encodeURIComponent(profile) + '/' + encodeURIComponent(sid);
  $('placeholder').hidden = true;
  const view = $('taskView');
  view.hidden = false;
  if (fresh) $('timeline').innerHTML = '';
  $('taskTitle').textContent = (profile === 'default' ? 'agent' : profile) + ' · ' + sid;
  $('taskStatus').className = s.live ? 'pill running' : 'pill done';
  $('taskStatus').textContent = s.live ? 'live' : (s.ended_at ? 'ended' : 'idle');
  $('taskGoal').textContent = s.title || (s.last_activity_desc || '');
  const meta = [];
  if (s.model) meta.push(s.model);
  meta.push((s.msg_count || 0) + ' msgs');
  if (s.last_activity_s !== null && s.last_activity_s !== undefined) meta.push('active ' + (s.last_activity_s < 5 ? 'just now' : s.last_activity_s + 's ago'));
  $('taskMeta').textContent = meta.join('  ·  ');

  let data;
  try { data = await (await fetch(url, { cache: 'no-store' })).json(); }
  catch (e) {
    $('taskStatus').textContent = 'load failed';
    $('taskStatus').className = 'pill failed';
    $('taskGoal').textContent = e.message;
    return;
  }
  state.logCache['main:' + profile + ':' + sid] = (data.lines || []).join('\n');
  const tl = $('timeline');
  const frag = document.createDocumentFragment();
  for (const ev of eventLines((data.lines || []).join('\n'), 0).events) frag.appendChild(makeEvent(ev));
  /* Re-loading a main transcript REPLACES the events (the server always
     returns the current tail), so clear first — but ONLY when switching
     sessions, not when the periodic main-sessions refresh re-pulls the same
     one: wiping the timeline mid-read would reset the user's scroll
     position every 8 s. */
  if (fresh || !mainFollow || mainFollow.sid !== sid) tl.innerHTML = '';
  tl.appendChild(frag);
  mainFollow = { profile, sid, url, count: (data.lines || []).length, live: !!s.live };
  scrollTimelineToBottom();
  if (mainFollow.live) { startMainFollow(); setFollowBtn(true); }
}

function startMainFollow() {
  stopPoll();
  let slow = false, lastGrow = Date.now();
  const tick = async () => {
    if (document.hidden || !mainFollow) return;
    if (!state.selectedTask || state.selectedTask.sid !== mainFollow.sid) return;
    let data;
    try { data = await (await fetch(mainFollow.url, { cache: 'no-store' })).json(); } catch (e) { return; }
    const lines = data.lines || [];
    state.logCache['main:' + mainFollow.profile + ':' + mainFollow.sid] = lines.join('\n');
    if (lines.length > mainFollow.count) {
      const add = eventLines(lines.slice(mainFollow.count).join('\n'), 0).events;
      for (const ev of add) $('timeline').appendChild(makeEvent(ev));
      mainFollow.count = lines.length;
      lastGrow = Date.now();
      /* Only auto-scroll while keep-latest-in-view is on; otherwise the user
         is reading history and a background re-fetch must not yank the view. */
      if (state.followOn) scrollTimelineToBottom();
      if (slow) { slow = false; clearInterval(pollTimer); pollTimer = setInterval(tick, POLL_MS); }
    } else if (lines.length < mainFollow.count) {
      mainFollow.count = 0;
      $('timeline').innerHTML = '';
      await loadMainTranscript(mainFollow.profile, mainFollow.sid, true);
      return;
    } else if (!slow && Date.now() - lastGrow > 20000) {
      slow = true;
      clearInterval(pollTimer);
      pollTimer = setInterval(tick, SLOW_POLL_MS);
    }
    // refresh the live/idle pill from the newest main-sessions snapshot
    const s = state.mainSessions.find(x => x.id === mainFollow.sid && x.profile === mainFollow.profile);
    if (s) {
      mainFollow.live = !!s.live;
      $('taskStatus').className = s.live ? 'pill running' : 'pill done';
      $('taskStatus').textContent = s.live ? 'live' : (s.ended_at ? 'ended' : 'idle');
    }
    if (!mainFollow.live && Date.now() - lastGrow > STALE_STOP_MS) {
      stopPoll();
      mainFollow.live = false;
    }
  };
  pollTimer = setInterval(tick, POLL_MS);
  showPollDot(true);
}

function getTimelineScroller() {
  /* #timeline itself is NOT the scroll container — #mainPanel scrolls and
     the events flow inside it. Walking up from the timeline to the nearest
     scrollable ancestor is what actually moves the view; setting
     timeline.scrollTop was a no-op, which is why "keep latest in view"
     looked dead while the user sat at the top of a long transcript. */
  const tl = $('timeline');
  if (!tl) return null;
  let sc = tl;
  while (sc && sc !== document.documentElement) {
    const oy = getComputedStyle(sc).overflowY;
    if (sc.scrollHeight > sc.clientHeight + 4 && /auto|scroll/.test(oy)) break;
    sc = sc.parentElement;
  }
  return sc;
}
function scrollTimelineToBottom() {
  const sc = getTimelineScroller();
  if (sc) sc.scrollTop = sc.scrollHeight;
}
function timelineAtBottom(thresholdPx) {
  const sc = getTimelineScroller();
  if (!sc) return true;
  return sc.scrollHeight - sc.scrollTop - sc.clientHeight < (thresholdPx || 80);
}

/* ── auto-follow ─────────────────────────────────────────────────── */

function startFollow(id, idx, url, text) {
  stopPoll();
  lastText = text;
  lastUrl = url;
  lastGrow = Date.now();
  const key = probeKey(id, idx);
  let slow = false;

  const tick = async () => {
    if (document.hidden) return;                 // mobile: pause when tab hidden
    if (state.selectedTask.idx !== idx || state.selectedBatch !== id) return;
    let t;
    try { t = await fetchText(lastUrl); } catch (e) { return; }

    if (taskDoneState(id, idx, t)) {
      state.logCache[key] = t;
      running[key] = false;
      stopPoll();
      paintTask(id, idx, t);
      renderBatches();
      return;
    }
    if (t.length > lastText.length) {
      const lastNl = t.lastIndexOf('\n');
      if (lastNl >= lastText.length) {
        const add = t.slice(lastText.length, lastNl + 1);
        appendEvents(add);
        lastText = t.slice(0, lastNl + 1);
        state.logCache[key] = lastText;
        lastGrow = Date.now();
        const hdr0 = lastText.match(/^started:\s(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/m);
        const b0 = state.batches.find(x => x.id === id);
        const addEv = eventLines(add, 0);
        lastEvTs = addEv.events.length ? addEv.events[addEv.events.length - 1].ts : lastEvTs;
        updateElapsed(hdr0 ? hdr0[1] : (b0 && b0.started), b0 && b0.started);
      }
      if (slow) {
        slow = false;
        clearInterval(pollTimer);
        pollTimer = setInterval(tick, POLL_MS);
      }
      return;
    }
    if (t.length < lastText.length) {
      lastText = t; // truncated/rotated — re-paint
      state.logCache[key] = t;
      paintTask(id, idx, t);
      return;
    }
    // no growth: if stalled 60s AND a summary exists → stop; else slow down
    if (Date.now() - lastGrow > STALE_STOP_MS && summaryFileFor(id, idx)) {
      stopPoll();
      running[key] = false;
      $('taskStatus').className = 'pill done';
      $('taskStatus').textContent = 'idle (no growth)';
      renderBatches();
      return;
    }
    if (!slow && Date.now() - lastGrow > 20000) {
      slow = true;
      clearInterval(pollTimer);
      pollTimer = setInterval(tick, SLOW_POLL_MS);
    }
  };
  pollTimer = setInterval(tick, POLL_MS);
  showPollDot(true);
}

function appendEvents(chunk) {
  const { events } = eventLines(chunk, 0);
  const tl = $('timeline');
  const atBottom = timelineAtBottom(80);
  const frag = document.createDocumentFragment();
  for (const ev of events) frag.appendChild(makeEvent(ev));
  tl.appendChild(frag);
  /* keep-latest-in-view ON => the user asked to see the newest content, so
     scroll is unconditional even when they were reading history. Without
     this, enabling follow from the top of a long transcript left the view
     parked at the top while new lines piled up at the bottom. */
  if (atBottom || state.followOn) scrollTimelineToBottom();
}

/* ── theme toggle (LIMCARS light/dark; follows ?theme= on load) ───── */
const themeBtn = $('themeBtn');
if (themeBtn) {
  const paintTheme = () => {
    const lt = document.documentElement.classList.contains('lt');
    themeBtn.textContent = lt ? '☾' : '◐';
    themeBtn.title = lt ? 'Switch to dark' : 'Toggle light / dark';
  };
  paintTheme();
  themeBtn.addEventListener('click', () => {
    const lt = document.documentElement.classList.toggle('lt');
    try { localStorage.setItem('sw-theme', lt ? 'light' : 'dark'); } catch (e) {}
    paintTheme();
  });
}

/* ── close button (embedded in the dash only; tells the parent to fold) ── */
const closeBtn = $('closeBtn');
if (closeBtn && window.top !== window.self) {
  closeBtn.hidden = false;
  closeBtn.addEventListener('click', () => {
    window.parent.postMessage('sw-close', '*');
  });
}

/* ── top bar: search + running toggle + refresh + back ──────────── */

let searchTimer = null;
$('searchBox').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = $('searchBox').value.trim().toLowerCase();
    applySearch();
  }, 300);
});
$('runningOnly').addEventListener('change', () => {
  state.runningOnly = $('runningOnly').checked;
  renderBatches();
  applySearch();
});
/* keep-latest-in-view: sticky auto-follow for the open transcript. Subagent
   logs re-enter startFollow (appends + auto-scrolls while at bottom); main
   sessions re-enter startMainFollow. Re-clicking detaches and re-arms. */
if (followBtn) {
  followBtn.addEventListener('click', async () => {
    const on = !state.followOn;
    setFollowBtn(on);
    if (!on) { stopPoll(); return; }
    if (!state.selectedTask) return;
    const st = state.selectedTask;
    if (st.idx === -1 && st.sid) {
      if (mainFollow && mainFollow.sid === st.sid) {
        /* Same session already open: re-arm the poller, don't re-fetch —
           re-loading would DUPLICATE the events already on screen. */
        startMainFollow();
      } else {
        await loadMainTranscript(st.profile, st.sid, true);
      }
    } else {
      startFollow(state.selectedBatch, st.idx, API + 'transcript/' + state.selectedBatch + '/task-' + st.idx + '.log', state.logCache[probeKey(state.selectedBatch, st.idx)] || '');
    }
    setTimeout(scrollTimelineToBottom, 50);
  });
}

/* Export: download the open transcript as .txt. Main sessions re-fetch the
   FULL transcript (limit=5000 — the live view only keeps a tail) so the
   export isn't truncated by what happened to render; subagent logs export
   the cached file text verbatim (that IS the complete log). */
const exportBtn = $('exportBtn');
if (exportBtn) {
  exportBtn.addEventListener('click', async () => {
    if (!state.selectedTask) return;
    const st = state.selectedTask;
    let text = '', name = 'transcript';
    try {
      if (st.idx === -1 && st.sid) {
        const d = await (await fetch(API + 'main-transcript/' + encodeURIComponent(st.profile) + '/' + encodeURIComponent(st.sid) + '?limit=5000', { cache: 'no-store' })).json();
        const s = state.mainSessions.find(x => x.id === st.sid && x.profile === st.profile) || {};
        text = '# ' + (st.profile === 'default' ? 'agent' : st.profile) + ' · ' + st.sid +
          '\n# ' + (s.title || '') +
          '\n# model: ' + (d.model || s.model || '?') + '   msgs: ' + (s.msg_count != null ? s.msg_count : (d.lines || []).length) +
          '\n# exported: ' + new Date().toISOString() +
          '\n' + (d.lines || []).join('\n');
        name = 'main-' + st.profile + '-' + st.sid + '.txt';
      } else {
        text = state.logCache[probeKey(state.selectedBatch, st.idx)] || '';
        if (!text) text = await fetchText(API + 'transcript/' + state.selectedBatch + '/task-' + st.idx + '.log');
        name = state.selectedBatch + '-task-' + st.idx + '.log';
      }
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) {
      alert('Export failed: ' + e.message);
    }
  });
}
$('refreshBtn').addEventListener('click', async () => {
  stopPoll();
  for (const k of Object.keys(running)) delete running[k];
  searchIndexStale = true;
  for (const k of Object.keys(searchIndex)) delete searchIndex[k];
  await loadBatches();
  if (state.selectedBatch) {
    if (state.selectedTask && state.selectedTask.idx === -1 && state.selectedTask.sid) {
      await loadMainTranscript(state.selectedTask.profile, state.selectedTask.sid, true);
    } else {
      await expandBatch(state.selectedBatch);
      if (state.selectedTask) await openTask(state.selectedBatch, state.selectedTask.idx);
    }
  }
});
$('backBtn').addEventListener('click', () => {
  document.body.classList.remove('has-task');
  $('backBtn').hidden = true;
  $('placeholder').hidden = false;
  $('taskView').hidden = true;
  state.selectedTask = null;
  mainFollow = null;
  stopPoll();
  renderBatches();
});

function applySearch() {
  const q = state.query;
  document.querySelectorAll('.batch').forEach(card => {
    const bid = card.dataset.batch;
    const man = state.manifest[bid];
    const batchText = !q ? '' : ([
      bid,
      man ? (man.model || '') : '',
      (state.logCache[probeKey(bid, 0)] || '')
    ].join(' ').toLowerCase());
    card.querySelectorAll('.task-row').forEach(row => {
      const g = row.querySelector('.t-goal');
      if (!g) return;
      const idx = (row.querySelector('.t-idx') || {}).textContent || '';
      let rowHit = !q;
      if (q) {
        const t = man && man.tasks && man.tasks.find(x => 'task ' + x.index === idx);
        const logTxt = searchIndex[probeKey(bid, parseInt(idx.replace('task ', ''), 10))] || '';
        const txt = ((t && t.goal ? t.goal : g.textContent) + ' ' + logTxt).toLowerCase();
        rowHit = txt.includes(q) || batchText.includes(q);
      }
      row.style.display = rowHit ? '' : 'none';
      if (q && rowHit && g.querySelector('.t-goal')) {
        const full = g.textContent;
        const i = full.toLowerCase().indexOf(q);
        g.innerHTML = '';
        if (i >= 0) {
          g.append(full.slice(0, i));
          const mk = document.createElement('mark');
          mk.textContent = full.slice(i, i + q.length);
          g.append(mk);
          g.append(full.slice(i + q.length));
        } else g.append(full);
      }
    });
    if (q) {
      let any = false;
      card.querySelectorAll('.task-row').forEach(rw => { if (!rw.style.display) any = true; });
      card.style.display = any ? '' : 'none';
    } else card.style.display = '';
  });

  const box = $('searchHits');
  if (q) {
    const idxDone = !searchIndexStale;
    let total = 0; const samples = [];
    for (const [k, txt] of Object.entries(searchIndex)) {
      const n = txt.split('\n').filter(l => l.toLowerCase().includes(q)).length;
      total += n;
      if (n && samples.length < 5) {
        const [id, i] = k.split(':');
        samples.push(txt.split('\n').filter(l => l.toLowerCase().includes(q))[0].slice(0, 140) + '  [ ' + id.slice(6) + ' · task ' + i + ' ]');
      }
    }
    if (!idxDone) {
      box.innerHTML = '';
      box.append(el('span', 'hits', 'indexing all transcripts…'));
      box.hidden = false;
      return;
    }
    if (total) {
      box.innerHTML = '';
      const h = el('div', '', '');
      h.append('matches across transcripts: ');
      const s2 = el('span', 'hits', total);
      h.appendChild(s2);
      box.appendChild(h);
      for (const l of samples) box.appendChild(el('span', 'h', l));
      box.hidden = false;
      return;
    }
  }
  if (q && state.selectedTask) {
    const txt = state.logCache[probeKey(state.selectedBatch, state.selectedTask.idx)] || '';
    const lines = txt.split('\n').filter(l => l.toLowerCase().includes(q));
    if (lines.length) {
      box.innerHTML = '';
      const h = el('div', '', '');
      h.append('open transcript: ');
      const s = el('span', 'hits', lines.length + ' line' + (lines.length === 1 ? '' : 's'));
      h.appendChild(s);
      box.appendChild(h);
      for (const l of lines.slice(0, 6)) {
        const li = el('span', 'h', l.length > 160 ? l.slice(0, 160) + '…' : l);
        box.appendChild(li);
      }
      box.hidden = false;
    } else box.hidden = true;
  } else box.hidden = true;
}

/* re-render elapsed clock while a running task is open (subagent logs only —
   main-session liveness is driven by /main-sessions, not log timestamps) */
setInterval(() => {
  if (!state.selectedTask || document.hidden) return;
  if (state.selectedTask.idx === -1) return;
  const st = $('taskStatus');
  if (st.classList.contains('done') || st.classList.contains('failed')) return;
  const txt = state.logCache[probeKey(state.selectedBatch, state.selectedTask.idx)] || '';
  const hdr = parseHeader(txt);
  const b = state.batches.find(x => x.id === state.selectedBatch);
  const evs = eventLines(txt, 0).events;
  lastEvTs = lastEvTsFrom(txt) || (evs.length ? evs[evs.length - 1].ts : null);
  updateElapsed(hdr.started, b ? b.started : null);
}, 5000);

/* keep the main-session cards fresh: re-pull /main-sessions on an interval so
   live/idle pills + "N s ago" update without a full refresh. Cheap (a couple
   of read-only SQLite queries) and pauses when the tab is hidden. */
setInterval(async () => {
  if (document.hidden) return;
  await loadMainSessions();
  renderBatches();
}, 8000);

loadBatches();
})();
