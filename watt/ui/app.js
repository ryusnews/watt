/* WATT 런처 화면. Python 쪽: window.pywebview.api (watt/app.py 의 Api). 브라우저로 열면 가짜 API(미리보기)로 돈다. */
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const setHTML = (el, html) => { if (el._h !== html) { el.innerHTML = html; el._h = html; } };
const icon = (id, cls = '') => `<svg class="ic ${cls}"><use href="#${id}"/></svg>`;

const S = { api: null, state: null, live: null, games: null, terms: null, view: 'home', step: 0, feedLang: 'all', termCat: 'all',
  selModel: null, selGame: null, progress: {}, results: {}, starting: 0, regionPreview: null };

/* ---------- API ---------- */
function getApi() {
  // 가짜 API 는 주소에 ?mock 이 있을 때만(브라우저 미리보기). 앱에서는 파이썬 연결을 끝까지 기다린다 — exe 는 연결이 1초 넘게 걸린다
  if (new URLSearchParams(location.search).has('mock')) return Promise.resolve(mockApi());
  return new Promise((resolve) => {
    const ready = () => window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_state === 'function';
    if (ready()) return resolve(window.pywebview.api);
    window.addEventListener('pywebviewready', () => resolve(window.pywebview.api), { once: true });
    const t = setInterval(() => { if (ready()) { clearInterval(t); resolve(window.pywebview.api); } }, 200);
  });
}
async function call(name, ...args) {
  try { return await S.api[name](...args); }
  catch (e) { toast(`${name}: ${e.message || e}`, 'err'); throw e; }
}

/* 파이썬 → 화면 */
window.WATT = {
  onEvent(ev) {
    if (ev.type === 'progress') { S.progress[ev.task] = ev; renderSetup(); renderEngine(); renderUpdate(); if (ev.task === 'ai') renderAi(); if (ev.task === 'move') renderModelsDir(); return; }
    if (ev.type === 'done') {
      delete S.progress[ev.task];
      if (!ev.ok) { toast(ev.error || '실패했습니다', 'err'); S.results[ev.task] = { error: ev.error }; }
      else {
        S.results[ev.task] = ev.result;
        if (ev.task === 'region' && ev.result) { S.regionPreview = ev.result.preview; toast('채팅 영역을 찾았습니다', 'ok'); }
        if (ev.task === 'pull') { toast('모델을 받았습니다', 'ok'); S.models = null; }
        if (ev.task === 'move') { toast('모델 폴더를 옮겼습니다', 'ok'); S.models = null; call('models_info').then((x) => { S.models = x; renderModelsDir(); }); }
        if (ev.task === 'ocr') toast('글자 인식 언어 팩을 확인했습니다', 'ok');
        if (ev.task === 'ollama') toast('AI 실행기를 확인했습니다', 'ok');
        if (ev.task === 'cleanup') toast('WATT 가 설치한 것을 정리했습니다', 'ok');
        if (ev.task === 'ai') { toast('AI 글자 인식 모델을 받았습니다', 'ok'); call('get_ai').then((x) => { S.ai = x; refresh(true); }); }
        if (ev.task === 'update' && ev.result && ev.result.opened) toast('릴리스 페이지를 열었습니다');
        if (ev.task === 'update' && ev.result && ev.result.ready) { if (S.update) S.update.ready = ev.result.ready; askRestart(ev.result.ready); }
      }
      if (ev.task === 'ai') renderAi();
      refresh(true);
    }
  },
};

/* ---------- 창 크기 — 테두리 없는 창이라 가장자리 손잡이와 최대화를 직접 둔다(좌표는 물리 픽셀) ---------- */
async function toggleMax() {
  const maxed = await call('toggle_maximize');
  document.body.classList.toggle('maxed', !!maxed);
  $('#max-icon').setAttribute('href', maxed ? '#i-restore' : '#i-max');
  $('#btn-max').setAttribute('aria-label', maxed ? '이전 크기로' : '최대화');
}
function setupResize() {
  for (const e of ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']) {
    const d = document.createElement('div'); d.className = `rz rz-${e}`; d.dataset.edge = e; d.setAttribute('aria-hidden', 'true'); document.body.appendChild(d);
  }
  let drag = null, busy = false, next = null;
  const send = async () => {  // 앞 요청이 끝나야 다음 것을 보낸다 — 끌기가 밀리지 않게 마지막 것만
    if (busy || !next) return;
    busy = true; const r = next; next = null;
    try { await S.api.set_window_rect(r.x, r.y, r.w, r.h); } catch (e) { /* 창이 닫히는 중 */ }
    busy = false; send();
  };
  document.addEventListener('pointerdown', async (ev) => {
    const el = ev.target.closest('.rz');
    if (!el || document.body.classList.contains('maxed') || !S.api.get_window_rect) return;
    ev.preventDefault(); el.setPointerCapture(ev.pointerId);
    const start = { edge: el.dataset.edge, x0: ev.screenX, y0: ev.screenY };
    start.r = await S.api.get_window_rect(); drag = start;
  });
  document.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const k = window.devicePixelRatio || 1, e = drag.edge, r = drag.r;
    const dx = (ev.screenX - drag.x0) * k, dy = (ev.screenY - drag.y0) * k, minW = 760 * k, minH = 560 * k;
    let { x, y, w, h } = r;
    if (e.includes('e')) w = Math.max(minW, r.w + dx);
    if (e.includes('s')) h = Math.max(minH, r.h + dy);
    if (e.includes('w')) { w = Math.max(minW, r.w - dx); x = r.x + r.w - w; }
    if (e.includes('n')) { h = Math.max(minH, r.h - dy); y = r.y + r.h - h; }
    next = { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) }; send();
  });
  const end = () => { if (drag) { drag = null; setTimeout(() => S.api.save_window_rect && S.api.save_window_rect(), 300); } };
  document.addEventListener('pointerup', end); document.addEventListener('pointercancel', end);
  // 창을 옮긴 뒤(제목 막대 끌기)에도 위치를 기억
  $$('.pywebview-drag-region').forEach((d) => d.addEventListener('mouseup', () => setTimeout(() => S.api.save_window_rect && S.api.save_window_rect(), 400)));
}

/* ---------- 화면 전환 ---------- */
function show(view) {
  S.view = view;
  document.body.classList.toggle('welcome-mode', view === 'welcome');
  $$('.view').forEach((v) => v.classList.toggle('on', v.id === `view-${view}`));
  $$('.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.view === view));
  if (view === 'terms') loadTerms();
  if (view === 'setup') { loadGames(); renderSetup(); }
  if (view === 'settings') { renderSettings(); if (!S.games) loadGames().then(renderSettings); }
}

/* ---------- 상태 ---------- */
async function refresh(full = false) {
  if (full || !S.state) S.state = await call('get_state', false);
  S.live = await call('get_live');
  renderAll();
  suggestLighter();
}

/* 번역이 밀리면(최근 1분 가운데 4초↑) 더 가벼운 모델을 한 번 권한다 — 같은 모델로는 한 시간에 한 번만 */
async function suggestLighter() {
  const l = S.live && S.live.lighter, st = S.live && S.live.live;
  if (!l || !st || S.asking) return;
  const key = `${st.model}>${l.name}`;
  if (S.suggested && S.suggested[key] && Date.now() - S.suggested[key] < 3600e3) return;
  (S.suggested = S.suggested || {})[key] = Date.now();
  S.asking = true;
  const ok = await confirmBox(`번역이 밀립니다 · ${st.slow}초`, l.installed ? `${l.label} 로 바꿀까요? 더 빠르고 조금 덜 정확합니다`
    : `${l.label} 을(를) 받아 바꿀까요? 환경 설정 → 번역 모델에서 받습니다`, l.installed ? '바꾸기' : '환경 설정으로', '그대로', false);
  S.asking = false;
  if (!ok) return;
  if (l.installed) { await call('use_model', l.name); toast(`${l.label} 로 바꿨습니다`, 'ok'); refresh(true); }
  else { show('setup'); S.step = Math.max(0, STEPS.findIndex((s) => s.key === 'model')); renderSetup(); }
}
function renderAll() { renderEngine(); renderHome(); renderLoaded(); renderUpdate(); if (S.view === 'setup') renderSetup(); $('#setup-dot').hidden = setupDone(); }
function setupDone() {
  const st = S.state && S.state.steps; if (!st) return false;
  return ['ocr', 'ollama', 'model', 'region'].every((k) => st[k] === 'done');
}

function renderEngine() {
  const st = S.state; if (!st) return;
  const dot = $('#engine .dot'), txt = $('#engine-text');
  const pull = S.progress.pull;
  dot.className = 'dot ' + (pull ? 'info' : st.ollama.running ? 'ok' : 'err');
  txt.textContent = pull ? `${pct(pull)}%` : st.ollama.running ? st.settings.model : 'off';
  $('#engine').dataset.tip = pull ? '모델 받는 중' : st.ollama.running ? 'AI 엔진 준비됨' : 'AI 엔진 꺼짐';
}

/* ---------- 홈 ---------- */
function renderHome() {
  const st = S.state, lv = S.live; if (!st || !lv) return;
  const on = lv.running.live;
  if (on && lv.live) S.starting = 0;
  const busy = S.starting && Date.now() - S.starting < 20000;
  const p = $('#power');
  p.classList.toggle('on', on); p.classList.toggle('busy', !!busy && !lv.live);
  p.setAttribute('aria-pressed', on); p.setAttribute('aria-label', on ? '통역 끄기' : '통역 시작');
  p.dataset.tip = on ? '통역 끄기' : '통역 시작';
  $('.ctrl-panel').classList.toggle('on', on);
  setSwitch('#sw-input', lv.running.input); $('#row-input').classList.toggle('on', lv.running.input);
  setSwitch('#sw-orig', st.settings.show_original); $('#row-orig').classList.toggle('on', !!st.settings.show_original);
  // 오늘
  const m = $('.metrics'); m.classList.toggle('dim', !lv.today);
  $('#m-count').textContent = lv.today || 0;
  $('#m-latency').innerHTML = lv.avg_sec != null ? `${lv.avg_sec.toFixed(1)}<small>s</small>` : '-';
  $('#m-read').innerHTML = lv.live && lv.live.ocr_ms ? `${lv.live.ocr_ms}<small>ms</small>` : '-';
  // 동작 — 오늘 통역이 돈 시간(껐다 켜도 더함). 툴팁: 이번 실행 시작 · 지난 시간
  $('#m-run').innerHTML = lv.runtime >= 60 ? dur(lv.runtime) : '-';
  const started = on && lv.live && lv.live.started;
  $('#m-run-now').textContent = started ? `${new Date(started * 1000).toTimeString().slice(0, 5)}~ ${dur(Date.now() / 1000 - started, true)}` : '';
  $('#m-run-box').dataset.tip = started ? '오늘 통역이 돈 시간 · 이번 실행 시작과 지난 시간' : '오늘 통역이 돈 시간';
  // 상태
  const ocrOk = st.ocr.langs.filter((l) => l.installed).length;
  const ocrMiss = st.ocr.langs.filter((l) => !l.installed).map((l) => l.label);
  stat('#st-ai', st.ollama.running ? (st.models.find((x) => x.name === st.settings.model)?.installed ? 'ok' : 'warn') : 'err',
    st.ollama.running ? st.settings.model + (st.vram && st.vram.device === 'cpu' ? ' · CPU' : '') : '꺼짐');
  const aiCovers = ocrMiss.length && st.steps && st.steps.ocr === 'done';  // 빠진 언어 팩을 AI 글자 인식이 대신 읽음
  stat('#st-ocr', ocrMiss.length && !aiCovers ? 'warn' : 'ok', !ocrMiss.length ? `${ocrOk}개 언어` : aiCovers ? `${ocrOk}개 + AI` : `${ocrMiss[0]} 없음`);
  stat('#st-game', st.game ? 'ok' : 'off', st.game ? (st.game.flavor || st.game.exe) : '꺼짐');
  stat('#st-region', st.region ? 'ok' : 'warn', st.region ? `${st.region.w}×${st.region.h}` : '찾기 필요');
  renderFeed();
}
function stat(sel, kind, value) {
  const el = $(sel); el.className = 'stat ' + (kind === 'warn' ? 'warn' : kind === 'off' ? 'off' : '');
  $('.dot', el).className = 'dot ' + (kind === 'off' ? '' : kind); $('b', el).textContent = value;
}
function setSwitch(sel, v) { $(sel).setAttribute('aria-checked', !!v); }
// 홈 목록 — 통째로 다시 그리면 갱신마다 모든 줄의 나타나기가 다시 돌아 깜빡였다. 줄마다 열쇠를 두고 새 줄만 위에 끼운다
const LANG_ORDER = ['en', 'zh', 'ru', 'es', 'de', 'fr', 'pt', 'uk', 'it', 'ja'];
const LANG_COLOR = ['en', 'zh', 'ru', 'ja', 'es', 'de', 'fr', 'pt', 'uk'];
const codeOf = (f) => f.tag || f.lang || '';
function renderLangTabs() {
  const seen = new Set((S.live.feed || []).map(codeOf).filter(Boolean));
  if (S.feedLang !== 'all') seen.add(S.feedLang);
  const codes = [...seen].sort((a, b) => (LANG_ORDER.indexOf(a) + 1 || 99) - (LANG_ORDER.indexOf(b) + 1 || 99));
  setHTML($('.lang-tabs'), [`<button role="tab" data-lang="all" class="${S.feedLang === 'all' ? 'on' : ''}">전체</button>`]
    .concat(codes.map((c) => `<button role="tab" data-lang="${esc(c)}" class="mono ${LANG_COLOR.includes(c) ? esc(c) : ''} ${S.feedLang === c ? 'on' : ''}">${esc(c.toUpperCase())}</button>`)).join(''));
}
function feedItem(f) {
  const el = document.createElement('article');
  el.className = 'item new';
  el.addEventListener('animationend', () => el.classList.remove('new'), { once: true });
  setTimeout(() => el.classList.remove('new'), 600);  // 창이 가려져 효과가 돌지 않았을 때도
  return el;
}
function fillItem(el, f) {
  const c = codeOf(f);
  const html = `<span class="code ${esc(c)} ${LANG_COLOR.includes(c) ? '' : 'other'}">${esc(c.toUpperCase())}</span>
      <div>${f.kind === 'ad' && !f.ko ? '<div class="ko ad">광고</div>' : `<div class="ko">${esc(f.ko)}</div>`}<div class="orig"><b>${esc(f.name)}</b>${esc(f.body)}</div></div>
      <span class="time">${esc((f.t || '').slice(11, 16))}</span>`;
  if (el._h !== html) { el.innerHTML = html; el._h = html; }
}
function renderFeed() {
  renderLangTabs();
  const items = (S.live.feed || []).filter((f) => S.feedLang === 'all' || codeOf(f) === S.feedLang);
  const box = $('#feed');
  S.feedEls = S.feedEls || new Map();
  const keep = new Set();
  items.forEach((f, i) => {
    const k = `${f.t}|${f.name}|${f.body}`;
    keep.add(k);
    let el = S.feedEls.get(k);
    if (!el) { el = feedItem(f); S.feedEls.set(k, el); }
    fillItem(el, f);
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
  });
  for (const [k, el] of S.feedEls) if (!keep.has(k)) { el.remove(); S.feedEls.delete(k); }
  $('#feed-empty').hidden = items.length > 0;
}

/* ---------- 환경 설정 ---------- */
const STEPS = [
  { key: 'system', name: '내 PC', icon: 'i-pc' },
  { key: 'ocr', name: '글자 인식', icon: 'i-ocr' },
  { key: 'ai', name: 'AI 글자 인식', icon: 'i-chip' },
  { key: 'ollama', name: 'AI 실행기', icon: 'i-box' },
  { key: 'model', name: '번역 모델', icon: 'i-download' },
  { key: 'addon', name: '게임 · 글꼴', icon: 'i-game' },
  { key: 'region', name: '채팅 영역', icon: 'i-frame' },
  { key: 'test', name: '번역 시험', icon: 'i-test' },
];
const TASK_OF = { ocr: 'ocr', ai: 'ai', ollama: 'ollama', model: 'pull', region: 'region', test: 'test' };
function pct(p) { return p.total ? Math.floor((p.done / p.total) * 100) : 0; }
function gb(n) { return (n / 1024 ** 3).toFixed(1); }
function mb(n) { return n >= 1024 ** 3 ? `${gb(n)}GB` : `${Math.max(0, n / 1024 ** 2).toFixed(n < 10 * 1024 ** 2 ? 1 : 0)}MB`; }

function defaultGame(games) {
  const saved = S.state && S.state.settings.game_dir;
  return (games.find((g) => g.dir === saved) || games.find((g) => g.running && g.supported) || games.find((g) => g.supported) || {}).dir;
}
async function pickFolder() {
  const r = await call('pick_game_folder');
  if (r.ok) { S.games = r.games; S.selGame = r.dir; S.state.settings.game_dir = r.dir; toast('게임 폴더를 지정했습니다', 'ok'); refresh(true); }
  else if (r.error) toast(r.error, 'err');
  renderSetup(); renderSettings();
}
async function loadGames() { S.games = await call('get_games'); if (S.view === 'setup') renderSetup(); }

function stepState(k) {
  const task = TASK_OF[k];
  if (task && S.progress[task]) return 'run';
  if (task && S.state.busy.includes(task)) return 'run';
  return (S.state.steps || {})[k] || 'todo';
}
function renderSetup() {
  const st = S.state; if (!st) return;
  const done = STEPS.filter((s) => stepState(s.key) === 'done').length;
  $('#setup-count').textContent = `${done}/${STEPS.length}`;
  $('#setup-bar').style.width = `${(done / STEPS.length) * 100}%`;
  setHTML($('#steps'), STEPS.map((s, i) => {
    const k = stepState(s.key);
    const p = S.progress[TASK_OF[s.key]];
    const mark = k === 'done' ? '✓' : k === 'run' ? (p && p.total ? `${pct(p)}%` : '…') : (s.key === 'test' ? '' : '!');
    return `<li><button data-step="${i}" class="${i === S.step ? 'on' : ''}" aria-label="${esc(s.name)}">${icon(s.icon)}<span class="nm">${esc(s.name)}</span>
      <span class="st ${k === 'todo' && s.key === 'test' ? '' : k}">${mark}</span></button></li>`;
  }).join(''));
  const s = STEPS[S.step];
  $('#step-icon').setAttribute('href', '#' + s.icon);
  $('#step-name').textContent = s.name;
  if (document.activeElement && document.activeElement.id === 'test-ko') S.testKo = document.activeElement.value;
  const typing = document.activeElement && document.activeElement.id === 'test-ko';
  const [body, action] = DETAIL[s.key]();
  if (typing && s.key === 'test' && !S.progress.test) { setHTML($('#step-action'), action || ''); return; }
  setHTML($('#step-body'), body);
  setHTML($('#step-action'), action || '');
  $('#step-prev').disabled = S.step === 0;
  const last = S.step === STEPS.length - 1;
  const nb = $('#step-next');
  setHTML(nb, last ? '완료' : `다음${icon('i-right', 'sm')}`);
  nb.classList.toggle('primary', stepState(s.key) === 'done');
}

const DETAIL = {
  system() {
    const s = S.state.system, g = s.gpu || {};
    const warn = !g.vram_gb || g.vram_gb < 6;
    return [`<div class="tiles">
      <div class="tile"><small>운영체제</small><b>${esc(s.windows.split(' (')[0])}</b></div>
      <div class="tile"><small>그래픽카드</small><b>${esc((g.name || '-').replace(/NVIDIA GeForce |AMD Radeon /, ''))}</b></div>
      <div class="tile"><small>VRAM</small><b class="mono">${g.vram_gb ?? '-'}GB</b></div>
      <div class="tile"><small>메모리</small><b class="mono">${s.ram_gb}GB</b></div></div>
      ${warn ? `<div class="chips"><span class="chip bad">${icon('i-chip')}그래픽카드 메모리가 작아 가벼운 모델을 권합니다</span></div>` : ''}`];
  },
  ocr() {
    const langs = S.state.ocr.langs, miss = S.state.ocr.missing, run = S.progress.ocr || S.state.busy.includes('ocr');
    const removable = S.state.removable_ocr || [];
    const chips = langs.map((l) => `<span class="chip ${l.installed ? 'ok' : 'bad'}">${icon(l.installed ? 'i-check' : 'i-ocr')}${esc(l.label)}${
      l.installed && removable.includes(l.code) && !run ? `<button class="chip-x" data-act="remove_ocr" data-code="${esc(l.code)}" data-label="${esc(l.label)}" aria-label="${esc(l.label)} 언어 팩 지우기" data-tip="언어 팩 지우기">${icon('i-x')}</button>` : ''}</span>`).join('');
    // 언어 팩을 깔 수 없는 PC(PC방 — DISM '액세스가 거부되었습니다')는 AI 글자 인식이 그 언어를 대신 읽는다
    const covered = S.state.steps && S.state.steps.ocr === 'done';
    const aiRun = S.progress.ai || S.state.busy.includes('ai');
    const action = miss.length && !covered ? `<span class="inline"><button class="btn" data-act="cover_ocr_with_ai" ${run || aiRun ? 'disabled' : ''} data-tip="관리자 권한 없이 — AI 글자 인식 모델을 WATT 폴더에 받아 대신 읽기">${icon('i-chip', 'sm')}${aiRun ? '받는 중' : 'AI로 대신'}</button>
      <button class="btn primary" data-act="install_ocr" ${run || aiRun ? 'disabled' : ''} data-tip="Windows 권한 확인 창이 뜹니다">${icon('i-shield', 'sm')}${run ? '설치 중' : '설치'}</button></span>` : '';
    const note = miss.length && covered ? `<div class="facts"><span class="okx">${icon('i-chip', 'sm')}AI 글자 인식이 대신 읽음</span></div>` : '';
    return [`<div class="chips">${chips}</div>${note}${run || aiRun ? '<div class="progress indet"><i></i></div>' : ''}`, action];
  },
  ai() {
    // 권장: 한국어 · 중국어 · 러시아어 — Windows OCR 만으로는 정답 표본 메시지 찾음 97.4% · 이름 91.9%, AI 를 켜면 99.6% · 97.8%
    const a = S.ai || {}, on = a.langs || [], models = a.models || {}, sizes = a.sizes || {};
    const p = S.progress.ai, run = p || S.state.busy.includes('ai');
    const NAMES = { ko: '한국어', zh: '중국어 · 영어', ru: '러시아어', latin: '유럽어' };
    const chips = Object.keys(NAMES).map((k) => `<button class="chip ${on.includes(k) ? 'ok' : ''}" data-act="toggle_ai" data-v="${k}" ${run ? 'disabled' : ''}
      data-tip="${models[k] ? '받음' : `${mb(sizes[k] || 0)} 받기`}">${icon(on.includes(k) ? 'i-check' : 'i-ocr')}${NAMES[k]}${k === 'latin' ? '' : ' <span class="badge bronze">권장</span>'}</button>`).join('');
    const need = ['ko', 'zh', 'ru'].filter((k) => !on.includes(k));
    const total = (a.runtime ? 0 : sizes.runtime || 0) + (models.det ? 0 : sizes.det || 0) + need.filter((k) => !models[k]).reduce((s, k) => s + (sizes[k] || 0), 0);
    const prog = run ? (p && p.total ? `<div class="box"><div class="head"><span class="grow mono muted">AI 글자 인식</span><span class="pct">${pct(p)}%</span></div>
      <div class="progress"><i style="width:${pct(p)}%"></i></div></div>` : '<div class="progress indet"><i></i></div>') : '';
    const action = need.length ? `<button class="btn primary" data-act="enable_ai" ${run ? 'disabled' : ''} data-tip="${total ? mb(total) + ' 받기 · ' : ''}PC 안에서만 돌아갑니다">${icon('i-download', 'sm')}권장대로 켜기</button>` : '';
    if (!S.ai) call('get_ai').then((x) => { S.ai = x; renderSetup(); });
    return [`<div class="chips">${chips}</div>${prog}`, action];
  },
  ollama() {
    const o = S.state.ollama, p = S.progress.ollama, run = p || S.state.busy.includes('ollama');
    // 방식: PC 에 Ollama 가 있으면 그것을 권장(더 받지 않음), 없으면 WATT 전용을 받는다
    const sys = o.system, opt = (v, label, tip, rec) => `<button class="${o.mode === v ? 'on' : ''}" data-act="runner_mode" data-v="${v}" ${run ? 'disabled' : ''} data-tip="${esc(tip)}">${label}${rec ? ' <span class="badge bronze">권장</span>' : ''}</button>`;
    const modes = sys ? `<div class="seg mode-seg" role="radiogroup" aria-label="AI 실행기">
      ${opt('system', `PC 의 Ollama <span class="mono muted">${esc(sys.version || '')}</span>`, '이미 설치된 Ollama 를 씁니다 — 더 받지 않습니다', true)}
      ${opt('watt', 'WATT 전용', `WATT 폴더에만 · ${gb(o.download || 0) > 0 && !o.watt_ready ? gb(o.download) + 'GB 받기' : '받음'} · 지우면 깨끗이`, false)}</div>` : '';
    const chips = modes + `<div class="chips">
      <span class="chip ${o.installed ? 'ok' : 'bad'}">${icon(o.installed ? 'i-check' : 'i-box')}${o.mode === 'system' ? '설치됨' : '받음'}</span>
      <span class="chip ${o.running ? 'ok' : 'bad'}">${icon(o.running ? 'i-check' : 'i-power')}실행</span>
      ${o.version ? `<span class="chip mono">${esc(o.version)}</span>` : ''}</div>`;
    let prog = '';
    if (run) prog = p && p.total ? `<div class="box"><div class="head"><span class="grow mono muted">ollama-windows-amd64.zip</span><span class="pct">${pct(p)}%</span></div>
      <div class="progress"><i style="width:${pct(p)}%"></i></div>
      <div class="sub"><span>${gb(p.done)} / ${gb(p.total)} GB</span></div></div>` : '<div class="progress indet"><i></i></div>';
    const remove = o.mode === 'watt' && o.installed && !run ? `<button class="btn ghost-danger" data-act="uninstall_ollama" data-tip="WATT 전용 AI 실행기와 받은 모델을 지웁니다">지우기</button>` : '';
    // WATT 전용 Ollama(공식 포터블) — PC 에 Ollama 가 있어도 따로. 설치 창 · 관리자 권한 없이 WATT 폴더에 푼다
    const action = !o.installed ? `<button class="btn primary" data-act="install_ollama" ${run ? 'disabled' : ''} data-tip="WATT 폴더에만 · 설치 창 없이">${icon('i-download', 'sm')}받기 ${o.download ? gb(o.download) + 'GB' : ''}</button>`
      : `<span class="inline">${remove}${!o.running ? `<button class="btn primary" data-act="start_ollama" ${run ? 'disabled' : ''}>${icon('i-power', 'sm')}켜기</button>` : ''}</span>`;
    const err = o.error && !o.running && !run ? `<div class="err-text">${esc(o.error)}</div>` : '';
    return [chips + prog + err, action];
  },
  model() {
    const st = S.state, sel = S.selModel || st.settings.model, p = S.progress.pull;
    const cards = st.models.map((m) => {
      const tags = [m.recommended ? '<span class="badge bronze">추천</span>' : '', m.installed ? '<span class="badge ok">받음</span>' : '',
        !m.verified ? '<span class="badge">검증 전</span>' : ''].join('');
      const busy = (p || st.busy.includes('pull'));
      const trash = m.installed && m.watt && !busy ? `<button class="trash" data-act="delete_model" data-name="${esc(m.name)}" data-label="${esc(m.label)}" data-size="${m.download_gb}" aria-label="${esc(m.label)} 지우기" data-tip="모델 지우기">${icon('i-trash', 'sm')}</button>` : '';
      return `<div class="model ${m.name === sel ? 'sel' : ''}" role="radio" tabindex="0" aria-checked="${m.name === sel}" data-model="${esc(m.name)}">
        <span class="n">${esc(m.label)}<span class="radio"></span></span>
        <span class="m"><span>${m.download_gb}GB</span><span>VRAM ${Math.round(m.vram_gb)}GB</span></span>
        <span class="tags">${tags}${trash}</span></div>`;
    }).join('');
    let box = '';
    if (p || st.busy.includes('pull')) {
      const left = p && p.speed > 0 ? Math.max(0, (p.total - p.done) / p.speed) : null;
      box = `<div class="box"><div class="head"><span class="grow mono muted">${esc((p && p.model) || sel)}</span>
        <span class="pct">${p ? pct(p) : 0}%</span>
        <button class="icon-btn sq" data-act="cancel_pull" aria-label="받기 취소" data-tip="받기 취소">${icon('i-x', 'sm')}</button></div>
        <div class="progress"><i style="width:${p ? pct(p) : 0}%"></i></div>
        <div class="sub"><span>${p && p.total ? `${gb(p.done)} / ${gb(p.total)} GB` : esc((p && p.status) || '준비 중')}</span>
        <span>${p && p.speed ? `${(p.speed / 1024 ** 2).toFixed(0)} MB/s` : ''}${left != null ? ` · ${Math.ceil(left / 60)}분` : ''}</span></div></div>`;
    }
    const m = st.models.find((x) => x.name === sel) || {};
    if (!S.models) call('models_info').then((x) => { S.models = x; renderSetup(); renderModelsDir(); });
    const can = S.models && S.models.importable.includes(sel);
    const action = p || st.busy.includes('pull') ? ''
      : !m.installed && can ? `<span class="inline"><button class="btn" data-act="pull_model" data-tip="새로 받기">${icon('i-download', 'sm')}받기</button>
        <button class="btn primary" data-act="import_model" data-tip="PC 의 Ollama 에 이미 받은 모델 — 다시 받지 않습니다">${icon('i-folder', 'sm')}가져오기</button></span>`
      : !m.installed ? `<button class="btn primary" data-act="pull_model">${icon('i-download', 'sm')}받기</button>`
      : sel !== st.settings.model ? `<button class="btn primary" data-act="use_model">사용</button>` : '<span class="badge ok">사용 중</span>';
    const v = st.vram;
    const vramLine = v && v.available != null ? `<div class="facts vram-facts" data-tip="지금 쓰는 VRAM ${v.used}GB / ${v.total}GB${v.wow ? ` · 와우 켜짐(약 ${v.wow_gb}GB)` : ` · 와우 꺼짐 — 켜면 약 ${v.wow_est}GB 더 씀(${v.wow_how === 'resolution' ? '해상도로 어림' : '이 PC 에서 잰 값'})`}${v.watt_loaded ? ` · 지금 올린 WATT 모델 ${v.watt_loaded}GB 포함` : ''}">
      ${icon('i-gauge', 'sm')}<span>번역 모델에 쓸 VRAM <b>${v.available}GB</b></span><span class="muted">${v.wow ? '와우 켜진 지금 기준' : '와우 몫 빼고'}</span></div>
      ${v.short > 0 && v.device === 'cpu' ? `<div class="facts">${icon('i-chip', 'sm')}<span>VRAM ${v.short}GB 부족 — <b>CPU 로 번역</b>합니다(문장당 약 2–3초)</span></div>`
        : v.short > 0 ? `<div class="err-text">VRAM ${v.short}GB 부족 — 일부를 공유 메모리로 돌려 번역이 느려집니다 · 번역 설정 → 실행 장치를 '자동'이나 'CPU'로${v.wows > 1 ? ` · 와우 창 ${v.wows}개 중 하나를 닫기` : ''}</div>` : ''}` : '';
    const terms = `${vramLine}<p class="terms-line">받으면 Google <a href="#" data-url="https://ai.google.dev/gemma/terms">Gemma 이용 약관</a>에 동의하는 것으로 봅니다</p>`;
    return [`<div class="models">${cards}</div>${terms}${box}`, action];
  },
  addon() {
    const games = S.games || [];
    if (!S.selGame || !games.some((g) => g.dir === S.selGame)) S.selGame = defaultGame(games);
    const list = games.length ? games.map((g) => `<div class="game ${g.dir === S.selGame ? 'sel' : ''} ${g.supported ? '' : 'off'}" role="radio" tabindex="0" aria-checked="${g.dir === S.selGame}" data-game="${esc(g.dir)}">
        <span class="radio"></span><span><b>${esc(g.label)}</b>${g.running ? ' <span class="badge ok">실행 중</span>' : ''}<span class="p">${esc(g.dir)}</span></span>
        ${g.addon ? `<span class="badge ok">애드온 ${esc(g.addon)}</span>` : g.supported ? '<span class="badge">애드온 없음</span>' : '<span class="badge">지원 안 함</span>'}
        ${g.addon ? `<button class="trash" data-act="remove_addon" data-dir="${esc(g.dir)}" data-label="${esc(g.label)}" aria-label="애드온 지우기" data-tip="글꼴 애드온 지우기">${icon('i-trash', 'sm')}</button>` : '<span></span>'}</div>`).join('')
      : `<div class="games-empty">${icon('i-folder', 'xl thin')}<span>게임 폴더를 찾지 못했습니다</span>
          <button class="btn primary" data-act="pick_folder">${icon('i-folder', 'sm')}폴더 선택</button></div>`;
    const g = games.find((x) => x.dir === S.selGame);
    const pick = `<button class="icon-btn sq" data-act="pick_folder" aria-label="게임 폴더 지정" data-tip="게임 폴더 직접 고르기">${icon('i-folder', 'sm')}</button>`;
    const action = games.length ? `<span class="inline">${pick}${g ? `<button class="btn primary" data-act="install_addon" data-tip="게임을 완전히 다시 켜야 적용됩니다">${g.addon ? '다시 설치' : '글꼴 애드온 설치'}</button>` : ''}</span>` : '';
    return [`<div class="games">${list}</div>`, action];
  },
  region() {
    const r = S.state.region, run = S.progress.region || S.state.busy.includes('region');
    const res = S.results.region;
    const img = S.regionPreview ? `<img src="${S.regionPreview}" alt="찾은 채팅 영역">` : icon('i-frame', 'xl thin');
    // 게임 창 고르기 — 실행 파일 이름이 다르거나 창이 여럿일 때(PC방)
    if (!S.windows) call('list_windows').then((x) => { S.windows = x; renderSetup(); });
    const wl = S.windows || { games: [], others: [] };
    const opt = (w) => `<option value="${esc(w.exe)}" ${wl.picked && wl.picked.toLowerCase() === w.exe.toLowerCase() ? 'selected' : ''}>${esc(w.exe)} · ${esc((w.title || '').slice(0, 24))} · ${w.w}×${w.h}</option>`;
    const pick = `<div class="win-pick">${icon('i-game', 'sm')}<label class="select"><select id="game-win" aria-label="게임 창">
      <option value="" ${wl.picked ? '' : 'selected'}>${wl.current && !wl.picked ? esc(wl.current) : '자동'}</option>
      ${wl.games.map(opt).join('')}${wl.others.length ? `<optgroup label="다른 창">${wl.others.map(opt).join('')}</optgroup>` : ''}
    </select>${icon('i-chev', 'sm')}</label><button class="icon-btn sq" data-act="reload_windows" aria-label="창 목록 새로" data-tip="창 목록 새로">${icon('i-refresh', 'sm')}</button></div>`;
    const facts = r ? `<div class="facts"><span class="okx">${icon('i-check', 'sm')}${r.w}×${r.h}</span><span>${r.line_h}px</span>${r.lines ? `<span>${r.lines}줄</span>` : ''}</div>` : '';
    const err = res && res.error ? `<div class="err-text">${esc(res.error)}</div>` : '';
    const tips = [['i-bg', '검정 배경', '채팅 배경을 불투명한 검정으로'], ['i-type', '글자 14+', '글자 크기 14 이상'],
      ['i-eyeoff', '사라짐 끄기', '채팅 글자 사라짐(페이드) 끄기'], ['i-tab', '전용 탭', '번역할 채널만 모은 채팅 탭']]
      .map(([i, l, t]) => `<span class="chip" data-tip="${esc(t)}">${icon(i, 'sm')}${l}</span>`).join('');
    const action = `<span class="inline"><button class="icon-btn sq" data-act="pick_region" ${run ? 'disabled' : ''} aria-label="직접 지정" data-tip="게임 화면에서 채팅창을 끌어서 지정">${icon('i-crop', 'sm')}</button>
      <button class="icon-btn sq" data-act="report_region" ${run ? 'disabled' : ''} aria-label="인식 오류 신고" data-tip="잘못 찾았을 때 화면을 보내 고치는 데 쓰기">${icon('i-flag', 'sm')}</button>
      <button class="btn ${r ? '' : 'primary'}" data-act="find_region" ${run ? 'disabled' : ''} data-tip="게임이 켜져 있어야 합니다">${icon('i-target', 'sm')}${run ? '찾는 중' : r ? '다시 찾기' : '찾기'}</button></span>`;
    return [`${pick}<div class="preview">${img}</div>${run ? '<div class="progress indet"><i></i></div>' : facts}${err}<div class="chips">${tips}</div>`, action];
  },
  test() {
    const run = S.progress.test || S.state.busy.includes('test'), res = S.results.test;
    // 받기: 지금 게임 채팅창의 최근 외국어 3줄 · 보내기: 입력한 한국어(없으면 예시)
    const input = `<label class="ko-input">${icon('i-kbd', 'sm')}<input id="test-ko" placeholder="보낼 말 (한국어)" value="${esc(S.testKo || '')}" aria-label="보낼 말"></label>`;
    let out = run ? '<div class="progress indet"><i></i></div>' : '';
    if (res && res.rows) {
      const row = (r, i) => `<div class="result"><span class="dir ${esc(r.lang)}">${r.dir === 'in' ? esc((r.lang || '').toUpperCase()) : 'KO→' + esc((r.lang || '').toUpperCase())}</span>
        <div>${r.dir === 'out' && i > 0 ? '' : `<div class="src">${r.who ? `<b>${esc(r.who)}</b> ` : ''}${esc(r.src)}</div>`}<div class="dst">${esc(r.dst)}</div></div><span class="sec">${r.sec}s</span></div>`;
      const ins = res.rows.filter((r) => r.dir === 'in');
      out = `<div class="sec-label">${icon('i-msg', 'sm')}받기 · 채팅창${res.note ? ` <span class="muted">— ${esc(res.note)}</span>` : ''}</div>
        ${ins.length ? `<div class="results">${ins.map(row).join('')}</div>` : ''}
        <div class="sec-label">${icon('i-kbd', 'sm')}보내기</div>
        <div class="results">${res.rows.filter((r) => r.dir === 'out').map(row).join('')}</div>`;
    } else if (res && res.error) out = `<div class="err-text">${esc(res.error)}</div>`;
    const action = `<button class="btn primary" data-act="test_translate" ${run ? 'disabled' : ''} data-tip="게임 채팅창의 최근 외국어 3줄 · 보낼 말을 영어 · 중국어 · 러시아어 · 일본어로">${icon('i-test', 'sm')}${run ? '번역 중' : '시험'}</button>`;
    return [input + out, action];
  },
};

async function act(name, d = {}) {
  const st = S.state;
  if (name === 'delete_model') {
    if (!(await confirmBox(`${d.label} 지우기`, `${d.size}GB 를 비웁니다. 다시 쓰려면 다시 받아야 합니다`))) return;
    await call('delete_model', d.name); toast('모델을 지웠습니다', 'ok'); return refresh(true);
  }
  if (name === 'remove_addon') {
    if (!(await confirmBox('글꼴 애드온 지우기', `${d.label} · 게임을 다시 켜면 적용됩니다`))) return;
    await call('remove_addon', d.dir); toast('글꼴 애드온을 지웠습니다', 'ok'); return loadGames().then(() => refresh(true));
  }
  if (name === 'remove_ocr') {
    if (!(await confirmBox(`${d.label} 언어 팩 지우기`, 'Windows 권한 확인 창이 뜹니다'))) return;
    return call('remove_ocr', d.code);
  }
  if (name === 'uninstall_ollama') {
    if (!(await confirmBox('Ollama 제거', '통역이 꺼지고 Ollama 제거 프로그램이 열립니다. 받은 모델 파일은 남습니다', '제거'))) return;
    return call('uninstall_ollama');
  }
  if (name === 'install_ocr') return call('install_ocr', null);
  if (name === 'enable_ai') { const r = await call('enable_ai', null); if (r && r.started) S.progress.ai = { done: 0, total: r.bytes }; S.ai = await call('get_ai'); return renderSetup(); }
  if (name === 'toggle_ai') { const v = d.v, on = (S.ai && S.ai.langs || []).includes(v); const r = await call('set_ai_lang', v, !on); if (r && r.started) S.progress.ai = { done: 0, total: r.bytes }; S.ai = await call('get_ai'); return renderSetup(); }
  if (name === 'install_ollama') return call('install_ollama');
  if (name === 'runner_mode') {
    const o = S.state.ollama;
    if (d.v === o.mode) return;
    if (d.v === 'watt' && !o.watt_ready) return call('install_ollama');  // 전용은 받기부터(받으면 그 방식으로)
    S.models = null; return call('set_runner_mode', d.v);
  }
  if (name === 'import_model') { S.progress.pull = { model: S.selModel || st.settings.model, done: 0, total: 0, status: '가져오는 중' }; renderSetup(); S.models = null; return call('import_model', S.selModel || st.settings.model); }
  if (name === 'start_ollama') return call('start_ollama');
  if (name === 'pull_model') { S.progress.pull = { model: S.selModel || st.settings.model, done: 0, total: 0, status: '준비 중' }; renderSetup(); return call('pull_model', S.selModel || st.settings.model); }
  if (name === 'cancel_pull') return call('cancel', 'pull');
  if (name === 'use_model') { await call('use_model', S.selModel); toast('모델을 바꿨습니다', 'ok'); return refresh(true); }
  if (name === 'pick_folder') return pickFolder();
  if (name === 'install_addon') { const r = await call('install_addon', S.selGame); toast(`글꼴 애드온 ${r.version || ''} 설치됨. 게임을 다시 켜 주세요`, 'ok'); return loadGames().then(() => refresh(true)); }
  if (name === 'find_region') { S.results.region = null; return call('find_region'); }
  if (name === 'cover_ocr_with_ai') { const r = await call('cover_ocr_with_ai'); if (r && r.started) S.progress.ai = { done: 0, total: r.bytes }; S.ai = await call('get_ai'); return refresh(true); }
  if (name === 'reload_windows') { S.windows = await call('list_windows'); return renderSetup(); }
  if (name === 'pick_region') return openShot('pick');
  if (name === 'report_region') return openShot('report');
  if (name === 'test_translate') { S.testKo = ($('#test-ko') || {}).value || ''; S.results.test = null; return call('test_translate', S.testKo); }
}

/* ---------- 번역 설정 ---------- */
// 모델 폴더 — 모델이 커서(4–8GB) 다른 드라이브로 옮길 수 있게. 고르면 그 아래 WATT-models 에 둔다
function renderModelsDir() {
  const el = $('#set-models-dir'); if (!el) return;
  const m = S.models, p = S.progress.move, run = p || (S.state && S.state.busy.includes('move'));
  if (!m) { call('models_info').then((x) => { S.models = x; renderModelsDir(); }); return; }
  $('#models-dir-field').hidden = m.mode !== 'watt';  // PC 의 Ollama 는 모델 폴더를 Ollama 설정이 정한다
  const o = S.state && S.state.ollama;
  $('#set-runner').value = (o && o.mode) || m.mode;
  $('#set-runner').querySelector('option[value=system]').disabled = !(o && o.system);
  el.textContent = run ? `옮기는 중 ${p && p.total ? pct(p) + '%' : ''}` : m.dir;
  el.dataset.tip = `${m.dir} · 쓰는 용량 ${gb(m.used)}GB · 빈 공간 ${gb(m.free)}GB`;
  $('#set-models-pick').disabled = !!run;
  $('#set-models-reset').hidden = !m.custom || !!run;
}
function renderDevice() {
  const el = $('#set-device'); if (!el || !S.state) return;
  el.value = S.state.settings.llm_device || 'auto';
  const v = S.state.vram;
  $('#device-now').textContent = v ? (v.device === 'cpu' ? 'CPU' : '그래픽 카드') : '';
}
function renderLoaded() {
  renderDevice();
  const o = S.state && S.state.ollama; if (!o || !$('#set-loaded')) return;
  const names = (o.loaded || []).map((m) => m.name);
  $('#set-loaded').textContent = names.length ? names.join(', ') : '없음';
  $('#set-unload').hidden = !names.length;
}
function renderSettings() {
  const c = S.state && S.state.settings; if (!c) return;
  renderModelsDir();
  renderLoaded();
  renderHotkey();
  const games = (S.games || []).filter((g) => g.supported);
  const cur = c.game_dir || defaultGame(S.games || []) || '';
  setHTML($('#set-game'), games.length ? games.map((g) => `<option value="${esc(g.dir)}" ${g.dir === cur ? 'selected' : ''}>${esc(g.label)}</option>`).join('')
    : '<option value="">없음</option>');
  $('#set-gamedir').textContent = cur || '-';
  $('#set-gamedir').dataset.tip = cur || '게임 폴더를 지정해 주세요';
  $('#set-lang').value = c.out_lang || 'en';
  $$('#set-mode button').forEach((b) => b.classList.toggle('on', b.dataset.v === c.out_mode));
  $$('#set-ads button').forEach((b) => b.classList.toggle('on', b.dataset.v === (c.ad_filter || 'fold')));
  $$('#set-newest button').forEach((b) => b.classList.toggle('on', b.dataset.v === (c.chat_newest || 'bottom')));
  const sel = $('#set-model');
  const names = S.state.ollama.models.map((m) => m.name);
  if (!names.includes(c.model)) names.unshift(c.model);
  sel.innerHTML = names.map((n) => `<option ${n === c.model ? 'selected' : ''}>${esc(n)}</option>`).join('');
  setSwitch('#set-update', c.update_check);
  call('get_storage').then((u) => { $('#set-usage').textContent = mb(u.total); $('#set-usage').dataset.tip = `화면 캡처 ${mb(u.frames)} · 추적 ${mb(u.trace)} · 받은 파일 ${mb(u.downloads)}`; }).catch(() => {});
  setSwitch('#set-preload', c.preload); setSwitch('#set-orig', c.show_original); setSwitch('#set-korean', c.show_korean !== false); setSwitch('#set-logs', c.keep_logs);
  $('#set-font').value = c.overlay_font; $('#out-font').textContent = c.overlay_font;
  $('#set-alpha').value = Math.round(c.overlay_alpha * 100); $('#out-alpha').textContent = Math.round(c.overlay_alpha * 100) + '%';
  $('#set-lines').value = c.overlay_lines; $('#out-lines').textContent = c.overlay_lines;
  // 숨긴 사람 · 광고 고침(#13) — 통역 창 오른쪽 클릭으로 정한 것, × 로 되돌리기
  const people = [...(c.hidden_names || []).map((n) => ['hidden_names', n, '숨김']), ...(c.ad_allow || []).map((n) => ['ad_allow', n, '광고 아님']),
    ...(c.ad_block || []).map((n) => ['ad_block', n, '광고'])];
  $('#people-field').hidden = !people.length;
  setHTML($('#set-people'), people.map(([k, n, label]) => `<span class="chip">${esc(n)}<small>${label}</small><button class="chip-x" data-k="${k}" data-n="${esc(n)}" aria-label="${esc(n)} 되돌리기">${icon('i-x')}</button></span>`).join(''));
  renderAi();
}

/* AI 글자 인식 — 언어마다 켜기(켤 때 그 모델만 받기) */
const AI_NAME = { ko: '한국어 이름 · 글', zh: '중국어 · 영어 본문(작은 글꼴에서 g·q 를 덜 헷갈림)', ru: '러시아어 · 우크라이나어', latin: '스페인어 · 독일어 · 프랑스어 등 악센트(ñ · ü · é) — 영어로 보이는 줄은 그대로' };
async function renderAi() {
  const a = S.ai = await call('get_ai');
  const p = S.progress.ai, run = p || (S.state && S.state.busy.includes('ai'));
  const mbOf = (k) => (a.sizes[k] / 1048576).toFixed(0);
  $$('#set-ai button').forEach((b) => {
    const v = b.dataset.v, have = a.models[v];
    b.classList.toggle('on', a.langs.includes(v));
    b.disabled = !!run;
    b.dataset.tip = AI_NAME[v] + (have ? '' : ` — 처음 켤 때 ${mbOf(v)}MB${a.models.det ? '' : ` + 공통 ${mbOf('det')}MB`}${a.runtime ? '' : ` + 실행 엔진 ${mbOf('runtime')}MB`} 받기`);
  });
  setHTML($('#ai-prog'), run ? (p && p.total ? `<div class="progress"><i style="width:${pct(p)}%"></i></div>` : '<div class="progress indet"><i></i></div>') : '');
  setSwitch('#set-aigpu', a.gpu);
  const st = $('#ai-state');
  st.hidden = !(a.active && a.active.length) && !a.error;
  st.textContent = a.error ? '오류' : (a.active_gpu ? 'GPU' : 'CPU');
  st.className = 'chip mono ' + (a.error ? 'bad' : 'ok');
  st.dataset.tip = a.error || `통역 창이 지금 쓰는 장치 · ${(a.active || []).map((l) => l.toUpperCase()).join(' · ')}`;
  const got = a.disk;
  $('#ai-size').textContent = got ? mb(got) : '-';
  $('#set-airemove').disabled = !got || !!run;
}
let saveTimer = null;
function save(changes, quiet = false) {
  Object.assign(S.state.settings, changes);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => { S.state.settings = await call('save_settings', changes); if (!quiet) toast('저장했습니다', 'ok'); renderAll(); }, 250);
}

/* ---------- 용어 사전 ---------- */
const CATS = [['all', '전체', () => true], ['dungeon', '던전', (t) => t.type === 'dungeon' || t.type === 'wing'], ['raid', '공격대', (t) => t.type === 'raid'],
  ['zone', '지역', (t) => t.type === 'zone'], ['city', '도시', (t) => t.type === 'city'], ['class', '직업·역할', (t) => ['class', 'spec', 'role'].includes(t.type)],
  ['chat', '채팅 말', (t) => t.type === 'chat'], ['item', '아이템', (t) => t.type === 'item'], ['verify', '확인 필요', (t) => (t.verify || []).length > 0],
  ['mine', '고친 것', (t) => t.origin && t.origin !== 'base']];
const MARK = { edited: '수정', added: '추가', hidden: '숨김' };
async function loadTerms() { if (!S.terms) S.terms = await call('get_terms'); renderTerms(); }
function renderTerms() {
  const terms = S.terms || [];
  const visible = (k) => (t) => k === 'mine' || t.origin !== 'hidden';  // 숨긴 것은 '고친 것'에서만
  $('#cats').innerHTML = CATS.map(([k, l, f]) => `<button role="tab" data-cat="${k}" class="${k === S.termCat ? 'on' : ''}">${l}<span>${terms.filter(visible(k)).filter(f).length}</span></button>`).join('');
  const q = $('#term-q').value.trim().toLowerCase();
  const f = CATS.find((c) => c[0] === S.termCat)[2];
  const rows = terms.filter(visible(S.termCat)).filter(f).filter((t) => !q || JSON.stringify(t).toLowerCase().includes(q));
  $('#term-rows').innerHTML = rows.map((t) => {
    const abbr = (t.en_abbr || [])[0] || '';
    const en = (t.en || '').replace(/\s*\(.*\)$/, '');
    const verify = (t.verify || []).length ? ' <span class="badge warn">확인 필요</span>' : '';
    const mark = MARK[t.origin] ? `<span class="mark ${t.origin}">${MARK[t.origin]}</span>` : '';
    return `<div class="tr${t.origin === 'hidden' ? ' hidden-term' : ''}" data-id="${esc(t.id)}"><span class="ko">${esc(t.ko)}${mark}</span><span><span class="alias">${esc((t.ko_alias || []).slice(0, 2).join(', '))}</span>${verify}</span>
      <span><span class="abbr">${esc(abbr)}</span> <span class="dimtx">${abbr.toLowerCase() === en.toLowerCase() ? '' : esc(en)}</span></span>
      <span class="other">${esc([...(t.zh || []).slice(0, 2), ...(t.zh_only ? (t.en_abbr || []) : [])].join(' · '))}</span>
      <span class="other">${esc((t.ru || [])[0] || '')}</span></div>`;
  }).join('');
}

function editTerm(t) {
  const isNew = !t, e = t || { type: 'chat', zh_only: false }, list = (v) => (v || []).join(', ');
  $('#te-title').textContent = isNew ? '용어 추가' : e.ko;
  $('#te-type').value = e.type || 'chat';
  for (const k of ['ko', 'en']) $(`#te-${k}`).value = e[k] || '';
  for (const k of ['ko_alias', 'en_abbr', 'zh', 'ru']) $(`#te-${k}`).value = list(e[k]);
  setSwitch('#te-zh_only', e.zh_only);
  // 확인 필요 줄임말 — 맞으면 ✓ 로 확인(태그가 빠짐), 틀리면 줄임말 칸에서 지우면 함께 빠진다
  let verify = [...(e.verify || [])];
  const drawVerify = () => {
    const now = $('#te-ko_alias').value.split(',').map((s) => s.trim());
    const show = verify.filter((v) => now.includes(v));
    $('#te-verify').hidden = !show.length;
    setHTML($('#te-verify'), show.map((v) => `<span class="badge warn" data-tip="실제로 쓰이는 줄임말인지 확인 전">${esc(v)}<button data-v="${esc(v)}" aria-label="${esc(v)} 확인" data-tip="맞는 줄임말">${icon('i-check')}</button></span>`).join(''));
  };
  $('#te-verify').onclick = (ev) => { const b = ev.target.closest('button[data-v]'); if (b) { verify = verify.filter((v) => v !== b.dataset.v); drawVerify(); } };
  $('#te-ko_alias').oninput = drawVerify;
  drawVerify();
  $('#te-zh_only').onclick = () => setSwitch('#te-zh_only', $('#te-zh_only').getAttribute('aria-checked') !== 'true');
  $('#te-err').textContent = '';
  const del = $('#te-del'), reset = $('#te-reset');
  del.hidden = isNew || e.origin === 'hidden';
  del.textContent = e.origin === 'added' ? '지우기' : '숨기기';
  del.dataset.tip = e.origin === 'added' ? '추가한 용어 지우기' : '번역에 쓰지 않기 · 고친 것에서 되살릴 수 있음';
  reset.hidden = !['edited', 'hidden'].includes(e.origin);
  $('#term-edit').hidden = false;
  $('#te-ko').focus();
  const done = async (fn, msg) => { const r = await fn(); if (r && r.error) { $('#te-err').textContent = r.error; return; }
    $('#term-edit').hidden = true; S.terms = await call('get_terms'); renderTerms(); toast(msg, 'ok'); };
  $('#te-no').onclick = () => { $('#term-edit').hidden = true; };
  $('#te-yes').onclick = () => done(() => call('save_term', { id: isNew ? '' : e.id, type: $('#te-type').value,
    ko: $('#te-ko').value, ko_alias: $('#te-ko_alias').value, en: $('#te-en').value, en_abbr: $('#te-en_abbr').value,
    zh: $('#te-zh').value, ru: $('#te-ru').value, zh_only: $('#te-zh_only').getAttribute('aria-checked') === 'true', verify }), '저장했습니다 · 바로 번역에 씁니다');
  del.onclick = async () => { if (await confirmBox(e.origin === 'added' ? '용어 지우기' : '용어 숨기기', e.ko)) done(() => call('delete_term', e.id), e.origin === 'added' ? '지웠습니다' : '숨겼습니다'); };
  reset.onclick = () => done(() => call('reset_term', e.id), '원래대로 되돌렸습니다');
}
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !$('#term-edit').hidden) $('#te-no').click(); });

/* ---------- 온보딩 ---------- */
const COACH = [
  { sel: '#power', circle: true, title: '통역 켜기 · 끄기', text: '게임 채팅을 한국어로 띄웁니다' },
  { sel: '#row-input', title: '보내기 입력창', text: '한국어로 쓰면 영어로 바꿔 줍니다. Ctrl+Shift+K (번역 설정에서 바꾸기)' },
  { sel: '#row-orig', title: '원문 보기', text: '번역 아래에 원래 글을 함께 보여 줍니다' },
  { sel: '#row-region', title: '채팅 영역', text: '채팅창을 옮겼다면 다시 찾습니다' },
];
let coachStep = 0;
function coach(start = true) {
  if (start) { show('home'); coachStep = 0; $('#coach').hidden = false; }
  const c = COACH[coachStep], el = $(c.sel), r = el.getBoundingClientRect(), pad = c.circle ? 10 : 6;
  const spot = $('#coach-spot');
  spot.classList.toggle('rect', !c.circle);
  Object.assign(spot.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
  const b = $('#coach-bubble');
  b.style.left = `${r.right + pad + 18}px`;
  b.style.top = `${Math.max(12, r.top + r.height / 2 - 33)}px`;
  $('#coach-title').textContent = c.title; $('#coach-text').textContent = c.text;
  $('#coach-dots').innerHTML = COACH.map((_, i) => `<i class="${i === coachStep ? 'on' : ''}"></i>`).join('');
  $('#coach-next').textContent = coachStep === COACH.length - 1 ? '완료' : '다음';
}
function coachEnd() { $('#coach').hidden = true; if (S.state && !S.state.settings.onboarded) save({ onboarded: true }, true); }

/* ---------- 업데이트 ---------- */
function renderUpdate() {
  const u = S.update, p = S.progress.update, b = $('#btn-update');
  b.hidden = !(u && u.newer);
  if (b.hidden) return;
  b.disabled = !!p;
  $('#update-text').textContent = p ? `${pct(p)}%` : u.ready || u.version;
  b.classList.toggle('ready', !!u.ready);
  b.dataset.tip = p ? '받는 중' : u.ready ? `${u.ready} 받음 · 다음에 켤 때 적용 · 누르면 지금 다시 시작` : `새 버전 ${u.version}`;
}
async function askRestart(ver) {
  renderUpdate();
  if (await confirmBox(`WATT ${ver}`, '받았습니다. 지금 다시 시작할까요?', '다시 시작', '나중에', false)) call('restart_update');
  else toast('다음에 켤 때 적용됩니다');
}
async function checkUpdate(force) {
  const u = await call('check_update', force);
  if (u && !u.error) S.update = u;
  renderUpdate();
  if (force) toast(u && u.error ? u.error : u && u.newer ? `새 버전 ${u.version} 이 있습니다` : '최신 버전입니다', u && u.error ? 'err' : 'ok');
}
async function applyUpdate() {
  const u = S.update; if (!u) return;
  if (u.ready) return askRestart(u.ready);
  S.progress.update = { done: 0, total: 0 }; renderUpdate();
  call('apply_update');
}

/* ---------- 채팅 영역 직접 지정 · 인식 오류 신고(#14) ---------- */
async function openShot(mode) {
  const s = await call('get_shot');
  if (!s || s.error) return toast((s && s.error) || '게임 화면을 받지 못했습니다', 'err');
  const box = $('#pick-box'), found = $('#pick-found'), shot = $('#pick-shot'), yes = $('#pick-yes');
  const place = (el, r) => { el.hidden = !r; if (r) Object.assign(el.style, { left: `${r.x / s.w * 100}%`, top: `${r.y / s.h * 100}%`, width: `${r.w / s.w * 100}%`, height: `${r.h / s.h * 100}%` }); };
  let picked = s.found ? { ...s.found } : null;  // 지금 영역에서 시작 — 가장자리만 끌어 고치면 된다(처음부터 다시 그리지 않게)
  $('#pick-img').src = s.img;
  place(found, mode === 'pick' ? s.found : null);
  place(box, picked);
  $('#pick-title').textContent = mode === 'pick' ? '채팅 영역 직접 지정' : '인식 오류 신고';
  $('#pick-text').textContent = mode === 'pick' ? '채팅창을 끌어서 고르세요' : '이 화면을 보내 채팅 영역 자동 찾기를 고치는 데 씁니다';
  const blocked = mode === 'report' && !s.report.can;
  $('#pick-note').textContent = blocked ? `이미 보냈습니다 · ${s.report.wait_h}시간 뒤 다시` : mode === 'report' ? '게임 화면 1장 · 다른 사람 이름이 보일 수 있음 · 30일 뒤 삭제' : '';
  yes.textContent = mode === 'pick' ? '저장' : '보내기';
  yes.disabled = blocked || (mode === 'pick' && !(picked && picked.w >= 120 && picked.h >= 40));
  shot.classList.toggle('drag', mode === 'pick');
  // 좌표는 게임 그림(img) 기준 — 틀(shot) 기준이면 미세하게 어긋났다. 다 그린 뒤 안을 끌면 옮기고 가장자리를 끌면 크기 조절
  const img = $('#pick-img'), gx = $('#pick-gx'), gy = $('#pick-gy');
  const at = (e) => { const b = img.getBoundingClientRect(); return { x: Math.max(0, Math.min(b.width, e.clientX - b.left)) / b.width * s.w, y: Math.max(0, Math.min(b.height, e.clientY - b.top)) / b.height * s.h, sx: b.width / s.w }; };
  const EDGE = 7;  // 화면 px — 가장자리 잡기 너비
  const hit = (p) => {  // 상자의 어디를 잡았나: 'move' · 'n' 's' 'e' 'w' 조합 · null
    if (!picked) return null;
    const t = EDGE / p.sx, r = picked;
    const inX = p.x > r.x - t && p.x < r.x + r.w + t, inY = p.y > r.y - t && p.y < r.y + r.h + t;
    if (!inX || !inY) return null;
    const v = (Math.abs(p.y - r.y) < t ? 'n' : Math.abs(p.y - (r.y + r.h)) < t ? 's' : '');
    const h = (Math.abs(p.x - r.x) < t ? 'w' : Math.abs(p.x - (r.x + r.w)) < t ? 'e' : '');
    return v + h || 'move';
  };
  const cursorFor = (k) => !k ? '' : k === 'move' ? 'over-box' : k.length === 2 ? (k === 'nw' || k === 'se' ? 'over-edge-nwse' : 'over-edge-nesw') : 'n s'.includes(k) ? 'over-edge-ns' : 'over-edge-ew';
  const setCursor = (k) => { shot.classList.remove('over-box', 'over-edge-ew', 'over-edge-ns', 'over-edge-nwse', 'over-edge-nesw'); const c = cursorFor(k); if (c) shot.classList.add(c); };
  const guides = (p) => { gx.hidden = gy.hidden = !p; if (p) { gx.style.left = `${p.x / s.w * 100}%`; gy.style.top = `${p.y / s.h * 100}%`; gx.style.width = `${1 / Z.z}px`; gy.style.height = `${1 / Z.z}px`; } };
  // 휠로 확대(커서 자리를 중심으로, 1–6배) · 오른쪽 · 가운데 버튼으로 끌어 옮기기 · 배율 표시를 누르면 원래대로
  const inner = $('#pick-in'), ztag = $('#pick-zoom');
  const Z = { z: 1, x: 0, y: 0 };
  const apply = () => {
    const W = shot.clientWidth, H = shot.clientHeight;
    Z.x = Math.min(0, Math.max(W - W * Z.z, Z.x)); Z.y = Math.min(0, Math.max(H - H * Z.z, Z.y));
    inner.style.transform = `translate(${Z.x}px, ${Z.y}px) scale(${Z.z})`;
    ztag.hidden = Z.z <= 1.001; ztag.textContent = `${Z.z.toFixed(1)}×`;
    $$('#pick .box').forEach((el) => { el.style.borderWidth = `${1.5 / Z.z}px`; });  // 테두리는 늘 1.5px 로 보이게
  };
  Object.assign(Z, { z: 1, x: 0, y: 0 }); apply();
  shot.onwheel = (e) => {
    e.preventDefault();
    const r = shot.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
    const z2 = Math.min(6, Math.max(1, Z.z * (e.deltaY < 0 ? 1.25 : 0.8)));
    Z.x = px - (px - Z.x) * (z2 / Z.z); Z.y = py - (py - Z.y) * (z2 / Z.z); Z.z = z2;
    apply(); guides(at(e));
  };
  ztag.onclick = (e) => { e.stopPropagation(); Object.assign(Z, { z: 1, x: 0, y: 0 }); apply(); };
  shot.oncontextmenu = (e) => e.preventDefault();
  shot.onmousemove = mode !== 'pick' ? null : (e) => { const p = at(e); guides(p); setCursor(hit(p)); };
  shot.onmouseleave = () => guides(null);
  shot.onmousedown = (e) => {
    if (e.button === 1 || e.button === 2) {  // 확대한 화면 옮기기
      if (Z.z <= 1.001) return;
      const sx = e.clientX, sy = e.clientY, x0 = Z.x, y0 = Z.y;
      shot.classList.add('panning');
      const pan = (ev) => { Z.x = x0 + ev.clientX - sx; Z.y = y0 + ev.clientY - sy; apply(); };
      const stop = () => { shot.classList.remove('panning'); window.removeEventListener('mousemove', pan); window.removeEventListener('mouseup', stop); };
      window.addEventListener('mousemove', pan); window.addEventListener('mouseup', stop);
      e.preventDefault();
      return;
    }
    if (mode !== 'pick' || e.button !== 0) return;
    const a = at(e), grab = hit(a), r0 = picked && { ...picked };
    const fix = (r) => ({ x: Math.max(0, r.x), y: Math.max(0, r.y), w: Math.min(s.w - Math.max(0, r.x), Math.abs(r.w)), h: Math.min(s.h - Math.max(0, r.y), Math.abs(r.h)) });
    const move = (ev) => {
      const c = at(ev);
      guides(c);
      if (!grab) {  // 새로 그리기
        picked = { x: Math.min(a.x, c.x), y: Math.min(a.y, c.y), w: Math.abs(c.x - a.x), h: Math.abs(c.y - a.y) };
      } else if (grab === 'move') {  // 옮기기
        const dx = c.x - a.x, dy = c.y - a.y;
        picked = { ...r0, x: Math.max(0, Math.min(s.w - r0.w, r0.x + dx)), y: Math.max(0, Math.min(s.h - r0.h, r0.y + dy)) };
      } else {  // 가장자리 · 모서리로 크기 조절
        let { x, y, w, h } = r0;
        if (grab.includes('w')) { w = r0.x + r0.w - c.x; x = c.x; }
        if (grab.includes('e')) { w = c.x - r0.x; }
        if (grab.includes('n')) { h = r0.y + r0.h - c.y; y = c.y; }
        if (grab.includes('s')) { h = c.y - r0.y; }
        if (w < 0) { x += w; w = -w; }
        if (h < 0) { y += h; h = -h; }
        picked = fix({ x, y, w, h });
      }
      place(box, picked); yes.disabled = picked.w < 120 || picked.h < 40;
    };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
    e.preventDefault();
  };
  $('#pick').hidden = false;
  const close = () => { $('#pick').hidden = true; shot.onmousedown = null; shot.onwheel = null; Object.assign(Z, { z: 1, x: 0, y: 0 }); apply(); };
  $('#pick-no').onclick = close;
  yes.onclick = async () => {
    yes.disabled = true;
    if (mode === 'pick') {
      const r = await call('set_region', picked);
      if (r.error) { yes.disabled = false; return toast(r.error, 'err'); }
      close(); S.regionPreview = r.preview; toast('채팅 영역을 저장했습니다', 'ok'); refresh(true);
      if (s.report.can && await confirmBox('인식 오류 신고', '이 화면을 보내 자동 찾기를 고치는 데 쓸까요? 게임 화면 1장 · 30일 뒤 삭제', '보내기', '나중에', false)) sendReport(s.found, picked);
    } else { close(); sendReport(s.found, picked); }
  };
}
async function sendReport(found, picked) {
  const r = await call('send_report', found, picked, 'region');
  if (r && r.ok) toast('보냈습니다. 고마워요', 'ok');
  else if (r && r.already) toast(`이미 보냈습니다 · ${r.wait_h}시간 뒤 다시`);
  else toast((r && r.error) || '보내지 못했습니다', 'err');
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#pick').hidden) $('#pick-no').click(); });

/* ---------- 확인 창 ---------- */
function confirmBox(title, text = '', yes = '지우기', no = '취소', danger = true) {
  return new Promise((resolve) => {
    $('#confirm-title').textContent = title; $('#confirm-text').textContent = text; $('#confirm-yes').textContent = yes;
    $('#confirm-no').textContent = no; $('#confirm-yes').className = 'btn ' + (danger ? 'danger' : 'primary');
    $('#confirm').hidden = false; $('#confirm-no').focus();
    const done = (v) => { $('#confirm').hidden = true; $('#confirm-yes').onclick = $('#confirm-no').onclick = null; resolve(v); };
    $('#confirm-yes').onclick = () => done(true);
    $('#confirm-no').onclick = () => done(false);
  });
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('#confirm').hidden) $('#confirm-no').click(); });

/* ---------- 툴팁 · 알림 ---------- */
let tipTimer = null;
document.addEventListener('mouseover', (e) => {
  const el = e.target.closest('[data-tip]'); clearTimeout(tipTimer); const tip = $('#tooltip');
  if (!el) { tip.hidden = true; return; }
  tipTimer = setTimeout(() => {
    tip.textContent = el.dataset.tip; tip.hidden = false;
    const r = el.getBoundingClientRect(), w = tip.offsetWidth;
    tip.style.left = `${Math.min(window.innerWidth - w - 8, Math.max(8, r.left + r.width / 2 - w / 2))}px`;
    tip.style.top = `${r.bottom + 8 + 30 > window.innerHeight ? r.top - 36 : r.bottom + 8}px`;
  }, 400);
});
// 초 → '1<small>h</small> 23<small>m</small>' (plain: '1시간 23분')
function dur(s, plain = false) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (plain) return h ? `${h}시간 ${m}분` : `${m}분`;
  return h ? `${h}<small>h</small> ${m}<small>m</small>` : `${m}<small>m</small>`;
}
const HK_DEFAULT = 'Ctrl+Shift+K';
function renderHotkey() {
  const hk = (S.state && S.state.settings && S.state.settings.hotkey_input) || HK_DEFAULT;
  const b = $('#set-hotkey');
  if (!b.classList.contains('rec')) setHTML(b, hk.split('+').map((k) => `<kbd>${esc(k)}</kbd>`).join(''));
  $('#set-hotkey-reset').hidden = hk === HK_DEFAULT;
  $('#hk-small').textContent = hk;
}
function comboOf(e) {
  const c = e.code || '';
  let key = null;
  if (/^Key[A-Z]$/.test(c)) key = c.slice(3);
  else if (/^Digit\d$/.test(c)) key = c.slice(5);
  else if (/^F([1-9]|1\d|2[0-4])$/.test(c)) key = c;
  else key = { Space: 'Space', Insert: 'Insert', Delete: 'Delete', Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown',
    Pause: 'Pause', Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\', Semicolon: ';',
    Quote: "'", Comma: ',', Period: '.', Slash: '/' }[c] || null;
  if (!key) return null;
  return [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Win', key].filter(Boolean).join('+');
}
function recordHotkey() {
  const b = $('#set-hotkey');
  if (b.classList.contains('rec')) return;
  b.classList.add('rec'); setHTML(b, '<kbd>…</kbd>');
  const done = () => { document.removeEventListener('keydown', onKey, true); b.classList.remove('rec'); renderHotkey(); };
  const onKey = (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.key === 'Escape') return done();
    if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) {  // 누르는 중 — 조합키만 보여 주기
      setHTML(b, [e.ctrlKey && 'Ctrl', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Win'].filter(Boolean).map((k) => `<kbd>${k}</kbd>`).join('') + '<kbd>…</kbd>');
      return;
    }
    const combo = comboOf(e);
    done();
    if (e.key === 'Enter') return toast('Ctrl+Enter 는 게임 중에 이미 됩니다 — 다른 프로그램의 보내기 키라 게임 밖에서는 쓰지 않습니다', 'info');
    if (combo) applyHotkey(combo); else toast('쓸 수 없는 키입니다', 'warn');
  };
  document.addEventListener('keydown', onKey, true);
  b.addEventListener('blur', done, { once: true });
}
async function applyHotkey(combo) {
  const r = await call('set_hotkey', combo);
  if (!r) return;
  if (!r.ok) {
    const why = { invalid: 'Ctrl · Alt · Win 과 함께 누르세요(F1–F12 는 혼자 가능)', taken: '다른 프로그램이 쓰는 조합입니다', reserved: '게임 · 시스템이 쓰는 조합입니다' }[r.why];
    return toast(`${combo} — ${why || '쓸 수 없습니다'}`, 'warn');
  }
  S.state.settings.hotkey_input = r.combo; renderHotkey();
  toast(`보내기 입력창: ${r.combo}`, 'ok');
}
function toast(text, kind = 'info') {
  const t = document.createElement('div'); t.className = 'toast';
  t.innerHTML = `<i class="dot ${kind}"></i><span>${esc(text)}</span>`;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), 3600);
}

/* ---------- 이벤트 ---------- */
function bind() {
  $$('.tabs button').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
  $('#btn-min').onclick = () => call('minimize');
  $('#btn-max').onclick = toggleMax;
  $$('.pywebview-drag-region').forEach((d) => d.addEventListener('dblclick', toggleMax));
  setupResize();
  $('#btn-close').onclick = () => call('close');
  $('#btn-help').onclick = () => coach(true);
  $('#btn-welcome').onclick = () => { save({ welcomed: true }, true); S.step = 0; show('setup'); };
  $('#power').onclick = async () => {
    const lv = S.live;
    if (lv.running.live) { await call('stop', 'live'); S.starting = 0; }
    else {
      if (!setupDone()) { toast('환경 설정을 먼저 마쳐 주세요', 'warn'); show('setup'); S.step = STEPS.findIndex((s) => stepState(s.key) !== 'done'); renderSetup(); return; }
      S.starting = Date.now(); await call('start', 'live');
    }
    refresh();
  };
  $('#sw-input').onclick = async () => {
    const on = !S.live.running.input;
    await call(on ? 'start' : 'stop', 'input'); save({ input_on: on }, true); refresh();
  };
  $('#sw-orig').onclick = () => { save({ show_original: !S.state.settings.show_original }, true); renderHome(); };
  $('#btn-refind').onclick = async () => { await call('refind'); toast('채팅 영역을 다시 찾습니다'); };
  $('.lang-tabs').addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-lang]'); if (!b) return;
    S.feedLang = b.dataset.lang;
    for (const el of S.feedEls?.values() || []) el.remove();
    S.feedEls = new Map(); renderFeed();
  });
  // 환경 설정
  $('#steps').addEventListener('click', (e) => { const b = e.target.closest('[data-step]'); if (b) { S.step = +b.dataset.step; renderSetup(); } });
  $('#step-prev').onclick = () => { S.step = Math.max(0, S.step - 1); renderSetup(); };
  $('#step-next').onclick = () => {
    if (S.step < STEPS.length - 1) { S.step++; renderSetup(); return; }
    show('home'); if (!S.state.settings.onboarded) setTimeout(() => coach(true), 250);
  };
  $('#view-setup').addEventListener('keydown', (e) => {
    const r = e.target.closest('[data-model],[data-game]');
    if (r && e.target === r && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); r.click(); }
  });
  $('#view-setup').addEventListener('click', (e) => {
    const a = e.target.closest('[data-act]'); if (a) return act(a.dataset.act, a.dataset);
    const m = e.target.closest('[data-model]'); if (m) { S.selModel = m.dataset.model; renderSetup(); return; }
    const g = e.target.closest('[data-game]'); if (g) { S.selGame = g.dataset.game; renderSetup(); save({ game_dir: g.dataset.game }, true); }
  });
  // 번역 설정
  $('#set-lang').onchange = (e) => { save({ out_lang: e.target.value }); renderSettings(); };
  $('#set-newest').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ chat_newest: b.dataset.v }); renderSettings(); } };
  $('#set-ads').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ ad_filter: b.dataset.v }); renderSettings(); } };
  $('#set-mode').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ out_mode: b.dataset.v }); renderSettings(); } };
  $('#set-model').onchange = (e) => save({ model: e.target.value });
  $('#set-preload').onclick = () => { save({ preload: !S.state.settings.preload }); renderSettings(); };
  $('#set-orig').onclick = () => { save({ show_original: !S.state.settings.show_original }); renderSettings(); };
  $('#set-ai').onclick = async (e) => {
    const b = e.target.closest('button'); if (!b || b.disabled) return;
    const r = await call('set_ai_lang', b.dataset.v, !b.classList.contains('on'));
    if (r.error) toast(r.error, 'err');
    if (r.started) S.progress.ai = { done: 0, total: r.bytes };
    renderAi();
  };
  $('#set-aigpu').onclick = async () => { await call('save_settings', { ai_gpu: !S.ai.gpu }); renderAi(); };
  $('#set-airemove').onclick = async () => {
    if (!await confirmBox('받은 AI 글자 인식 파일을 지울까요?')) return;
    const r = await call('remove_ai'); if (r.error) toast(r.error, 'err'); renderAi();
  };
  $('#set-people').onclick = (e) => { const b = e.target.closest('button[data-k]'); if (!b) return; const k = b.dataset.k;
    save({ [k]: (S.state.settings[k] || []).filter((n) => n !== b.dataset.n) }); renderSettings(); };
  $('#set-korean').onclick = () => { save({ show_korean: S.state.settings.show_korean === false }); renderSettings(); };
  $('#set-logs').onclick = () => { save({ keep_logs: !S.state.settings.keep_logs }); renderSettings(); };
  $('#set-font').oninput = (e) => { $('#out-font').textContent = e.target.value; save({ overlay_font: +e.target.value }, true); };
  $('#set-alpha').oninput = (e) => { $('#out-alpha').textContent = e.target.value + '%'; save({ overlay_alpha: e.target.value / 100 }, true); };
  $('#set-lines').oninput = (e) => { $('#out-lines').textContent = e.target.value; save({ overlay_lines: +e.target.value }, true); };
  $('#set-device').onchange = (e) => { save({ llm_device: e.target.value }); toast('다음 번역부터 적용 — 모델을 다시 올립니다', 'ok'); setTimeout(() => refresh(true), 800); };
  $('#set-unload').onclick = async () => {
    const live = S.live && S.live.running && (S.live.running.live || S.live.running.input);
    if (live && !(await confirmBox('모델 내리기', '통역 · 입력창을 끄고 내립니다 — 켜 두면 다음 번역에 다시 올라옵니다', '끄고 내리기', '취소', false))) return;
    const r = await call('unload_models');
    toast(r && r.unloaded && r.unloaded.length ? `내렸습니다: ${r.unloaded.join(', ')}` : '올라간 모델이 없습니다', 'ok');
    refresh(true);
  };
  $('#set-runner').onchange = async (e) => {
    const o = S.state.ollama, v = e.target.value;
    if (v === 'watt' && !o.watt_ready) { show('setup'); S.step = STEPS.findIndex((s) => s.key === 'ollama'); renderSetup(); e.target.value = o.mode; return; }
    S.models = null; await call('set_runner_mode', v); refresh(true);
  };
  $('#set-models-pick').onclick = async () => { const r = await call('pick_models_dir'); if (r && r.error) toast(r.error, 'warn'); renderModelsDir(); };
  $('#set-models-reset').onclick = async () => { if (await confirmBox('모델 폴더를 기본으로', '받은 모델을 WATT 데이터 폴더로 옮깁니다', '옮기기', '취소', false)) { await call('set_models_dir', ''); renderModelsDir(); } };
  $('#set-resetpos').onclick = async () => { await call('reset_overlay'); toast('통역 창을 채팅창 위로 옮겼습니다', 'ok'); };
  // 보내기 입력창 단축키 — 눌러서 새 조합을 누른다(Esc 취소). 런처가 다른 프로그램과 겹치는지 보고 저장
  $('#set-hotkey').onclick = () => recordHotkey();
  $('#set-hotkey-reset').onclick = () => applyHotkey('Ctrl+Shift+K');
  $('#set-resetinput').onclick = async () => { await call('reset_input'); toast('입력창은 다음에 열 때 채팅창 위에 뜹니다', 'ok'); };
  $('#set-openlogs').onclick = () => call('open_folder', 'logs');
  $('#set-clear').onclick = async () => {
    if (!(await confirmBox('기록 지우기', '화면 캡처 · 추적 기록 · 번역 기록 · 받은 파일. 설정은 남습니다'))) return;
    const r = await call('clear_logs'); toast(`${mb(r.freed)} 비웠습니다`, 'ok'); renderSettings();
  };
  $('#set-update').onclick = () => { save({ update_check: !S.state.settings.update_check }); renderSettings(); };
  $('#btn-update').onclick = () => applyUpdate();
  $('#btn-check-update').onclick = () => checkUpdate(true);
  $('#btn-cleanup').onclick = async () => {
    const w = (S.state && S.state.watt_installed) || {};
    const parts = [w.models && w.models.length ? `모델 ${w.models.length}` : '', w.ocr && w.ocr.length ? `언어 팩 ${w.ocr.length}` : '',
      w.addons && w.addons.length ? `애드온 ${w.addons.length}` : '', w.ollama ? 'Ollama' : ''].filter(Boolean);
    if (!parts.length) { toast('WATT 가 설치한 것이 없습니다'); return; }
    if (!(await confirmBox('설치한 것 정리', `${parts.join(' · ')} · 원래 있던 것은 그대로 둡니다`, '정리'))) return;
    call('cleanup_installed');
  };
  $('#set-game').onchange = (e) => { save({ game_dir: e.target.value }); S.selGame = e.target.value; renderSettings(); };
  $('#set-pickgame').onclick = () => pickFolder();
  // 용어 사전
  $('#cats').onclick = (e) => { const b = e.target.closest('[data-cat]'); if (b) { S.termCat = b.dataset.cat; renderTerms(); } };
  $('#term-q').oninput = () => renderTerms();
  $('#term-add').onclick = () => editTerm(null);
  $('#term-rows').onclick = (ev) => { const r = ev.target.closest('.tr[data-id]'); if (r) editTerm((S.terms || []).find((t) => t.id === r.dataset.id)); };
  // 바깥 링크는 기본 브라우저로(정해 둔 주소만 — watt/app.py LINKS)
  // 게임 창 고르기(채팅 영역 단계) — 고르면 저장하고 바로 다시 찾기
  document.addEventListener('change', async (e) => {
    if (e.target.id !== 'game-win') return;
    S.windows = await call('pick_window', e.target.value);
    S.regionPreview = null; S.results.region = null; renderSetup();
    act('find_region');
  });
  document.addEventListener('click', (e) => {
    const a = e.target.closest('[data-url]'); if (!a) return;
    e.preventDefault(); call('open_url', a.dataset.url);
  });
  // 온보딩
  $('#coach-next').onclick = () => { if (coachStep < COACH.length - 1) { coachStep++; coach(false); } else coachEnd(); };
  $('#coach-skip').onclick = coachEnd;
  window.addEventListener('resize', () => { if (!$('#coach').hidden) coach(false); });
}

/* ---------- 시작 ---------- */
(async function boot() {
  bind();
  S.api = await getApi();
  const report = (m) => { try { S.api.log && S.api.log(String(m)); } catch (e) { /* 연결 전 */ } };
  window.addEventListener('error', (e) => report(`${e.message} @${e.filename}:${e.lineno}`));
  window.addEventListener('unhandledrejection', (e) => report(`promise: ${e.reason && (e.reason.message || e.reason)}`));
  await refresh(true);
  $('#about-ver').textContent = `${S.state.app.version} · ${S.state.app.portable ? '포터블' : '설치판'} · ${S.state.app.full}`;
  const c = S.state.settings;
  show(!c.welcomed ? 'welcome' : setupDone() ? 'home' : 'setup');
  if (c.welcomed && !setupDone()) S.step = Math.max(0, STEPS.findIndex((s) => stepState(s.key) !== 'done'));
  document.body.classList.remove('booting');
  if (c.input_on && !S.live.running.input && setupDone()) call('start', 'input');
  if (setupDone()) call('resume').then((r) => { if (r && r.live) { S.starting = Date.now(); toast('업데이트 전처럼 통역을 이어서 켭니다', 'ok'); refresh(); } }).catch(() => {});
  if (S.view === 'home' && !c.onboarded) setTimeout(() => coach(true), 400);
  checkUpdate(false).catch(() => {});
  setInterval(() => checkUpdate(false).catch(() => {}), 30 * 60 * 1000);  // 켜 둔 동안에도 새 버전을 알린다(설정 '자동 확인'을 따름)
  setInterval(() => refresh(false).catch(() => {}), 1500);
  setInterval(() => refresh(true).catch(() => {}), 8000);
})();

/* ---------- 미리보기용 가짜 API (브라우저로 열었을 때) ---------- */
let mockTerms = null;
function mockApi() {
  const settings = { model: 'gemma4:12b', out_lang: 'en', out_mode: 'clipboard', overlay_font: 11, overlay_alpha: 0.88, overlay_lines: 10,
    show_original: false, show_korean: true, ad_filter: 'fold', chat_newest: 'bottom', keep_logs: true, preload: true, input_on: true, welcomed: true, onboarded: true, setup_done: true, update_check: true };
  const running = { live: true, input: true };
  const feed = [
    { t: '2026-10-01T00:05:46', lang: 'zh', name: '青山', body: '20LR 求组 AH', ko: '20레벨 사냥꾼, 통곡의 동굴 파티 찾음' },
    { t: '2026-10-01T00:04:11', lang: 'en', name: 'Mirelle', body: 'WTS linen cloth stack 50s', ko: '리넨 옷감 한 묶음 50실버에 팝니다' },
    { t: '2026-10-01T00:03:02', lang: 'ru', name: 'Бродяга', body: 'Кто в Огненную пропасть?', ko: '성난불길 협곡 같이 가실 분?' },
    { t: '2026-10-01T00:02:40', lang: 'zh', name: '影月', body: '哀嚎3=2 来 T,N', ko: '통곡의 동굴 3명 있음, 2명 더 구함. 탱커와 힐러' },
    { t: '2026-09-30T23:58:13', lang: 'en', name: 'Thornvale', body: 'LF tank for WC, pst', ko: '통곡의 동굴 탱커 구함, 귓속말 주세요' },
    { t: '2026-09-30T23:57:30', lang: 'en', name: 'Kaelric', body: 'where do u get that quest', ko: '방금 그 퀘스트 어디서 받아요?' },
    { t: '2026-09-30T23:55:02', lang: 'zh', name: '医生姐姐', body: '奶求组 NY 任务队', ko: '힐러, 성난불길 협곡 퀘스트 파티 찾음' },
    { t: '2026-09-30T23:54:40', lang: 'zh', name: '收白菜出白菜', body: '无限服老友交流群 | 攻略 / 组队 … 加 V ： QCW1392010', ko: '', kind: 'ad' },
  ];
  const state = () => ({
    app: { version: '0.1.0', full: 'WoW AI Translation Tool' }, settings,
    system: { windows: 'Windows 11 (빌드 26200)', ram_gb: 31, gpu: { name: 'NVIDIA GeForce RTX 5080', vram_gb: 15.9 }, free_disk_gb: 764 },
    ocr: { langs: [{ code: 'en-US', label: '영어', installed: true }, { code: 'ko', label: '한국어', installed: true }, { code: 'zh-Hans-CN', label: '중국어(간체)', installed: true }, { code: 'ru-RU', label: '러시아어', installed: true }], missing: [] },
    ollama: { mode: 'system', chosen: '', installed: true, running: true, version: '0.34.4', models: [{ name: 'gemma4:12b' }, { name: 'qwen3:14b' }], loaded: [],
      watt: false, watt_ready: false, download: 1461196158, system: { exe: 'C:\Ollama\ollama.exe', version: '0.34.4' } },
    vram: { total: 15.9, used: 5.7, wow: false, wow_est: 4, watt_loaded: 0, available: 5.4, pick: 'gemma4:e4b' },
    models: [{ name: 'gemma4:12b', label: 'Gemma 4 12B', download_gb: 7.7, vram_gb: 8.1, verified: true, installed: true, recommended: true },
      { name: 'gemma4:e4b', label: 'Gemma 4 E4B', download_gb: 6.6, vram_gb: 7, verified: false, installed: false, recommended: false },
      { name: 'gemma4:e2b', label: 'Gemma 4 E2B', download_gb: 4.6, vram_gb: 5, verified: false, installed: false, recommended: false }],
    game: { exe: 'WowB.exe', flavor: '클래식 베타', w: 2560, h: 1440 }, region: { w: 688, h: 325, line_h: 20, lines: 6 },
    steps: { system: 'done', ocr: 'done', ai: 'todo', ollama: 'done', model: 'done', addon: 'done', region: 'done', test: 'done' },
    running, busy: [], removable_ocr: ['zh-Hans-CN', 'ru-RU'],
  });
  const ok = (v) => Promise.resolve(v);
  return {
    get_state: () => ok(state()),
    get_live: () => ok({ running, live: running.live ? { ocr_ms: 247, started: Date.now() / 1000 - 4980 } : null, feed, today: 128, avg_sec: 1.3, runtime: 8340 }),
    get_games: () => ok([{ dir: 'C:\\Program Files (x86)\\World of Warcraft\\_classic_beta_', label: '클래식 베타', supported: true, running: true, addon: '0.2.0' },
      { dir: 'C:\\Program Files (x86)\\World of Warcraft\\_anniversary_', label: '기념 서버', supported: true, running: false, addon: null },
      { dir: 'C:\\Program Files (x86)\\World of Warcraft\\_retail_', label: '리테일', supported: false, running: false, addon: null }]),
    get_terms: () => (mockTerms ? ok(mockTerms) : fetch('../../translator/wow_terms.json').then((r) => r.json())
      .then((d) => (mockTerms = d.terms.map((x) => ({ ...x, origin: 'base' })))).catch(() => [])),
    save_term: (e) => { const l = (v) => String(v || '').split(',').map((s) => s.trim()).filter(Boolean);
      if (!e.ko.trim()) return ok({ error: '한국 이름이 필요합니다' });
      const n = { ...e, ko_alias: l(e.ko_alias), en_abbr: l(e.en_abbr), zh: l(e.zh), ru: l(e.ru) }; const i = mockTerms.findIndex((x) => x.id === e.id);
      if (i >= 0) mockTerms[i] = { ...mockTerms[i], ...n, origin: mockTerms[i].origin === 'added' ? 'added' : 'edited' }; else mockTerms.push({ ...n, id: 'u-' + Date.now(), origin: 'added' });
      return ok({ id: e.id }); },
    delete_term: (id) => { const i = mockTerms.findIndex((x) => x.id === id); if (mockTerms[i].origin === 'added') mockTerms.splice(i, 1); else mockTerms[i].origin = 'hidden'; return ok({ ok: true }); },
    reset_term: (id) => { const t = mockTerms.find((x) => x.id === id); t.origin = 'base'; return ok({ ok: true }); },
    save_settings: (c) => ok(Object.assign(settings, c)),
    start: (r) => { running[r] = true; return ok(true); }, stop: (r) => { running[r] = false; return ok(false); },
    refind: () => ok(true), open_url: () => ok(true), reset_overlay: () => ok(true), reset_input: () => ok(true), set_hotkey: (c) => ok({ ok: true, combo: c }), models_info: () => ok({ mode: 'watt', dir: 'C:\\Users\\me\\AppData\\Local\\WATT\\models', custom: '', used: 7.2e9, free: 120e9, importable: ['gemma4:e4b'] }), pick_models_dir: () => ok({ cancel: true }), unload_models: () => ok({ unloaded: ['gemma4:12b'], stopped: [] }), set_models_dir: () => ok({}), import_model: () => ok({ started: true }), open_folder: () => ok(true), minimize: () => ok(), close: () => ok(),
    install_ocr: () => ok({ started: true }), install_ollama: () => ok({ started: true }), start_ollama: () => ok({ started: true }),
    pull_model: () => ok({ started: true }), cancel: () => ok(true), delete_model: () => ok({}), remove_addon: () => ok({}),
    remove_ocr: () => ok({ started: true }), uninstall_ollama: () => ok({ started: true }),
    check_update: () => ok({ version: '0.1.3', current: '0.1.2', newer: true }), apply_update: () => ok({ started: true }),
    get_shot: () => ok({ img: 'data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="1600" height="900" fill="#2b3240"/><rect x="10" y="560" width="520" height="260" fill="#000" opacity=".6"/></svg>'), w: 1600, h: 900, found: { x: 10, y: 560, w: 520, h: 260 }, report: { can: true, wait_h: 0 } }),
    set_region: () => ok({ preview: '' }), send_report: () => ok({ ok: true }), restart_update: () => ok({ restarting: '0.1.3' }),
    cleanup_installed: () => ok({ started: true }),
    list_windows: () => ok({ games: [{ exe: 'WowB.exe', title: '월드 오브 워크래프트', w: 1920, h: 1009 }],
      others: [{ exe: 'chrome.exe', title: 'Chrome', w: 1200, h: 900 }], picked: settings.game_exe || '', current: 'WowB.exe' }),
    pick_window: (exe) => { settings.game_exe = exe; return ok({ games: [{ exe: 'WowB.exe', title: '월드 오브 워크래프트', w: 1920, h: 1009 }],
      others: [{ exe: 'chrome.exe', title: 'Chrome', w: 1200, h: 900 }], picked: exe, current: exe || 'WowB.exe' }); },
    get_ai: () => ok({ langs: settings.ai_langs || [], gpu: settings.ai_gpu !== false, runtime: (settings.ai_langs || []).length > 0,
      models: { det: true, ko: false, zh: (settings.ai_langs || []).includes('zh'), ru: false, latin: false }, active: settings.ai_langs || [], active_gpu: true, error: null, disk: (settings.ai_langs || []).length ? 96e6 : 0,
      sizes: { runtime: 25111930, det: 9929594, ko: 13488748, zh: 21234383, ru: 8074092, latin: 7904513 } }),
    set_ai_lang: (l, on) => ok({ langs: settings.ai_langs = ['ko', 'zh', 'ru', 'latin'].filter((x) => x === l ? on : (settings.ai_langs || []).includes(x)) }),
    remove_ai: () => ok({}),
    resume: () => ok({}), enable_ai: () => ok({ langs: settings.ai_langs = ['ko', 'zh', 'ru'] }), toggle_maximize: () => ok(false), get_window_rect: () => ok({ x: 0, y: 0, w: innerWidth, h: innerHeight }), set_window_rect: () => ok(), save_window_rect: () => ok(),
    get_storage: () => ok({ total: 48 * 1024 ** 2, frames: 31 * 1024 ** 2, trace: 9 * 1024 ** 2, downloads: 0 }), clear_logs: () => ok({ freed: 40 * 1024 ** 2 }), use_model: (n) => ok(Object.assign(settings, { model: n })),
    install_addon: () => ok({ version: '0.2.0' }), select_game: (d) => ok(Object.assign(settings, { game_dir: d })),
    pick_game_folder: () => ok({ ok: false, error: '미리보기에서는 폴더를 고를 수 없습니다' }), log: () => ok(), find_region: () => ok({ started: true }), test_translate: () => ok({ started: true }),
  };
}
