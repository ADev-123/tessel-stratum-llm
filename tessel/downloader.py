import asyncio
import os
import shutil

import httpx

URL = "https://huggingface.co/{repo}/resolve/main/{path}"


def model_dir(cfg, m):
    return os.path.join(cfg["models_dir"], m["id"])


def file_paths(cfg, m):
    d = model_dir(cfg, m)
    out = []
    for f in m["files"]:
        name = os.path.basename(f["path"])
        new = os.path.join(d, name)
        old = os.path.join(cfg["models_dir"], name)
        out.append((f, new if os.path.exists(new) or not os.path.exists(old) else old))
    return out


def on_disk(cfg, m):
    total = 0
    complete = True
    for f, p in file_paths(cfg, m):
        size = os.path.getsize(p) if os.path.exists(p) else 0
        part = os.path.getsize(p + ".part") if os.path.exists(p + ".part") else 0
        total += size or part
        if size < f["bytes"] * 0.995:
            complete = False
    return total, complete


class Downloads:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {}
        self.tasks = {}

    def info(self, mid):
        return self.state.get(mid)

    def busy(self):
        return any(not t.done() for t in self.tasks.values())

    async def start(self, m):
        mid = m["id"]
        if on_disk(self.cfg, m)[1]:
            self.state[mid] = {"state": "done", "error": None}
            return None
        if mid in self.tasks and not self.tasks[mid].done():
            return self.tasks[mid]
        os.makedirs(model_dir(self.cfg, m), exist_ok=True)
        need = sum(f["bytes"] for f in m["files"]) - on_disk(self.cfg, m)[0]
        free = shutil.disk_usage(model_dir(self.cfg, m)).free
        if need + 1e9 > free:
            raise RuntimeError(f"not enough disk space on the head node: need {need / 1e9:.1f} GB, {free / 1e9:.1f} GB free")
        self.state[mid] = {"state": "downloading", "error": None}
        self.tasks[mid] = asyncio.create_task(self._run(m))
        return self.tasks[mid]

    async def _run(self, m):
        mid = m["id"]
        headers = {}
        token = os.environ.get("HF_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60, connect=15), headers=headers) as c:
                for f, dest in file_paths(self.cfg, m):
                    if os.path.exists(dest) and os.path.getsize(dest) >= f["bytes"] * 0.995:
                        continue
                    part = dest + ".part"
                    have = os.path.getsize(part) if os.path.exists(part) else 0
                    h = {"Range": f"bytes={have}-"} if have else {}
                    async with c.stream("GET", URL.format(repo=m["repo"], path=f["path"]), headers=h) as r:
                        if r.status_code not in (200, 206):
                            raise RuntimeError(f"download failed with HTTP {r.status_code}")
                        mode = "ab" if r.status_code == 206 else "wb"
                        with open(part, mode) as out:
                            async for chunk in r.aiter_bytes(1 << 20):
                                out.write(chunk)
                    if os.path.getsize(part) < f["bytes"] * 0.995:
                        raise RuntimeError("download ended early, start it again to resume")
                    os.replace(part, dest)
            self.state[mid] = {"state": "done", "error": None}
        except asyncio.CancelledError:
            self.state[mid] = {"state": "stopped", "error": None}
            raise
        except Exception as e:
            self.state[mid] = {"state": "error", "error": f"{type(e).__name__}: {e}"}

    def cancel(self, mid):
        t = self.tasks.get(mid)
        if t and not t.done():
            t.cancel()
