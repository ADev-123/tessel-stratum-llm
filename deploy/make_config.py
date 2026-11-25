import json
import os
import secrets
import sys

import envfile

envfile.load()

STATE = os.path.expanduser(os.environ.get("TESSEL_STATE_DIR", "~/.tessel"))
CLUSTER = os.path.join(STATE, "cluster.json")
TOKEN = os.path.join(STATE, "admin_token")

cluster = json.load(open(CLUSTER))
token = os.environ.get("TESSEL_ADMIN_TOKEN", "")
if not token:
    if not os.path.exists(TOKEN):
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(18))
    token = open(TOKEN).read().strip()

workers = [{"name": n.replace("tessel-", ""), "id": v["id"], "ip": v["private_ip"], "port": 50052, "type": "r7g.medium"}
           for n, v in sorted(cluster["nodes"].items()) if n.startswith("tessel-worker")]

node_types = [
    {"type": "r7g.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 8, "ami_ok": True, "note": "Graviton3, 8 GiB"},
    {"type": "r7gd.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 8, "ami_ok": True, "note": "Graviton3, 8 GiB, local NVMe"},
    {"type": "m7g.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 4, "ami_ok": True, "note": "Graviton3, 4 GiB"},
    {"type": "r6g.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 8, "ami_ok": False, "note": "Graviton2 needs its own build"},
    {"type": "t4g.medium", "arch": "arm64", "vcpu": 2, "mem_gib": 4, "ami_ok": False, "note": "2 vCPU Graviton2 needs its own build"},
    {"type": "r7a.medium", "arch": "x86_64", "vcpu": 1, "mem_gib": 8, "ami_ok": False, "note": "x86 needs its own build"},
    {"type": "t3.medium", "arch": "x86_64", "vcpu": 2, "mem_gib": 4, "ami_ok": False, "note": "x86 needs its own build"},
]

cfg = {
    "admin_token": token,
    "llama_bin": "/opt/tessel/llama.cpp/build/bin/llama-server",
    "llama_port": 8080,
    "models_dir": "/opt/tessel/models",
    "log_file": "/opt/tessel/llama-server.log",
    "workers": workers,
    "sets": [{"id": "A", "port": 8080, "log_file": "/opt/tessel/llama-server.log", "workers": workers}],
    "per_set_concurrency": 1,
    "queue_timeout_s": 180,
    "region": os.environ.get("TESSEL_REGION", "us-east-1"),
    "workers_file": "/opt/tessel/app/workers.json",
    "node_types": node_types,
    "max_instances": 9,
    "key_name": os.environ.get("TESSEL_KEY_NAME", "vockey"),
    "subnet": cluster["subnet"],
    "nodes_sg": cluster["nodes_sg"],
    "instance_profile": os.environ.get("TESSEL_INSTANCE_PROFILE", "LabInstanceProfile"),
    "base_ami": cluster.get("base_ami"),
    "artifact_bucket": cluster.get("artifact_bucket"),
    "models": [{
        "id": "qwen3-30b-a3b", "name": "Qwen3-30B-A3B", "quant": "Q4_K_M",
        "file": "Qwen3-30B-A3B-Q4_K_M.gguf", "size_gb": 18.56, "license": "Apache-2.0", "n_layers": 48,
    }],
    "auto_replace": True,
    "default_model": "qwen3-30b-a3b",
    "ctx_total": 8192,
    "parallel": 4,
    "threads": 1,
    "max_tokens_cap": 512,
    "chat_rate_per_min": 12,
    "max_concurrent_chats": 2,
    "max_queue": 20,
}
json.dump(cfg, sys.stdout, indent=1)
