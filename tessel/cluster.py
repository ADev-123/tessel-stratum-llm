import asyncio
import json
import os
import time

import boto3

from . import fit as fitting
from .manager import tcp_ok
from .userdata import worker_userdata

TAG = [{"Key": "tessel:managed", "Value": "true"}]
SET_IDS = "ABCDEFGH"


class Cluster:
    def __init__(self, cfg, mgrs, make_mgr):
        self.cfg = cfg
        self.mgrs = mgrs
        self.make_mgr = make_mgr
        self.ec2 = boto3.client("ec2", region_name=cfg["region"])
        self.job = {"state": "idle", "steps": [], "error": None, "started": time.time(), "kind": None}
        self.state_file = cfg["workers_file"]
        self.limit_file = self.state_file + ".limit"
        self.specs = {}
        if os.path.exists(self.limit_file):
            try:
                cfg["max_instances"] = json.load(open(self.limit_file))["max_instances"]
            except (OSError, ValueError, KeyError):
                pass
        self.load_state()

    def load_state(self):
        if not os.path.exists(self.state_file):
            return
        data = json.load(open(self.state_file))
        if data and "ip" in data[0]:
            data = [{"id": "A", "port": 8080, "log_file": self.cfg["sets"][0]["log_file"], "workers": data}]
        have = {m.id: m for m in self.mgrs}
        for s in data:
            if s["id"] in have:
                have[s["id"]].workers[:] = s["workers"]
            else:
                self.mgrs.append(self.make_mgr(s))

    def save_state(self):
        json.dump([{"id": m.id, "port": m.port, "log_file": m.log_file, "workers": m.workers} for m in self.mgrs],
                  open(self.state_file, "w"))

    def current_shape(self):
        first = self.mgrs[0].workers
        return (first[0].get("type") if first else None), len(first), len(self.mgrs)

    def target_model(self, model_id=None):
        return self.mgrs[0].models[model_id or self.cfg.get("target_model") or self.cfg["default_model"]]

    def cached_spec(self, itype):
        if itype in self.specs:
            return self.specs[itype]
        return next((t for t in self.cfg["node_types"] if t["type"] == itype), None)

    async def spec(self, itype):
        found = self.cached_spec(itype)
        if found:
            return found
        try:
            r = await asyncio.to_thread(self.ec2.describe_instance_types, InstanceTypes=[itype])
            i = r["InstanceTypes"][0]
        except Exception:
            return None
        archs = i["ProcessorInfo"]["SupportedArchitectures"]
        arch = "arm64" if "arm64" in archs else "x86_64"
        self.specs[itype] = {"type": itype, "arch": arch, "vcpu": i["VCpuInfo"]["DefaultVCpus"],
                             "mem_gib": round(i["MemoryInfo"]["SizeInMiB"] / 1024, 1)}
        return self.specs[itype]

    def set_limit(self, n):
        hard = self.cfg.get("max_instances_hard", self.cfg["max_instances"])
        used = 1 + sum(len(m.workers) for m in self.mgrs)
        if n < 2 or n > hard:
            raise ValueError(f"the limit must be between 2 and {hard}")
        if n < used:
            raise ValueError(f"{used} instances are running now, remove some before lowering the limit to {n}")
        self.cfg["max_instances"] = n
        json.dump({"max_instances": n}, open(self.limit_file, "w"))

    async def fit(self, itype, count, replicas, model_id=None):
        spec = await self.spec(itype)
        if not spec:
            return "instance type not found in this region"
        if spec["arch"] not in self.cfg.get("rpc_archs", ["arm64"]):
            return f"no {spec['arch']} worker build is published, only {', '.join(self.cfg.get('rpc_archs', ['arm64']))} is available"
        if replicas < 1 or replicas > len(SET_IDS):
            return "replicas must be between 1 and %d" % len(SET_IDS)
        return fitting.check(self.target_model(model_id), spec, count, replicas, self.cfg["max_instances"])

    def states(self, ids):
        r = self.ec2.describe_instances(InstanceIds=ids)
        return {i["InstanceId"]: i["State"]["Name"] for x in r["Reservations"] for i in x["Instances"]}

    def log(self, msg):
        self.job["steps"].append(f"{time.strftime('%H:%M:%S')} {msg}")

    def begin(self, kind):
        if self.job["state"] == "running":
            raise RuntimeError("a cluster change is already running")
        self.job = {"state": "running", "steps": [], "error": None, "started": time.time(), "kind": kind}

    def finish(self):
        self.job["state"] = "done"
        self.job["elapsed_s"] = round(time.time() - self.job["started"])
        self.log(f"done in {self.job['elapsed_s']} s")

    def fail(self, e):
        self.job["state"] = "error"
        self.job["error"] = f"{type(e).__name__}: {e}"
        self.log("failed: " + self.job["error"])

    def _launch(self, itype, n):
        gen = int(time.time()) % 100000
        out = []
        for k in range(n):
            r = self.ec2.run_instances(
                ImageId=self.cfg["base_ami"], InstanceType=itype, MinCount=1, MaxCount=1, KeyName=self.cfg["key_name"],
                UserData=worker_userdata(self.cfg["artifact_bucket"]),
                SubnetId=self.cfg["subnet"], SecurityGroupIds=[self.cfg["nodes_sg"]],
                IamInstanceProfile={"Name": self.cfg["instance_profile"]},
                BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 30, "VolumeType": "gp2", "DeleteOnTermination": True}}],
                MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
                TagSpecifications=[{"ResourceType": "instance", "Tags": TAG + [
                    {"Key": "Name", "Value": f"tessel-worker-{gen}-{k + 1}"}, {"Key": "tessel:role", "Value": "worker"}]}])
            out.append(r["Instances"][0]["InstanceId"])
        return out

    async def _terminate(self, ids):
        if not ids:
            return
        io = asyncio.to_thread
        await io(self.ec2.terminate_instances, InstanceIds=ids)
        await io(self.ec2.get_waiter("instance_terminated").wait, InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 60})

    async def _provision(self, itype, n):
        io = asyncio.to_thread
        self.log(f"launching {n} x {itype}")
        ids = await io(self._launch, itype, n)
        await io(self.ec2.get_waiter("instance_running").wait, InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
        desc = await io(self.ec2.describe_instances, InstanceIds=ids)
        nodes = sorted(({"id": i["InstanceId"], "ip": i["PrivateIpAddress"], "port": 50052, "type": itype}
                        for r in desc["Reservations"] for i in r["Instances"]), key=lambda x: x["id"])
        self.log("waiting for workers to answer")
        deadline = time.time() + 300
        while time.time() < deadline:
            if all([(await tcp_ok(w["ip"], w["port"]))[0] for w in nodes]):
                return nodes
            await asyncio.sleep(5)
        raise RuntimeError("workers did not come up in time")

    def _assign(self, mgr, nodes):
        for n, w in enumerate(nodes, 1):
            w["name"] = f"worker-{n}"
        mgr.workers[:] = nodes

    def _new_mgr(self, idx):
        sid = SET_IDS[idx]
        log = self.cfg["sets"][0]["log_file"] if idx == 0 else f"/opt/tessel/llama-server-{sid}.log"
        return self.make_mgr({"id": sid, "port": 8080 + idx, "log_file": log, "workers": []})

    async def apply(self, itype, count, replicas, model_id=None):
        if model_id and model_id not in self.mgrs[0].models:
            raise ValueError("unknown model")
        err = await self.fit(itype, count, replicas, model_id)
        if err:
            raise ValueError(err)
        if model_id:
            self.cfg["target_model"] = model_id
        self.begin("apply")
        asyncio.create_task(self._apply(itype, count, replicas))

    async def _apply(self, itype, count, replicas):
        try:
            cur_type, cur_count, cur_rep = self.current_shape()
            if (itype, count) == (cur_type, cur_count):
                await self._resize(replicas)
            else:
                await self._rebuild(itype, count, replicas)
            want = self.target_model()["id"]
            for m in self.mgrs:
                if m.model_id != want and m.state not in ("waiting", "loading"):
                    self.log(f"set {m.id}: loading {want}")
                    await m.start(want)
            self.finish()
        except Exception as e:
            self.fail(e)

    async def _managed_workers(self):
        r = await asyncio.to_thread(self.ec2.describe_instances, Filters=[
            {"Name": "tag:tessel:managed", "Values": ["true"]}, {"Name": "tag:tessel:role", "Values": ["worker"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        return [i["InstanceId"] for x in r["Reservations"] for i in x["Instances"]]

    async def _rebuild(self, itype, count, replicas):
        self.log("stopping all sets")
        for m in self.mgrs:
            await m.stop()
        old = await self._managed_workers()
        if old:
            self.log(f"terminating {len(old)} old workers")
            await self._terminate(old)
        nodes = await self._provision(itype, count * replicas)
        del self.mgrs[replicas:]
        while len(self.mgrs) < replicas:
            self.mgrs.append(self._new_mgr(len(self.mgrs)))
        for i, m in enumerate(self.mgrs):
            self._assign(m, nodes[i * count:(i + 1) * count])
        self.save_state()
        self.log("workers ready, loading the model on each set")
        for m in self.mgrs:
            await m.start(self.target_model()["id"])

    async def _resize(self, replicas):
        cur = len(self.mgrs)
        itype, count, _ = self.current_shape()
        if replicas > cur:
            nodes = await self._provision(itype, count * (replicas - cur))
            fresh = []
            for i in range(replicas - cur):
                m = self._new_mgr(cur + i)
                self._assign(m, nodes[i * count:(i + 1) * count])
                self.mgrs.append(m)
                fresh.append(m)
            self.save_state()
            self.log(f"added {len(fresh)} replica set(s), loading the model")
            for m in fresh:
                await m.start(self.target_model()["id"])
        elif replicas < cur:
            drop = self.mgrs[replicas:]
            ids = []
            for m in drop:
                await m.stop()
                ids += [w["id"] for w in m.workers if w.get("id")]
            self.log(f"removing {len(drop)} replica set(s)")
            await self._terminate(ids)
            del self.mgrs[replicas:]
            self.save_state()
        else:
            self.log("nothing to change")

    async def replace_dead(self, sid=None):
        self.begin("replace")
        asyncio.create_task(self._replace(sid))

    async def _replace(self, sid):
        try:
            targets = [m for m in self.mgrs if sid in (None, m.id)]
            for m in targets:
                status = await m.workers_status()
                dead = [w["name"] for w in status if not w["up"]]
                if not dead:
                    continue
                self.log(f"set {m.id}: dead {', '.join(dead)}")
                await m.stop()
                by_name = {w["name"]: w for w in m.workers}
                await self._terminate([by_name[n]["id"] for n in dead if by_name[n].get("id")])
                itype = m.workers[0].get("type", "r7g.medium")
                nodes = await self._provision(itype, len(dead))
                for name, node in zip(dead, nodes):
                    node["name"] = name
                    by_name[name] = node
                m.workers[:] = [by_name[w["name"]] for w in m.workers]
                self.save_state()
                self.log(f"set {m.id}: replacements ready, reloading the model")
                await m.start(self.target_model()["id"])
            self.finish()
        except Exception as e:
            self.fail(e)
