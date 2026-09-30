"""gpt-oss 가 번역에서 JSON 형식을 안 지키는 원인 확인 — 원시 응답(본문·추론·종료 이유)을 본다."""
import json
import urllib.request

from translator import outgoing
from translator.llm import URL

text = "그림자송곳니 성채 법사 구해요"
system = outgoing.system_prompt("en", "English", outgoing.terms_for(text, "en"))
for num_ctx in (2048, 8192):
    body = {"model": "gpt-oss:20b", "stream": False, "think": "low", "format": outgoing.SCHEMA, "keep_alive": "5m",
            "options": {"temperature": 0, "num_ctx": num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
    req = urllib.request.Request(URL + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))
    m = r["message"]
    print(f"num_ctx={num_ctx} done={r.get('done_reason')} prompt_tok={r.get('prompt_eval_count')} eval_tok={r.get('eval_count')}")
    print(f"   content : {m.get('content', '')[:200]!r}")
    print(f"   thinking: {(m.get('thinking') or '')[:200]!r}")
