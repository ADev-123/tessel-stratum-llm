import json
import os
import secrets
import sys

import envfile

envfile.load()

STATE = os.path.expanduser(os.environ.get("TESSEL_STATE_DIR", "~/.tessel"))
CLUSTER = os.path.join(STATE, "cluster.json")
TOKEN = os.path.join(STATE, "admin_token")
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tessel", "models.json")

cluster = json.load(open(CLUSTER))
token = os.environ.get("TESSEL_ADMIN_TOKEN", "")
if not token:
    if not os.path.exists(TOKEN):
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(18))
    token = open(TOKEN).read().strip()

first_type = os.environ.get("TESSEL_NODE_TYPE", "r7g.medium")
workers = [{"name": n.replace("tessel-", ""), "id": v["id"], "ip": v["private_ip"], "port": 50052, "type": first_type}
           for n, v in sorted(cluster["nodes"].items()) if n.startswith("tessel-worker")]

# Suggestions shown in the node type box. Any other instance type can be typed in,
# its size is looked up from EC2 when it is checked.
node_types = [
    {"type": "m7g.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 4},
    {"type": "r7g.medium", "arch": "arm64", "vcpu": 1, "mem_gib": 8},
    {"type": "r7g.large", "arch": "arm64", "vcpu": 2, "mem_gib": 16},
    {"type": "r7g.xlarge", "arch": "arm64", "vcpu": 4, "mem_gib": 32},
    {"type": "r7g.2xlarge", "arch": "arm64", "vcpu": 8, "mem_gib": 64},
    {"type": "r7g.4xlarge", "arch": "arm64", "vcpu": 16, "mem_gib": 128},
    {"type": "r7g.8xlarge", "arch": "arm64", "vcpu": 32, "mem_gib": 256},
]

models = json.load(open(CATALOG))
default_model = os.environ.get("TESSEL_DEFAULT_MODEL", "qwen3-30b-a3b")

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
    "rpc_archs": os.environ.get("TESSEL_RPC_ARCHS", "arm64").split(","),
    "max_instances": int(os.environ.get("TESSEL_MAX_INSTANCES", "9")),
    "max_instances_hard": int(os.environ.get("TESSEL_HARD_MAX_INSTANCES", "64")),
    "key_name": os.environ.get("TESSEL_KEY_NAME", "vockey"),
    "subnet": cluster["subnet"],
    "nodes_sg": cluster["nodes_sg"],
    "instance_profile": os.environ.get("TESSEL_INSTANCE_PROFILE", "LabInstanceProfile"),
    "base_ami": cluster.get("base_ami"),
    "artifact_bucket": cluster.get("artifact_bucket"),
    "models": models,
    "auto_replace": True,
    "default_model": default_model,
    "ctx_total": 8192,
    "parallel": 4,
    "threads": 1,
    "max_tokens_cap": 512,
    "chat_rate_per_min": 12,
    "max_concurrent_chats": 2,
    "max_queue": 20,
}
json.dump(cfg, sys.stdout, indent=1)
