import math

USABLE = 0.9
OVERHEAD_GB = 1.3
HEAD_NODES = 1
SLOW_ABOVE = 8


def per_node_gb(model, workers):
    return model["size_gb"] / workers + OVERHEAD_GB


def min_workers(model, mem_gib):
    room = mem_gib * USABLE - OVERHEAD_GB
    if room <= 0:
        return None
    n = max(1, math.ceil(model["size_gb"] / room))
    return n if n <= model["n_layers"] else None


def plan(model, spec, limit, replicas=None):
    n = min_workers(model, spec["mem_gib"])
    if n is None:
        return {"ok": False, "reason": f"{spec['type']} has too little memory for this model"}
    top = (limit - HEAD_NODES) // n
    out = {"workers": n, "per_node_gb": round(per_node_gb(model, n), 1), "recommended_limit": n + HEAD_NODES,
           "max_replicas": max(top, 0)}
    if top < 1:
        out.update(ok=False, reason=f"needs {n} workers plus the head, but the limit is {limit}")
        return out
    rep = min(replicas or top, top)
    out.update(ok=True, replicas=rep, instances=n * rep + HEAD_NODES)
    if n > SLOW_ABOVE:
        out["warning"] = f"{n} workers in a row makes every token slower, a bigger node type would be faster"
    return out


def check(model, spec, workers, replicas, limit):
    if workers < 1 or replicas < 1:
        return "workers and replica sets must be at least 1"
    if workers > model["n_layers"]:
        return f"this model has {model['n_layers']} layers, so at most {model['n_layers']} workers can share it"
    if workers * replicas + HEAD_NODES > limit:
        return f"{workers} workers x {replicas} sets + head is {workers * replicas + HEAD_NODES}, the limit is {limit}"
    need = per_node_gb(model, workers)
    if need > spec["mem_gib"] * USABLE:
        more = min_workers(model, spec["mem_gib"])
        hint = f", use at least {more} workers" if more else ""
        return f"{model['name']} needs about {need:.1f} GB per node but {spec['type']} has {spec['mem_gib']} GiB{hint}"
    return None
