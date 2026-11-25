import asyncio
import os
import subprocess
import time

import httpx
import psutil


async def tcp_ok(host, port, timeout=1.5):
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True, (time.perf_counter() - t0) * 1000
    except Exception:
        return False, None


def tail(path, n=40):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def llama_processes(port):
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        if p.info["name"] != "llama-server":
            continue
        cmd = p.info["cmdline"] or []
        if "--port" in cmd and cmd[cmd.index("--port") + 1] == str(port):
            out.append(p)
    return out


def connected_ips(port=50052):
    out = set()
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_ESTABLISHED and c.raddr and c.raddr.port == port:
                out.add(c.raddr.ip)
    except (psutil.Error, OSError):
        pass
    return out


def head_status():
    vm = psutil.virtual_memory()
    return {"cpu_percent": psutil.cpu_percent(interval=None), "mem_used_gb": round(vm.used / 1e9, 2),
            "mem_total_gb": round(vm.total / 1e9, 2), "load1": round(os.getloadavg()[0], 2)}


class Manager:
    def __init__(self, cfg, sid, port, workers, log_file):
        self.cfg = cfg
        self.id = sid
        self.port = port
        self.workers = workers
        self.log_file = log_file
        self.models = {m["id"]: m for m in cfg["models"]}
        self.lock = None
        self.model_id = None
        self.state = "stopped"
        self.started_at = None
        self.ready_at = None
        self.error = None
        self.bytes0 = None
        self.active = 0
        self.bench_running = False
        self._task = None
        if cfg.get("assume_ready"):
            self.state = "ready"
            self.model_id = cfg["default_model"]
            self.started_at = self.ready_at = time.time()

    def model_path(self, m):
        return os.path.join(self.cfg["models_dir"], m["file"])

    def catalog(self):
        out = []
        for m in self.cfg["models"]:
            path = self.model_path(m)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            out.append({**m, "downloaded_gb": round(size / 1e9, 2),
                        "downloaded": size >= m["size_gb"] * 1e9 * 0.995})
        return out

    def command(self, m):
        c = self.cfg
        rpc = ",".join(f"{w['ip']}:{w['port']}" for w in self.workers)
        return [c["llama_bin"], "-m", self.model_path(m), "--host", "127.0.0.1", "--port", str(self.port),
                "--rpc", rpc, "-ngl", "99", "-c", str(c["ctx_total"]), "-np", str(c["parallel"]),
                "-t", str(c.get("threads", 1)), "--jinja", "--no-webui", "--metrics", "-a", m["id"]]

    async def workers_status(self):
        res = []
        n = len(self.workers)
        layers = self.models[self.cfg["default_model"]].get("n_layers")
        connected = connected_ips()

        async def probe(w):
            if w["ip"] in connected:
                return True, None
            return await tcp_ok(w["ip"], w["port"], 0.8)

        probes = await asyncio.gather(*(probe(w) for w in self.workers))
        for i, (w, (ok, rtt)) in enumerate(zip(self.workers, probes)):
            span = f"{i * layers // n}-{(i + 1) * layers // n - 1}" if layers else None
            res.append({"name": w["name"], "id": w.get("id"), "ip": w["ip"], "type": w.get("type"), "up": ok,
                        "rtt_ms": round(rtt, 1) if rtt else None, "layers": span})
        return res

    def precheck(self, model_id):
        if model_id not in self.models:
            raise ValueError("unknown model")
        info = next(c for c in self.catalog() if c["id"] == model_id)
        if not info["downloaded"]:
            raise FileNotFoundError("model file is not fully downloaded yet")

    async def start(self, model_id):
        self.precheck(model_id)
        await self.stop()
        self.model_id = model_id
        self.state = "waiting"
        self.error = None
        self.started_at = time.time()
        self.ready_at = None
        self._task = asyncio.create_task(self._run(model_id))

    async def _run(self, model_id):
        try:
            async with self.lock:
                down = [w["name"] for w in await self.workers_status() if not w["up"]]
                if down:
                    raise RuntimeError("workers unreachable: " + ", ".join(down))
                m = self.models[model_id]
                log = open(self.log_file, "ab")
                subprocess.Popen(self.command(m), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.state = "loading"
                self.started_at = time.time()
                self.bytes0 = psutil.net_io_counters().bytes_sent
                await self._watch()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None
        procs = llama_processes(self.port)
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        if procs:
            await asyncio.get_running_loop().run_in_executor(None, lambda: psutil.wait_procs(procs, timeout=15))
            for p in procs:
                if p.is_running():
                    p.kill()
        self.state = "stopped"
        self.model_id = None
        self.ready_at = None

    async def adopt(self):
        ps = llama_processes(self.port)
        if not ps:
            return
        cmd = ps[0].info["cmdline"] or []
        if "-m" in cmd:
            fn = os.path.basename(cmd[cmd.index("-m") + 1])
            self.model_id = next((m["id"] for m in self.cfg["models"] if m["file"] == fn), None)
        self.state = "loading"
        self.started_at = ps[0].create_time()
        self._task = asyncio.create_task(self._watch())

    async def _watch(self):
        url = f"http://127.0.0.1:{self.port}/health"
        deadline = time.time() + 3600
        async with httpx.AsyncClient(timeout=3) as client:
            while time.time() < deadline:
                if not llama_processes(self.port):
                    self.state = "error"
                    self.error = "llama-server exited: " + " | ".join(tail(self.log_file, 4))
                    return
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        self.state = "ready"
                        self.ready_at = time.time()
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
        self.state = "error"
        self.error = "load timed out"

    def model_status(self):
        prog = None
        if self.state == "loading" and self.model_id and self.bytes0 is not None:
            size = self.models[self.model_id]["size_gb"] * 1e9
            prog = min(0.99, max(0.0, (psutil.net_io_counters().bytes_sent - self.bytes0) / size))
        if self.state == "ready":
            prog = 1.0
        end = self.ready_at or time.time()
        return {
            "set": self.id,
            "id": self.model_id,
            "name": self.models[self.model_id]["name"] if self.model_id in self.models else None,
            "state": self.state,
            "progress": prog,
            "elapsed_s": round(end - self.started_at, 1) if self.started_at and self.state != "stopped" else None,
            "load_s": round(self.ready_at - self.started_at, 1) if self.ready_at and self.started_at else None,
            "error": self.error,
            "active": self.active,
            "log": tail(self.log_file, 3) if self.state == "loading" else [],
        }
