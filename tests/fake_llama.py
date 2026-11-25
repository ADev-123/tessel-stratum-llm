import asyncio
import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

REPLY = ("Here is **a quick answer** with `inline code`.\n\n"
         "## Steps\n\n- first idea\n- second idea\n\n"
         "1. one\n2. two\n\n"
         "```python\ndef hello():\n    return 'hi'\n```\n\n"
         "A [link](https://example.com) and *emphasis*. <b>not html</b>\n")
CHUNKS = re.findall(r"\S+\s*|\n+", REPLY)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    n = min(body.get("max_tokens", 32), len(CHUNKS))

    async def gen():
        yield f"data: {json.dumps({'choices': [{'delta': {}}], 'prompt_progress': {'processed': 5, 'total': 10}})}\n\n"
        for i in range(n):
            await asyncio.sleep(0.06)
            yield f"data: {json.dumps({'choices': [{'delta': {'content': CHUNKS[i]}}]})}\n\n"
        yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 12, 'completion_tokens': n}, 'timings': {'prompt_ms': 120, 'prompt_per_second': 100.0}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
