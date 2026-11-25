import asyncio
import json
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import bench
from . import fit as fitting
from .cluster import Cluster
from .downloader import Downloads
from .manager import Manager, head_status

CONFIG_PATH = os.environ.get("TESSEL_CONFIG", "/opt/tessel/app/config.json")
STATIC = os.path.join(os.path.dirname(__file__), "static")

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

if "sets" not in cfg:
    cfg["sets"] = [{"id": "A", "port": cfg.get("llama_port", 8080), "log_file": cfg["log_file"], "workers": cfg["workers"]}]


state = {"bench": False, "lock": None}
status_cache = {"t": 0.0, "data": None}


def make_mgr(s):
    m = Manager(cfg, s["id"], s["port"], s["workers"], s["log_file"])
    m.lock = state["lock"]
    return m


mgrs = [make_mgr(s) for s in cfg["sets"]]
cluster = Cluster(cfg, mgrs, make_mgr) if cfg.get("region") else None
downloads = Downloads(cfg)
queue = deque()
stats = {"requests": 0, "tokens": 0, "errors": 0, "decode_time": 0.0, "since": time.time(), "reply_s": 20.0}


class Limiter:
    def __init__(self, per_min):
        self.per_min = per_min
        self.hits = {}

    def allow(self, key):
        now = time.time()
        if len(self.hits) > 5000:
            self.hits.clear()
        q = self.hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_min:
            return False
        q.append(now)
        return True


chat_limiter = Limiter(cfg.get("chat_rate_per_min", 12))
admin_limiter = Limiter(10)
PRIORITY = {"ready": 0, "loading": 1, "waiting": 2, "error": 3, "stopped": 4}


def ready_sets():
    return [m for m in mgrs if m.state == "ready"]


def pick():
    cap = cfg.get("per_set_concurrency", 1)
    free = [m for m in ready_sets() if m.active < cap]
    return min(free, key=lambda m: m.active) if free else None


def aggregate():
    best = sorted(mgrs, key=lambda m: PRIORITY.get(m.state, 9))[0]
    s = best.model_status()
    s["sets_ready"] = len(ready_sets())
    s["sets_total"] = len(mgrs)
    return s


async def autostart():
    for m in list(mgrs):
        while True:
            try:
                if m.state == "stopped":
                    await m.start(m.target())
                break
            except Exception:
                await asyncio.sleep(15)


async def monitor():
    misses = {}
    while True:
        await asyncio.sleep(10)
        if cluster is None or cluster.job["state"] == "running" or not cfg.get("auto_replace"):
            misses.clear()
            continue
        try:
            ids = [w["id"] for m in mgrs for w in m.workers if w.get("id")]
            states = await asyncio.to_thread(cluster.states, ids) if ids else {}
        except Exception:
            continue
        bad = None
        for m in mgrs:
            if m.state != "ready":
                continue
            for w in await m.workers_status():
                key = (m.id, w["name"])
                gone = states.get(w.get("id") or "", "running") not in ("pending", "running")
                misses[key] = 0 if w["up"] else misses.get(key, 0) + 1
                if gone:
                    misses[key] = 99
                if misses[key] >= 6:
                    bad = m.id
        if bad:
            misses.clear()
            try:
                await cluster.replace_dead(bad)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app):
    state["lock"] = asyncio.Lock()
    for m in mgrs:
        m.lock = state["lock"]
    for m in mgrs:
        await m.adopt()
    if cluster and mgrs[0].workers and mgrs[0].workers[0].get("type"):
        await cluster.spec(mgrs[0].workers[0]["type"])
    tasks = [asyncio.create_task(monitor())]
    if not cfg.get("assume_ready") and any(m.state == "stopped" for m in mgrs):
        tasks.append(asyncio.create_task(autostart()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def client_ip(request: Request):
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def require_admin(request: Request):
    if not admin_limiter.allow("admin:" + client_ip(request)):
        raise HTTPException(429, "too many admin attempts")
    token = request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(token.encode(), cfg["admin_token"].encode()):
        raise HTTPException(401, "admin token required")


class Msg(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(max_length=8000)


class ChatBody(BaseModel):
    messages: List[Msg] = Field(min_length=1, max_length=40)
    max_tokens: int = 256
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    thinking: bool = False


class LoadBody(BaseModel):
    id: str


class ApplyBody(BaseModel):
    type: str = Field(max_length=40, pattern=r"^[a-z0-9.\-]+$")
    count: int
    replicas: int = 1
    model: Optional[str] = None


class LimitBody(BaseModel):
    max_instances: int


class DownloadBody(BaseModel):
    id: str
    cancel: bool = False


class ReplaceBody(BaseModel):
    set: Optional[str] = None


class BenchBody(BaseModel):
    levels: List[int] = [1, 2, 4]
    duration_s: int = 30
    max_tokens: int = 48


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    if status_cache["data"] is not None and time.time() - status_cache["t"] < 1.5:
        return status_cache["data"]
    data = await build_status()
    status_cache["data"] = data
    status_cache["t"] = time.time()
    return data


async def build_status():
    ctx_slot = cfg["ctx_total"] // cfg["parallel"]
    ws = await asyncio.gather(*(m.workers_status() for m in mgrs))
    sets = [{"id": m.id, "state": m.state, "active": m.active, "model": m.model_status(), "workers": w} for m, w in zip(mgrs, ws)]
    agg = aggregate()
    catalog = mgrs[0].catalog()
    shape = cluster.current_shape() if cluster else (None, 0, len(mgrs))
    cur_spec = cluster.cached_spec(shape[0]) if cluster and shape[0] else None
    limit = cfg["max_instances"]
    for c in catalog:
        c["running"] = any(m.model_id == c["id"] and m.state in ("loading", "ready") for m in mgrs)
        c["download"] = downloads.info(c["id"])
        if cur_spec:
            c["plan"] = fitting.plan(c, cur_spec, limit)
            c["fits_now"] = fitting.check(c, cur_spec, shape[1], shape[2], limit) is None
    return {
        "model": agg,
        "models": catalog,
        "sets": sets,
        "head": {"name": "head", **head_status()},
        "limits": {"ctx_tokens": ctx_slot, "max_tokens_cap": cfg["max_tokens_cap"], "rate_per_min": cfg.get("chat_rate_per_min", 12),
                   "per_set": cfg.get("per_set_concurrency", 1), "queue": len(queue), "queue_max": cfg.get("max_queue", 20),
                   "queue_timeout_s": cfg.get("queue_timeout_s", 180), "slots": cfg["parallel"]},
        "bench_running": state["bench"],
        "cluster": ({"job": cluster.job, "auto_replace": bool(cfg.get("auto_replace")), "types": cfg["node_types"],
                     "type": shape[0], "count": shape[1], "replicas": shape[2], "max_instances": limit,
                     "max_instances_hard": cfg.get("max_instances_hard", limit), "archs": cfg.get("rpc_archs", ["arm64"]),
                     "rules": {"usable": fitting.USABLE, "overhead_gb": fitting.OVERHEAD_GB, "head": fitting.HEAD_NODES,
                               "slow_above": fitting.SLOW_ABOVE},
                     "target_model": cluster.target_model()["id"]}
                    if cluster else None),
        "stats": {"requests": stats["requests"], "tokens": stats["tokens"], "errors": stats["errors"],
                  "avg_tok_s": round(stats["tokens"] / stats["decode_time"], 2) if stats["decode_time"] else None,
                  "uptime_s": round(time.time() - stats["since"])},
    }


@app.post("/api/models/load")
async def load_model(body: LoadBody, request: Request):
    require_admin(request)
    if state["bench"]:
        raise HTTPException(409, "limit test running")
    try:
        for m in mgrs:
            m.precheck(body.id)
        if cluster:
            itype, count, reps = cluster.current_shape()
            spec = cluster.cached_spec(itype) if itype else None
            err = fitting.check(mgrs[0].models[body.id], spec, count, reps, cfg["max_instances"]) if spec else None
            if err:
                raise ValueError(err + ". Change the nodes below first.")
            cfg["target_model"] = body.id
        for m in mgrs:
            await m.start(body.id)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/models/download")
async def download_model(body: DownloadBody, request: Request):
    require_admin(request)
    m = mgrs[0].models.get(body.id)
    if not m:
        raise HTTPException(400, "unknown model")
    if body.cancel:
        downloads.cancel(body.id)
        return {"ok": True}
    if downloads.busy():
        raise HTTPException(409, "another download is running")
    try:
        await downloads.start(m)
    except (OSError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/cluster/type")
async def node_type(name: str, request: Request):
    require_admin(request)
    if not cluster:
        raise HTTPException(404, "cluster control is not enabled")
    if len(name) > 40 or not all(ch.isalnum() or ch in ".-" for ch in name):
        raise HTTPException(400, "not a valid instance type name")
    spec = await cluster.spec(name)
    if not spec:
        raise HTTPException(404, "instance type not found in this region")
    return {**spec, "supported": spec["arch"] in cfg.get("rpc_archs", ["arm64"])}


@app.post("/api/cluster/limit")
async def cluster_limit(body: LimitBody, request: Request):
    require_admin(request)
    if not cluster:
        raise HTTPException(404, "cluster control is not enabled")
    try:
        cluster.set_limit(body.max_instances)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    status_cache["data"] = None
    return {"ok": True}


@app.post("/api/models/stop")
async def stop_model(request: Request):
    require_admin(request)
    for m in mgrs:
        await m.stop()
    return {"ok": True}


@app.post("/api/cluster/apply")
async def cluster_apply(body: ApplyBody, request: Request):
    require_admin(request)
    if not cluster:
        raise HTTPException(404, "cluster control is not enabled")
    if state["bench"]:
        raise HTTPException(409, "limit test running")
    try:
        await cluster.apply(body.type, body.count, body.replicas, body.model)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/cluster/replace")
async def cluster_replace(body: ReplaceBody, request: Request):
    require_admin(request)
    if not cluster:
        raise HTTPException(404, "cluster control is not enabled")
    try:
        await cluster.replace_dead(body.set)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request):
    if state["bench"]:
        raise HTTPException(503, "limit test running, try again shortly")
    if not any(m.state in ("ready", "loading", "waiting") for m in mgrs):
        raise HTTPException(503, "no model is running")
    if not chat_limiter.allow(client_ip(request)):
        raise HTTPException(429, "rate limit reached, wait a moment")
    max_tokens = max(8, min(body.max_tokens, cfg["max_tokens_cap"]))
    ctx_slot = cfg["ctx_total"] // cfg["parallel"]
    est = sum(len(m.content) for m in body.messages) // 3 + 8 * len(body.messages)
    if est + max_tokens > ctx_slot:
        raise HTTPException(413, f"conversation too long: about {est} + {max_tokens} tokens exceeds the {ctx_slot} token context limit")
    if len(queue) >= cfg.get("max_queue", 20):
        raise HTTPException(429, "server busy, the queue is full")
    payload = {
        "stream": True, "max_tokens": max_tokens, "temperature": body.temperature,
        "messages": [m.model_dump() for m in body.messages],
        "stream_options": {"include_usage": True}, "return_progress": True,
        "chat_template_kwargs": {"enable_thinking": body.thinking},
    }
    return StreamingResponse(generate(request, payload, max_tokens, ctx_slot), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def estimate_wait(position):
    n = max(1, len(ready_sets()))
    return round(stats["reply_s"] * position / (n * cfg.get("per_set_concurrency", 1)))


async def generate(request, payload, max_tokens, ctx_slot):
    ticket = object()
    queue.append(ticket)
    queued_at = time.perf_counter()
    mgr = None
    try:
        last = 0.0
        while True:
            if await request.is_disconnected():
                return
            if queue[0] is ticket:
                mgr = pick()
                if mgr:
                    queue.popleft()
                    mgr.active += 1
                    break
            waited = time.perf_counter() - queued_at
            if waited > cfg.get("queue_timeout_s", 180):
                stats["errors"] += 1
                yield sse("error", {"message": "waited too long in the queue, please try again"})
                return
            if not any(m.state in ("ready", "loading", "waiting") for m in mgrs):
                yield sse("error", {"message": "no model is running"})
                return
            if waited - last >= 1 or last == 0.0:
                last = waited
                pos = list(queue).index(ticket) + 1
                yield sse("queue", {"position": pos, "size": len(queue), "sets_ready": len(ready_sets()),
                                    "sets_total": len(mgrs), "est_wait_s": estimate_wait(pos), "waited_s": round(waited)})
            await asyncio.sleep(0.25)
        waited = time.perf_counter() - queued_at
        yield sse("start", {"set": mgr.id, "waited_ms": round(waited * 1000)})
        t0 = time.perf_counter()
        first = None
        n = 0
        usage = {}
        timings = {}
        finish = None
        payload["model"] = mgr.model_id
        url = f"http://127.0.0.1:{mgr.port}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=5)) as client:
            async with client.stream("POST", url, json=payload) as r:
                if r.status_code != 200:
                    stats["errors"] += 1
                    body = (await r.aread()).decode("utf-8", "replace")[:300]
                    yield sse("error", {"message": f"model server returned {r.status_code}: {body}"})
                    return
                async for line in r.aiter_lines():
                    if await request.is_disconnected():
                        return
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    o = json.loads(d)
                    if o.get("usage"):
                        usage = o["usage"]
                    if o.get("timings"):
                        timings = o["timings"]
                    if o.get("prompt_progress"):
                        p = o["prompt_progress"]
                        yield sse("progress", {"processed": p.get("processed"), "total": p.get("total")})
                    for ch in o.get("choices", []):
                        dl = ch.get("delta", {})
                        if dl.get("reasoning_content"):
                            n += 1
                            first = first or time.perf_counter()
                            yield sse("token", {"text": dl["reasoning_content"], "kind": "think", "n": n})
                        if dl.get("content"):
                            n += 1
                            first = first or time.perf_counter()
                            yield sse("token", {"text": dl["content"], "kind": "text", "n": n})
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        end = time.perf_counter()
        completion = usage.get("completion_tokens", n)
        decode_s = end - (first or t0)
        stats["requests"] += 1
        stats["tokens"] += completion
        stats["decode_time"] += max(decode_s, 1e-6)
        stats["reply_s"] = 0.7 * stats["reply_s"] + 0.3 * (end - t0)
        yield sse("done", {
            "set": mgr.id, "ttft_ms": round(((first or end) - t0) * 1000), "total_ms": round((end - t0) * 1000),
            "queued_ms": round(waited * 1000),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": completion,
            "tok_per_s": round(completion / decode_s, 2) if decode_s > 0 else None,
            "prompt_ms": round(timings["prompt_ms"]) if timings.get("prompt_ms") is not None else None,
            "prompt_tok_per_s": round(timings["prompt_per_second"], 2) if timings.get("prompt_per_second") else None,
            "finish_reason": finish, "max_tokens": max_tokens, "ctx_limit": ctx_slot,
        })
    except httpx.HTTPError as e:
        stats["errors"] += 1
        yield sse("error", {"message": f"model server unreachable: {type(e).__name__}"})
    finally:
        if ticket in queue:
            queue.remove(ticket)
        if mgr:
            mgr.active -= 1


@app.post("/api/bench")
async def run_bench(body: BenchBody, request: Request):
    require_admin(request)
    ready = ready_sets()
    if not ready:
        raise HTTPException(503, "no replica set is ready")
    if state["bench"]:
        raise HTTPException(409, "limit test already running")
    levels = [max(1, min(c, 2 * cfg["parallel"] * len(ready))) for c in body.levels][:6]
    duration = max(10, min(body.duration_s, 120))
    max_tokens = max(8, min(body.max_tokens, 256))
    urls = [f"http://127.0.0.1:{m.port}/v1/chat/completions" for m in ready]

    async def gen():
        state["bench"] = True
        try:
            async for ev in bench.run(cfg, urls, levels, duration, max_tokens):
                if await request.is_disconnected():
                    return
                yield sse(ev["event"], ev)
        finally:
            state["bench"] = False

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
