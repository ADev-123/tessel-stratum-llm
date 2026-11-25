import asyncio
import json
import statistics
import time

import httpx

PROMPT = "Explain in two sentences what a load balancer does."


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


async def one(client, url, payload, results):
    t0 = time.perf_counter()
    first = None
    n = 0
    try:
        async with client.stream("POST", url, json=payload) as r:
            if r.status_code != 200:
                results.append({"err": f"http {r.status_code}"})
                return
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                o = json.loads(d)
                for ch in o.get("choices", []):
                    dl = ch.get("delta", {})
                    if dl.get("content") or dl.get("reasoning_content"):
                        n += 1
                        if first is None:
                            first = time.perf_counter()
        end = time.perf_counter()
        results.append({"lat": end - t0, "ttft": (first or end) - t0, "tokens": n,
                        "decode": n / max(end - (first or t0), 1e-6)})
    except Exception as e:
        results.append({"err": type(e).__name__})


async def step(urls, conc, duration, payload):
    results = []
    t0 = time.perf_counter()
    deadline = t0 + duration
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as client:
        async def loop(url):
            while time.perf_counter() < deadline:
                await one(client, url, payload, results)

        await asyncio.gather(*[loop(urls[i % len(urls)]) for i in range(conc)])
    wall = time.perf_counter() - t0
    ok = [r for r in results if "err" not in r]
    errs = [r["err"] for r in results if "err" in r]
    tokens = sum(r["tokens"] for r in ok)
    return {
        "concurrency": conc,
        "requests": len(ok),
        "errors": len(errs),
        "wall_s": round(wall, 1),
        "tokens": tokens,
        "agg_tok_s": round(tokens / wall, 2) if wall else 0,
        "per_req_tok_s": round(statistics.mean(r["decode"] for r in ok), 2) if ok else None,
        "lat_p50_s": round(pct([r["lat"] for r in ok], 0.5), 1) if ok else None,
        "lat_p95_s": round(pct([r["lat"] for r in ok], 0.95), 1) if ok else None,
        "ttft_p50_s": round(pct([r["ttft"] for r in ok], 0.5), 2) if ok else None,
    }


async def run(cfg, urls, levels, duration, max_tokens):
    payload = {
        "model": cfg["default_model"], "stream": True, "max_tokens": max_tokens, "temperature": 0.7,
        "messages": [{"role": "user", "content": PROMPT}],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    yield {"event": "start", "levels": levels, "duration_s": duration, "max_tokens": max_tokens, "sets": len(urls)}
    steps = []
    for c in levels:
        yield {"event": "running", "concurrency": c}
        s = await step(urls, c, duration, payload)
        steps.append(s)
        yield {"event": "step", **s}
        if s["errors"] and s["errors"] >= max(2, s["requests"]):
            break
    best = max(steps, key=lambda s: s["agg_tok_s"])
    knee = None
    for prev, cur in zip(steps, steps[1:]):
        if cur["errors"] or cur["agg_tok_s"] < prev["agg_tok_s"] * 1.1:
            knee = prev["concurrency"]
            break
    yield {"event": "done", "best_concurrency": best["concurrency"], "best_agg_tok_s": best["agg_tok_s"],
           "saturation_concurrency": knee or steps[-1]["concurrency"], "steps": steps}
