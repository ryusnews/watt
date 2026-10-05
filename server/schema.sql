-- WATT 받는 서버(D1). 사람 이름 · 문장 · IP 원문은 저장하지 않는다(IP 는 SALT 해시로 횟수 세기에만).

-- #7 사전 후보: 말 · 언어마다 합계 한 줄
CREATE TABLE IF NOT EXISTS terms (
  term TEXT NOT NULL,
  lang TEXT NOT NULL,
  installs INTEGER NOT NULL DEFAULT 0,  -- 본 설치 수
  hits INTEGER NOT NULL DEFAULT 0,      -- 본 횟수 합
  ctx TEXT NOT NULL DEFAULT '[]',       -- 앞뒤 단어 묶음 최대 5개(JSON)
  status TEXT NOT NULL DEFAULT 'new',   -- new · added · rejected
  first_day TEXT NOT NULL,
  last_day TEXT NOT NULL,
  PRIMARY KEY (term, lang)
);

-- 같은 설치가 같은 말을 두 번 세지 않게(90일 뒤 지움)
CREATE TABLE IF NOT EXISTS term_installs (
  term TEXT NOT NULL,
  lang TEXT NOT NULL,
  install TEXT NOT NULL,                -- 설치 ID 의 SALT 해시
  day TEXT NOT NULL,
  PRIMARY KEY (term, lang, install)
);

-- #14 인식 오류 신고 — 이미지는 R2(30일 뒤 지움)
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,                  -- 이미지 SHA-256
  at INTEGER NOT NULL,                  -- 받은 시각(ms)
  day TEXT NOT NULL,
  install TEXT NOT NULL,                -- 설치 ID 의 SALT 해시
  ver TEXT NOT NULL,
  meta TEXT NOT NULL,                   -- 찾은 영역 · 지정한 영역 · 창 크기 · 배율 …(JSON)
  r2key TEXT NOT NULL,
  size INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'new'    -- new · seen · fixed · invalid
);
CREATE INDEX IF NOT EXISTS reports_install ON reports (install, at);
CREATE INDEX IF NOT EXISTS reports_status ON reports (status, at);

-- #135 진단 기록(zip) — R2, 14일 뒤 지움
CREATE TABLE IF NOT EXISTS diags (
  id TEXT PRIMARY KEY,                  -- zip SHA-256 앞 12자(사용자에게 보이는 번호)
  at INTEGER NOT NULL,
  day TEXT NOT NULL,
  install TEXT NOT NULL,                -- 설치 ID 의 SALT 해시
  ver TEXT NOT NULL,
  r2key TEXT NOT NULL,
  size INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'new'    -- new · seen
);
CREATE INDEX IF NOT EXISTS diags_day ON diags (day);

-- 하루 횟수(설치 · IP 해시 · 전체). 이틀 뒤 지움
CREATE TABLE IF NOT EXISTS quota (
  day TEXT NOT NULL,
  key TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key)
);

-- #7 번역 품질 개선 참여 — 동의한 설치의 번역 표본(이름 · 채널 · 연락처 없이). 90일 뒤 지움
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  day TEXT NOT NULL,
  install TEXT NOT NULL,                -- 설치 ID 의 SALT 해시
  ver TEXT NOT NULL,
  lang TEXT NOT NULL,                   -- 통역 창이 고른 언어(en · zh · ru …)
  src TEXT NOT NULL DEFAULT '',         -- 모델이 본 원문 언어 표시(es · de …)
  kind TEXT NOT NULL DEFAULT '',        -- chat · party · trade · guild · ad
  model TEXT NOT NULL DEFAULT '',
  sec REAL NOT NULL DEFAULT 0,
  body TEXT NOT NULL,
  ko TEXT NOT NULL,
  reads TEXT NOT NULL DEFAULT '{}',     -- 엔진마다 읽은 글(JSON)
  score INTEGER NOT NULL DEFAULT 0,     -- 먼저 볼 것(엔진끼리 다름 · 원문 낱말 남음)
  status TEXT NOT NULL DEFAULT 'new',   -- new · ok · bad · doubt
  verdict TEXT NOT NULL DEFAULT ''      -- 검증 까닭
);
CREATE INDEX IF NOT EXISTS samples_status ON samples (status, id);
