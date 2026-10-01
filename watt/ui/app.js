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
    if (ev.type === 'progress') { S.progress[ev.task] = ev; renderSetup(); renderEngine(); renderUpdate(); return; }
    if (ev.type === 'done') {
      delete S.progress[ev.task];
      if (!ev.ok) { toast(ev.error || '실패했습니다', 'err'); S.results[ev.task] = { error: ev.error }; }
      else {
        S.results[ev.task] = ev.result;
        if (ev.task === 'region' && ev.result) { S.regionPreview = ev.result.preview; toast('채팅 영역을 찾았습니다', 'ok'); }
        if (ev.task === 'pull') toast('모델을 받았습니다', 'ok');
        if (ev.task === 'ocr') toast('글자 인식 언어 팩을 확인했습니다', 'ok');
        if (ev.task === 'ollama') toast('AI 실행기를 확인했습니다', 'ok');
        if (ev.task === 'cleanup') toast('WATT 가 설치한 것을 정리했습니다', 'ok');
        if (ev.task === 'update' && ev.result && ev.result.opened) toast('릴리스 페이지를 열었습니다');
        if (ev.task === 'update' && ev.result && ev.result.ready) { if (S.update) S.update.ready = ev.result.ready; askRestart(ev.result.ready); }
      }
      refresh(true);
    }
  },
};

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
}
function renderAll() { renderEngine(); renderHome(); renderUpdate(); if (S.view === 'setup') renderSetup(); $('#setup-dot').hidden = setupDone(); }
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
  // 상태
  const ocrOk = st.ocr.langs.filter((l) => l.installed).length;
  const ocrMiss = st.ocr.langs.filter((l) => !l.installed).map((l) => l.label);
  stat('#st-ai', st.ollama.running ? (st.models.find((x) => x.name === st.settings.model)?.installed ? 'ok' : 'warn') : 'err',
    st.ollama.running ? st.settings.model : '꺼짐');
  stat('#st-ocr', ocrMiss.length ? 'warn' : 'ok', ocrMiss.length ? `${ocrMiss[0]} 없음` : `${ocrOk}개 언어`);
  stat('#st-game', st.game ? 'ok' : 'off', st.game ? (st.game.flavor || st.game.exe) : '꺼짐');
  stat('#st-region', st.region ? 'ok' : 'warn', st.region ? `${st.region.w}×${st.region.h}` : '찾기 필요');
  renderFeed();
}
function stat(sel, kind, value) {
  const el = $(sel); el.className = 'stat ' + (kind === 'warn' ? 'warn' : kind === 'off' ? 'off' : '');
  $('.dot', el).className = 'dot ' + (kind === 'off' ? '' : kind); $('b', el).textContent = value;
}
function setSwitch(sel, v) { $(sel).setAttribute('aria-checked', !!v); }
function renderFeed() {
  const items = (S.live.feed || []).filter((f) => S.feedLang === 'all' || f.lang === S.feedLang);
  const showOrig = true;
  setHTML($('#feed'), items.map((f) => `
    <article class="item"><span class="code ${esc(f.lang)}">${esc((f.lang || '').toUpperCase())}</span>
      <div>${f.kind === 'ad' && !f.ko ? '<div class="ko ad">광고</div>' : `<div class="ko">${esc(f.ko)}</div>`}${showOrig ? `<div class="orig"><b>${esc(f.name)}</b>${esc(f.body)}</div>` : ''}</div>
      <span class="time">${esc((f.t || '').slice(11, 16))}</span></article>`).join(''));
  $('#feed-empty').hidden = items.length > 0;
}

/* ---------- 환경 설정 ---------- */
const STEPS = [
  { key: 'system', name: '내 PC', icon: 'i-pc' },
  { key: 'ocr', name: '글자 인식', icon: 'i-ocr' },
  { key: 'ollama', name: 'AI 실행기', icon: 'i-box' },
  { key: 'model', name: '번역 모델', icon: 'i-download' },
  { key: 'addon', name: '게임 · 글꼴', icon: 'i-game' },
  { key: 'region', name: '채팅 영역', icon: 'i-frame' },
  { key: 'test', name: '번역 시험', icon: 'i-test' },
];
const TASK_OF = { ocr: 'ocr', ollama: 'ollama', model: 'pull', region: 'region', test: 'test' };
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
    return `<li><button data-step="${i}" class="${i === S.step ? 'on' : ''}">${icon(s.icon)}<span>${esc(s.name)}</span>
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
    const action = miss.length ? `<button class="btn primary" data-act="install_ocr" ${run ? 'disabled' : ''} data-tip="Windows 권한 확인 창이 뜹니다">${icon('i-shield', 'sm')}${run ? '설치 중' : '설치'}</button>` : '';
    return [`<div class="chips">${chips}</div>${run ? '<div class="progress indet"><i></i></div>' : ''}`, action];
  },
  ollama() {
    const o = S.state.ollama, p = S.progress.ollama, run = p || S.state.busy.includes('ollama');
    const chips = `<div class="chips">
      <span class="chip ${o.installed ? 'ok' : 'bad'}">${icon(o.installed ? 'i-check' : 'i-box')}설치</span>
      <span class="chip ${o.running ? 'ok' : 'bad'}">${icon(o.running ? 'i-check' : 'i-power')}실행</span>
      ${o.version ? `<span class="chip mono">${esc(o.version)}</span>` : ''}</div>`;
    let prog = '';
    if (run) prog = p && p.total ? `<div class="box"><div class="head"><span class="grow mono muted">OllamaSetup.exe</span><span class="pct">${pct(p)}%</span></div>
      <div class="progress"><i style="width:${pct(p)}%"></i></div></div>` : '<div class="progress indet"><i></i></div>';
    const remove = o.installed && o.watt && !run ? `<button class="btn ghost-danger" data-act="uninstall_ollama" data-tip="WATT 가 설치한 Ollama 를 제거합니다">제거</button>` : '';
    const action = !o.installed ? `<button class="btn primary" data-act="install_ollama" ${run ? 'disabled' : ''}>${icon('i-download', 'sm')}설치</button>`
      : `<span class="inline">${remove}${!o.running ? `<button class="btn primary" data-act="start_ollama" ${run ? 'disabled' : ''}>${icon('i-power', 'sm')}켜기</button>` : ''}</span>`;
    return [chips + prog, action];
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
    const action = p || st.busy.includes('pull') ? ''
      : !m.installed ? `<button class="btn primary" data-act="pull_model">${icon('i-download', 'sm')}받기</button>`
      : sel !== st.settings.model ? `<button class="btn primary" data-act="use_model">사용</button>` : '<span class="badge ok">사용 중</span>';
    const terms = `<p class="terms-line">받으면 Google <a href="#" data-url="https://ai.google.dev/gemma/terms">Gemma 이용 약관</a>에 동의하는 것으로 봅니다</p>`;
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
    const facts = r ? `<div class="facts"><span class="okx">${icon('i-check', 'sm')}${r.w}×${r.h}</span><span>${r.line_h}px</span>${r.lines ? `<span>${r.lines}줄</span>` : ''}</div>` : '';
    const err = res && res.error ? `<div class="err-text">${esc(res.error)}</div>` : '';
    const tips = [['i-bg', '검정 배경', '채팅 배경을 불투명한 검정으로'], ['i-type', '글자 14+', '글자 크기 14 이상'],
      ['i-eyeoff', '사라짐 끄기', '채팅 글자 사라짐(페이드) 끄기'], ['i-tab', '전용 탭', '번역할 채널만 모은 채팅 탭']]
      .map(([i, l, t]) => `<span class="chip" data-tip="${esc(t)}">${icon(i, 'sm')}${l}</span>`).join('');
    const action = `<span class="inline"><button class="icon-btn sq" data-act="pick_region" ${run ? 'disabled' : ''} aria-label="직접 지정" data-tip="게임 화면에서 채팅창을 끌어서 지정">${icon('i-crop', 'sm')}</button>
      <button class="icon-btn sq" data-act="report_region" ${run ? 'disabled' : ''} aria-label="인식 오류 신고" data-tip="잘못 찾았을 때 화면을 보내 고치는 데 쓰기">${icon('i-flag', 'sm')}</button>
      <button class="btn ${r ? '' : 'primary'}" data-act="find_region" ${run ? 'disabled' : ''} data-tip="게임이 켜져 있어야 합니다">${icon('i-target', 'sm')}${run ? '찾는 중' : r ? '다시 찾기' : '찾기'}</button></span>`;
    return [`<div class="preview">${img}</div>${run ? '<div class="progress indet"><i></i></div>' : facts}${err}<div class="chips">${tips}</div>`, action];
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
  if (name === 'install_ollama') return call('install_ollama');
  if (name === 'start_ollama') return call('start_ollama');
  if (name === 'pull_model') { S.progress.pull = { model: S.selModel || st.settings.model, done: 0, total: 0, status: '준비 중' }; renderSetup(); return call('pull_model', S.selModel || st.settings.model); }
  if (name === 'cancel_pull') return call('cancel', 'pull');
  if (name === 'use_model') { await call('use_model', S.selModel); toast('모델을 바꿨습니다', 'ok'); return refresh(true); }
  if (name === 'pick_folder') return pickFolder();
  if (name === 'install_addon') { const r = await call('install_addon', S.selGame); toast(`글꼴 애드온 ${r.version || ''} 설치됨. 게임을 다시 켜 주세요`, 'ok'); return loadGames().then(() => refresh(true)); }
  if (name === 'find_region') { S.results.region = null; return call('find_region'); }
  if (name === 'pick_region') return openShot('pick');
  if (name === 'report_region') return openShot('report');
  if (name === 'test_translate') { S.testKo = ($('#test-ko') || {}).value || ''; S.results.test = null; return call('test_translate', S.testKo); }
}

/* ---------- 번역 설정 ---------- */
function renderSettings() {
  const c = S.state && S.state.settings; if (!c) return;
  const games = (S.games || []).filter((g) => g.supported);
  const cur = c.game_dir || defaultGame(S.games || []) || '';
  setHTML($('#set-game'), games.length ? games.map((g) => `<option value="${esc(g.dir)}" ${g.dir === cur ? 'selected' : ''}>${esc(g.label)}</option>`).join('')
    : '<option value="">없음</option>');
  $('#set-gamedir').textContent = cur || '-';
  $('#set-gamedir').dataset.tip = cur || '게임 폴더를 지정해 주세요';
  $$('#set-lang button').forEach((b) => b.classList.toggle('on', b.dataset.v === c.out_lang));
  $$('#set-mode button').forEach((b) => b.classList.toggle('on', b.dataset.v === c.out_mode));
  $$('#set-ads button').forEach((b) => b.classList.toggle('on', b.dataset.v === (c.ad_filter || 'fold')));
  $$('#set-newest button').forEach((b) => b.classList.toggle('on', b.dataset.v === (c.chat_newest || 'bottom')));
  const sel = $('#set-model');
  const names = S.state.ollama.models.map((m) => m.name);
  if (!names.includes(c.model)) names.unshift(c.model);
  sel.innerHTML = names.map((n) => `<option ${n === c.model ? 'selected' : ''}>${esc(n)}</option>`).join('');
  setSwitch('#set-update', c.update_check);
  call('get_storage').then((u) => { $('#set-usage').textContent = mb(u.total); $('#set-usage').dataset.tip = `화면 캡처 ${mb(u.frames)} · 추적 ${mb(u.trace)} · 받은 파일 ${mb(u.downloads)}`; }).catch(() => {});
  setSwitch('#set-preload', c.preload); setSwitch('#set-orig', c.show_original); setSwitch('#set-logs', c.keep_logs);
  $('#set-font').value = c.overlay_font; $('#out-font').textContent = c.overlay_font;
  $('#set-alpha').value = Math.round(c.overlay_alpha * 100); $('#out-alpha').textContent = Math.round(c.overlay_alpha * 100) + '%';
  $('#set-lines').value = c.overlay_lines; $('#out-lines').textContent = c.overlay_lines;
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
  ['chat', '채팅 말', (t) => t.type === 'chat'], ['item', '아이템', (t) => t.type === 'item'], ['verify', '확인 필요', (t) => (t.verify || []).length > 0]];
async function loadTerms() { if (!S.terms) S.terms = await call('get_terms'); renderTerms(); }
function renderTerms() {
  const terms = S.terms || [];
  $('#cats').innerHTML = CATS.map(([k, l, f]) => `<button role="tab" data-cat="${k}" class="${k === S.termCat ? 'on' : ''}">${l}<span>${terms.filter(f).length}</span></button>`).join('');
  const q = $('#term-q').value.trim().toLowerCase();
  const f = CATS.find((c) => c[0] === S.termCat)[2];
  const rows = terms.filter(f).filter((t) => !q || JSON.stringify(t).toLowerCase().includes(q));
  $('#term-rows').innerHTML = rows.map((t) => {
    const abbr = (t.en_abbr || [])[0] || '';
    const en = t.en.replace(/\s*\(.*\)$/, '');
    const verify = (t.verify || []).length ? ' <span class="badge warn">확인 필요</span>' : '';
    return `<div class="tr"><span class="ko">${esc(t.ko)}</span><span><span class="alias">${esc((t.ko_alias || []).slice(0, 2).join(', '))}</span>${verify}</span>
      <span><span class="abbr">${esc(abbr)}</span> <span class="dimtx">${abbr.toLowerCase() === en.toLowerCase() ? '' : esc(en)}</span></span>
      <span class="other">${esc([...(t.zh || []).slice(0, 2), ...(t.zh_only ? (t.en_abbr || []) : [])].join(' · '))}</span>
      <span class="other">${esc((t.ru || [])[0] || '')}</span></div>`;
  }).join('');
}

/* ---------- 온보딩 ---------- */
const COACH = [
  { sel: '#power', circle: true, title: '통역 켜기 · 끄기', text: '게임 채팅을 한국어로 띄웁니다' },
  { sel: '#row-input', title: '보내기 입력창', text: '한국어로 쓰면 영어로 바꿔 줍니다. Ctrl+Shift+K' },
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
  let picked = mode === 'report' ? s.found : null;
  $('#pick-img').src = s.img;
  place(found, mode === 'pick' ? s.found : null);
  place(box, picked);
  $('#pick-title').textContent = mode === 'pick' ? '채팅 영역 직접 지정' : '인식 오류 신고';
  $('#pick-text').textContent = mode === 'pick' ? '채팅창을 끌어서 고르세요' : '이 화면을 보내 채팅 영역 자동 찾기를 고치는 데 씁니다';
  const blocked = mode === 'report' && !s.report.can;
  $('#pick-note').textContent = blocked ? `이미 보냈습니다 · ${s.report.wait_h}시간 뒤 다시` : mode === 'report' ? '게임 화면 1장 · 다른 사람 이름이 보일 수 있음 · 30일 뒤 삭제' : '';
  yes.textContent = mode === 'pick' ? '저장' : '보내기';
  yes.disabled = mode === 'pick' || blocked;
  shot.classList.toggle('drag', mode === 'pick');
  const at = (e) => { const b = shot.getBoundingClientRect(); return { x: Math.max(0, Math.min(b.width, e.clientX - b.left)) / b.width * s.w, y: Math.max(0, Math.min(b.height, e.clientY - b.top)) / b.height * s.h }; };
  shot.onmousedown = mode !== 'pick' ? null : (e) => {
    const a = at(e);
    const move = (ev) => { const c = at(ev); picked = { x: Math.min(a.x, c.x), y: Math.min(a.y, c.y), w: Math.abs(c.x - a.x), h: Math.abs(c.y - a.y) }; place(box, picked); yes.disabled = picked.w < 120 || picked.h < 40; };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  };
  $('#pick').hidden = false;
  const close = () => { $('#pick').hidden = true; shot.onmousedown = null; };
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
function toast(text, kind = 'info') {
  const t = document.createElement('div'); t.className = 'toast';
  t.innerHTML = `<i class="dot ${kind}"></i><span>${esc(text)}</span>`;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), 3600);
}

/* ---------- 이벤트 ---------- */
function bind() {
  $$('.tabs button').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
  $('#btn-min').onclick = () => call('minimize');
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
  $$('.lang-tabs button').forEach((b) => b.addEventListener('click', () => {
    S.feedLang = b.dataset.lang; $$('.lang-tabs button').forEach((x) => x.classList.toggle('on', x === b)); renderFeed();
  }));
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
  $('#set-lang').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ out_lang: b.dataset.v }); renderSettings(); } };
  $('#set-newest').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ chat_newest: b.dataset.v }); renderSettings(); } };
  $('#set-ads').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ ad_filter: b.dataset.v }); renderSettings(); } };
  $('#set-mode').onclick = (e) => { const b = e.target.closest('button'); if (b) { save({ out_mode: b.dataset.v }); renderSettings(); } };
  $('#set-model').onchange = (e) => save({ model: e.target.value });
  $('#set-preload').onclick = () => { save({ preload: !S.state.settings.preload }); renderSettings(); };
  $('#set-orig').onclick = () => { save({ show_original: !S.state.settings.show_original }); renderSettings(); };
  $('#set-logs').onclick = () => { save({ keep_logs: !S.state.settings.keep_logs }); renderSettings(); };
  $('#set-font').oninput = (e) => { $('#out-font').textContent = e.target.value; save({ overlay_font: +e.target.value }, true); };
  $('#set-alpha').oninput = (e) => { $('#out-alpha').textContent = e.target.value + '%'; save({ overlay_alpha: e.target.value / 100 }, true); };
  $('#set-lines').oninput = (e) => { $('#out-lines').textContent = e.target.value; save({ overlay_lines: +e.target.value }, true); };
  $('#set-resetpos').onclick = async () => { await call('reset_overlay'); toast('통역 창을 채팅창 위로 옮겼습니다', 'ok'); };
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
  // 바깥 링크는 기본 브라우저로(정해 둔 주소만 — watt/app.py LINKS)
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
  if (S.view === 'home' && !c.onboarded) setTimeout(() => coach(true), 400);
  checkUpdate(false).catch(() => {});
  setInterval(() => refresh(false).catch(() => {}), 1500);
  setInterval(() => refresh(true).catch(() => {}), 8000);
})();

/* ---------- 미리보기용 가짜 API (브라우저로 열었을 때) ---------- */
function mockApi() {
  const settings = { model: 'gemma4:12b', out_lang: 'en', out_mode: 'clipboard', overlay_font: 11, overlay_alpha: 0.88, overlay_lines: 10,
    show_original: false, ad_filter: 'fold', chat_newest: 'bottom', keep_logs: true, preload: true, input_on: true, welcomed: true, onboarded: true, setup_done: true, update_check: true };
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
    ollama: { installed: true, running: true, version: '0.34.4', models: [{ name: 'gemma4:12b' }, { name: 'qwen3:14b' }], loaded: [] },
    models: [{ name: 'gemma4:12b', label: 'Gemma 4 12B', download_gb: 7.7, vram_gb: 8.1, verified: true, installed: true, recommended: true },
      { name: 'gemma4:e4b', label: 'Gemma 4 E4B', download_gb: 6.6, vram_gb: 7, verified: false, installed: false, recommended: false },
      { name: 'gemma4:e2b', label: 'Gemma 4 E2B', download_gb: 4.6, vram_gb: 5, verified: false, installed: false, recommended: false }],
    game: { exe: 'WowB.exe', flavor: '클래식 베타', w: 2560, h: 1440 }, region: { w: 688, h: 325, line_h: 20, lines: 6 },
    steps: { system: 'done', ocr: 'done', ollama: 'done', model: 'done', addon: 'done', region: 'done', test: 'done' },
    running, busy: [], removable_ocr: ['zh-Hans-CN', 'ru-RU'],
  });
  const ok = (v) => Promise.resolve(v);
  return {
    get_state: () => ok(state()),
    get_live: () => ok({ running, live: running.live ? { ocr_ms: 247 } : null, feed, today: 128, avg_sec: 1.3 }),
    get_games: () => ok([{ dir: 'C:\\Program Files (x86)\\World of Warcraft\\_classic_beta_', label: '클래식 베타', supported: true, running: true, addon: '0.2.0' },
      { dir: 'C:\\Program Files (x86)\\World of Warcraft\\_anniversary_', label: '기념 서버', supported: true, running: false, addon: null },
      { dir: 'C:\\Program Files (x86)\\World of Warcraft\\_retail_', label: '리테일', supported: false, running: false, addon: null }]),
    get_terms: () => fetch('../../translator/wow_terms.json').then((r) => r.json()).then((d) => d.terms).catch(() => []),
    save_settings: (c) => ok(Object.assign(settings, c)),
    start: (r) => { running[r] = true; return ok(true); }, stop: (r) => { running[r] = false; return ok(false); },
    refind: () => ok(true), open_url: () => ok(true), reset_overlay: () => ok(true), open_folder: () => ok(true), minimize: () => ok(), close: () => ok(),
    install_ocr: () => ok({ started: true }), install_ollama: () => ok({ started: true }), start_ollama: () => ok({ started: true }),
    pull_model: () => ok({ started: true }), cancel: () => ok(true), delete_model: () => ok({}), remove_addon: () => ok({}),
    remove_ocr: () => ok({ started: true }), uninstall_ollama: () => ok({ started: true }),
    check_update: () => ok({ version: '0.1.3', current: '0.1.2', newer: true }), apply_update: () => ok({ started: true }),
    get_shot: () => ok({ img: 'data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="1600" height="900" fill="#2b3240"/><rect x="10" y="560" width="520" height="260" fill="#000" opacity=".6"/></svg>'), w: 1600, h: 900, found: { x: 10, y: 560, w: 520, h: 260 }, report: { can: true, wait_h: 0 } }),
    set_region: () => ok({ preview: '' }), send_report: () => ok({ ok: true }), restart_update: () => ok({ restarting: '0.1.3' }),
    cleanup_installed: () => ok({ started: true }),
    get_storage: () => ok({ total: 48 * 1024 ** 2, frames: 31 * 1024 ** 2, trace: 9 * 1024 ** 2, downloads: 0 }), clear_logs: () => ok({ freed: 40 * 1024 ** 2 }), use_model: (n) => ok(Object.assign(settings, { model: n })),
    install_addon: () => ok({ version: '0.2.0' }), select_game: (d) => ok(Object.assign(settings, { game_dir: d })),
    pick_game_folder: () => ok({ ok: false, error: '미리보기에서는 폴더를 고를 수 없습니다' }), log: () => ok(), find_region: () => ok({ started: true }), test_translate: () => ok({ started: true }),
  };
}
