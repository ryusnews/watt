// WATT 받는 서버 (Cloudflare Worker)
//   POST /v1/terms            사전 후보(#7) — 동의한 설치만, 하루 1번
//   POST /v1/report           인식 오류 신고(#14) — 이미지 + 영역 정보, 설치마다 24시간에 1번
//   GET  /v1/health
//   /v1/admin/*               관리(Authorization: Bearer ADMIN_TOKEN)
// 사람 이름 · 문장 · IP 원문은 저장하지 않는다. 설치 ID · IP 는 SALT 해시로만.

const LANGS = new Set(['en', 'zh', 'ru', 'es', 'de', 'fr', 'pt', 'ja', 'ko', 'other']);
const INSTALL = /^[A-Za-z0-9-]{16,64}$/;
const TERMS_MAX_BYTES = 16 * 1024;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '');
    try {
      if (path === '/v1/health') return json({ ok: true });
      if (path === '/v1/terms' && request.method === 'POST') return await terms(request, env);
      if (path === '/v1/report' && request.method === 'POST') return await report(request, env);
      if (path.startsWith('/v1/admin/')) {
        if (!(await isAdmin(request, env))) return json({ error: 'unauthorized' }, 401);
        return await admin(request, env, path.slice('/v1/admin/'.length), url);
      }
      return json({ error: 'not found' }, 404);
    } catch (e) {
      console.error(path, e && e.stack || e);
      return json({ error: 'server' }, 500);
    }
  },

  async scheduled(_event, env) {
    await cleanup(env);
  },
};

// ---- 사전 후보(#7)
async function terms(request, env) {
  if (+(request.headers.get('content-length') || 0) > TERMS_MAX_BYTES) return json({ error: 'too large' }, 413);
  const text = await request.text();
  if (text.length > TERMS_MAX_BYTES) return json({ error: 'too large' }, 413);
  let body;
  try { body = JSON.parse(text); } catch { return json({ error: 'bad json' }, 400); }
  if (!INSTALL.test(body.install || '') || !Array.isArray(body.items)) return json({ error: 'bad request' }, 400);
  const items = body.items.slice(0, +env.TERMS_ITEMS_MAX).map(cleanTerm).filter(Boolean);
  if (!items.length) return json({ ok: true, accepted: 0 });

  const install = await hashed(env, 'i:' + body.install);
  const ip = await hashed(env, 'ip:' + (request.headers.get('cf-connecting-ip') || ''));
  if (!(await bump(env, 't:i:' + install, 1))) return json({ error: 'already', retry: 'tomorrow' }, 429);
  if (!(await bump(env, 't:ip:' + ip, +env.TERMS_IP_DAY))) return json({ error: 'limit' }, 429);
  if (!(await bump(env, 't:all', +env.TERMS_ALL_DAY))) return json({ error: 'busy' }, 503);

  const d = today();
  const stmts = [];
  for (const it of items) {
    const ctx = it.ctx.join(' ');
    // 설치 수는 이 설치가 처음 본 말일 때만 +1 — term_installs 넣기 전에 센다
    stmts.push(env.DB.prepare(
      `INSERT INTO terms (term, lang, installs, hits, ctx, first_day, last_day)
       VALUES (?1, ?2, 1, ?3, CASE WHEN ?4 = '' THEN '[]' ELSE json_array(?4) END, ?5, ?5)
       ON CONFLICT (term, lang) DO UPDATE SET
         hits = hits + excluded.hits,
         last_day = excluded.last_day,
         installs = installs + (SELECT CASE WHEN EXISTS
           (SELECT 1 FROM term_installs WHERE term = ?1 AND lang = ?2 AND install = ?6) THEN 0 ELSE 1 END),
         ctx = CASE WHEN ?4 != '' AND json_array_length(ctx) < 5 AND NOT EXISTS
           (SELECT 1 FROM json_each(ctx) WHERE value = ?4) THEN json_insert(ctx, '$[#]', ?4) ELSE ctx END`
    ).bind(it.term, it.lang, it.n, ctx, d, install));
    stmts.push(env.DB.prepare(
      'INSERT OR IGNORE INTO term_installs (term, lang, install, day) VALUES (?, ?, ?, ?)'
    ).bind(it.term, it.lang, install, d));
  }
  await env.DB.batch(stmts);
  return json({ ok: true, accepted: items.length });
}

function cleanTerm(it) {
  if (!it || typeof it.term !== 'string' || !LANGS.has(it.lang)) return null;
  const term = it.term.trim().normalize('NFC');
  if (!term || term.length > 32 || /\s{2,}/.test(term)) return null;
  if (/\d{5,}|https?:|www\.|\.(com|net|gg|cn|ru)\b|@/i.test(term)) return null;  // 연락처 · 주소처럼 생긴 것
  const ctx = (Array.isArray(it.ctx) ? it.ctx : [])
    .filter((w) => typeof w === 'string').map((w) => w.trim()).filter((w) => w && w.length <= 24 && !/\d{5,}/.test(w))
    .slice(0, 6);
  if (ctx.join(' ').length > 80) ctx.length = 0;
  const n = Math.min(1000, Math.max(1, parseInt(it.n, 10) || 1));
  return { term, lang: it.lang, ctx, n };
}

// ---- 인식 오류 신고(#14)
async function report(request, env) {
  const max = +env.REPORT_MAX_BYTES;
  if (+(request.headers.get('content-length') || 0) > max + 64 * 1024) return json({ error: 'too large' }, 413);
  let form;
  try { form = await request.formData(); } catch { return json({ error: 'bad form' }, 400); }
  const raw = form.get('meta');
  const image = form.get('image');
  if (typeof raw !== 'string' || raw.length > 4096 || !image || typeof image === 'string') return json({ error: 'bad request' }, 400);
  let meta;
  try { meta = JSON.parse(raw); } catch { return json({ error: 'bad json' }, 400); }
  if (!INSTALL.test(meta.install || '')) return json({ error: 'bad install' }, 400);
  if (image.size > max) return json({ error: 'too large' }, 413);
  const bytes = new Uint8Array(await image.arrayBuffer());
  const kind = imageKind(bytes);
  if (!kind) return json({ error: 'not an image' }, 415);

  const id = await sha256(bytes);
  if (await env.DB.prepare('SELECT 1 FROM reports WHERE id = ?').bind(id).first()) {
    return json({ error: 'already', already: true }, 409);
  }
  const install = await hashed(env, 'i:' + meta.install);
  const now = Date.now();
  const every = +env.REPORT_EVERY_HOURS * 3600 * 1000;
  const last = await env.DB.prepare('SELECT at FROM reports WHERE install = ? ORDER BY at DESC LIMIT 1').bind(install).first();
  if (last && now - last.at < every) {
    return json({ error: 'already', already: true, retry_after: Math.ceil((every - (now - last.at)) / 1000) }, 429);
  }
  const ip = await hashed(env, 'ip:' + (request.headers.get('cf-connecting-ip') || ''));
  if (!(await bump(env, 'r:ip:' + ip, +env.REPORT_IP_DAY))) return json({ error: 'limit' }, 429);
  if (!(await bump(env, 'r:all', +env.REPORT_ALL_DAY))) return json({ error: 'busy' }, 503);

  const d = today();
  const key = `reports/${d}/${id}.${kind.ext}`;
  await env.REPORTS.put(key, bytes, { httpMetadata: { contentType: kind.type } });
  await env.DB.prepare(
    'INSERT INTO reports (id, at, day, install, ver, meta, r2key, size) VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
  ).bind(id, now, d, install, String(meta.ver || '').slice(0, 16), JSON.stringify(cleanMeta(meta)), key, bytes.length).run();
  return json({ ok: true, id }, 201);
}

function imageKind(b) {
  if (b.length > 8 && b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return { ext: 'png', type: 'image/png' };
  if (b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return { ext: 'jpg', type: 'image/jpeg' };
  return null;
}

function cleanMeta(m) {
  // 알려진 칸만, 숫자 · 짧은 글자만 남긴다
  const rect = (r) => r && typeof r === 'object'
    ? Object.fromEntries(['x', 'y', 'w', 'h'].map((k) => [k, Math.round(+r[k] || 0)])) : null;
  const short = (s, n = 40) => (typeof s === 'string' ? s.slice(0, n) : null);
  return {
    found: rect(m.found), picked: rect(m.picked), window: rect(m.window), image: rect(m.image),
    scale: +m.scale || null, dpi: +m.dpi || null, lines: Math.round(+m.lines || 0),
    engines: Array.isArray(m.engines) ? m.engines.slice(0, 8).map((e) => short(e, 16)) : [],
    flavor: short(m.flavor), reason: short(m.reason, 24), os: short(m.os),
  };
}

// ---- 관리
async function admin(request, env, path, url) {
  const parts = path.split('/');
  if (path === 'stats' && request.method === 'GET') {
    const q = (sql) => env.DB.prepare(sql).first();
    return json({
      reports: await q("SELECT count(*) AS n, sum(status = 'new') AS new, sum(size) AS bytes FROM reports"),
      terms: await q("SELECT count(*) AS n, sum(status = 'new') AS new FROM terms"),
      today: (await env.DB.prepare('SELECT key, n FROM quota WHERE day = ? AND key IN (?, ?)').bind(today(), 'r:all', 't:all').all()).results,
    });
  }
  if (parts[0] === 'reports') {
    if (parts.length === 1 && request.method === 'GET') {
      const status = url.searchParams.get('status') || 'new';
      const limit = Math.min(200, +url.searchParams.get('limit') || 50);
      const rows = await env.DB.prepare('SELECT id, at, day, ver, meta, size, status FROM reports WHERE status = ? ORDER BY at DESC LIMIT ?')
        .bind(status, limit).all();
      return json(rows.results.map((r) => ({ ...r, meta: JSON.parse(r.meta) })));
    }
    const row = await env.DB.prepare('SELECT * FROM reports WHERE id = ?').bind(parts[1] || '').first();
    if (!row) return json({ error: 'not found' }, 404);
    if (parts[2] === 'image' && request.method === 'GET') {
      const obj = await env.REPORTS.get(row.r2key);
      if (!obj) return json({ error: 'gone' }, 410);
      return new Response(obj.body, { headers: { 'content-type': obj.httpMetadata?.contentType || 'application/octet-stream' } });
    }
    if (parts.length === 2 && request.method === 'POST') {
      const { status } = await request.json();
      if (!['new', 'seen', 'fixed', 'invalid'].includes(status)) return json({ error: 'bad status' }, 400);
      await env.DB.prepare('UPDATE reports SET status = ? WHERE id = ?').bind(status, row.id).run();
      return json({ ok: true });
    }
    if (parts.length === 2 && request.method === 'DELETE') {
      await env.REPORTS.delete(row.r2key);
      await env.DB.prepare('DELETE FROM reports WHERE id = ?').bind(row.id).run();
      return json({ ok: true });
    }
  }
  if (path === 'terms' && request.method === 'GET') {
    const min = Math.max(1, +url.searchParams.get('min') || 2);
    const status = url.searchParams.get('status') || 'new';
    const rows = await env.DB.prepare(
      'SELECT term, lang, installs, hits, ctx, status, first_day, last_day FROM terms WHERE status = ? AND installs >= ? ORDER BY installs DESC, hits DESC LIMIT 500'
    ).bind(status, min).all();
    return json(rows.results.map((r) => ({ ...r, ctx: JSON.parse(r.ctx) })));
  }
  if (path === 'terms' && request.method === 'POST') {
    const { term, lang, status } = await request.json();
    if (!['new', 'added', 'rejected'].includes(status)) return json({ error: 'bad status' }, 400);
    await env.DB.prepare('UPDATE terms SET status = ? WHERE term = ? AND lang = ?').bind(status, term, lang).run();
    return json({ ok: true });
  }
  return json({ error: 'not found' }, 404);
}

async function isAdmin(request, env) {
  const got = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  if (!env.ADMIN_TOKEN || !got) return false;
  const a = new TextEncoder().encode(await sha256(new TextEncoder().encode(got)));
  const b = new TextEncoder().encode(await sha256(new TextEncoder().encode(env.ADMIN_TOKEN)));
  return crypto.subtle.timingSafeEqual(a, b);
}

// ---- 매일 정리: 오래된 신고(이미지 포함) · 횟수 · 설치 기록
async function cleanup(env) {
  const keepFrom = dayOffset(-(+env.KEEP_DAYS));
  for (;;) {
    const old = await env.DB.prepare('SELECT id, r2key FROM reports WHERE day < ? LIMIT 100').bind(keepFrom).all();
    if (!old.results.length) break;
    await env.REPORTS.delete(old.results.map((r) => r.r2key));
    await env.DB.batch(old.results.map((r) => env.DB.prepare('DELETE FROM reports WHERE id = ?').bind(r.id)));
  }
  await env.DB.prepare('DELETE FROM quota WHERE day < ?').bind(dayOffset(-2)).run();
  await env.DB.prepare('DELETE FROM term_installs WHERE day < ?').bind(dayOffset(-90)).run();
}

// ---- 도우미
async function bump(env, key, limit) {
  const r = await env.DB.prepare(
    'INSERT INTO quota (day, key, n) VALUES (?, ?, 1) ON CONFLICT (day, key) DO UPDATE SET n = n + 1 RETURNING n'
  ).bind(today(), key).first();
  return r.n <= limit;
}

async function hashed(env, s) {
  return (await sha256(new TextEncoder().encode((env.SALT || '') + s))).slice(0, 32);
}

async function sha256(bytes) {
  const h = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(h)].map((x) => x.toString(16).padStart(2, '0')).join('');
}

function today() { return new Date().toISOString().slice(0, 10); }
function dayOffset(n) { return new Date(Date.now() + n * 86400000).toISOString().slice(0, 10); }

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json; charset=utf-8' } });
}
