# WATT 받는 서버

Cloudflare Worker + D1 + R2. 사전 후보(#7)와 인식 오류 신고(#14)를 받는다. 주소: `https://watt-api.watt-api.workers.dev`

| 주소 | 하는 일 | 제한 |
|---|---|---|
| `POST /v1/terms` | 사전 후보(JSON) | 설치마다 하루 1번, 30개, 16KB |
| `POST /v1/report` | 신고(`meta` JSON + `image` PNG·JPEG) | 1MB, 설치마다 24시간에 1번, 같은 화면 거절 |
| `GET /v1/health` | | |
| `/v1/admin/stats` · `reports` · `reports/<id>/image` · `terms` | 관리 | `Authorization: Bearer <관리자 키>` |

- IP별 하루 제한과 하루 전체 상한(신고 500, 사전 20,000)이 있다. 넘으면 받지 않는다 — 무료 한도를 넘지 않게
- 설치 ID · IP 는 SALT 해시로만 남긴다. 이름 · 문장은 받지 않고, 신고 `meta` 는 정해진 칸만 남긴다
- 신고 이미지는 30일 뒤 지운다(R2 수명 규칙 + 매일 정리)
- 요청에 `User-Agent: WATT/<버전>` 이 있어야 한다(Cloudflare 가 Python 기본 값을 막는다, error 1010)

## 배포
```
npm install
npx wrangler login
npx wrangler d1 execute watt --remote --file schema.sql
npx wrangler deploy
npx wrangler secret put ADMIN_TOKEN   # 관리자 키
npx wrangler secret put SALT
```

## 시험
```
npx wrangler d1 execute watt --local --file schema.sql
npx wrangler dev --local                  # .dev.vars 에 ADMIN_TOKEN · SALT
python test_api.py                        # 또는: python test_api.py https://watt-api.watt-api.workers.dev (WATT_ADMIN=관리자 키)
```
