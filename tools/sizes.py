import time, collections
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

R = "us-east-1"
CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=8, read_timeout=25)
ec2 = boto3.client("ec2", region_name=R, config=CFG)
ssm = boto3.client("ssm", region_name=R, config=CFG)
ami = {a: ssm.get_parameter(Name=f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{a}")["Parameter"]["Value"]
       for a in ("x86_64", "arm64")}

by_size = collections.defaultdict(list)
for p in ec2.get_paginator("describe_instance_types").paginate():
    for t in p["InstanceTypes"]:
        a = t["ProcessorInfo"]["SupportedArchitectures"]
        arch = "arm64" if "arm64" in a else ("x86_64" if "x86_64" in a else None)
        if arch:
            fam, size = t["InstanceType"].split(".", 1)
            by_size[size].append((t["InstanceType"], arch, t["VCpuInfo"]["DefaultVCpus"], round(t["MemoryInfo"]["SizeInMiB"] / 1024, 1)))

sample = []
for size, items in by_size.items():
    items.sort()
    step = max(1, len(items) // 14)
    sample.extend(items[::step][:14])


def dry(item):
    t, arch, v, m = item
    for attempt in range(8):
        try:
            ec2.run_instances(ImageId=ami[arch], InstanceType=t, MinCount=1, MaxCount=1, DryRun=True)
            return item, "allowed"
        except ClientError as e:
            c = e.response["Error"]["Code"]
            if c == "DryRunOperation":
                return item, "allowed"
            if c == "RequestLimitExceeded":
                time.sleep(2 + attempt * 2)
                continue
            return item, "denied" if "Unauthorized" in c or "Denied" in c else c
    return item, "throttled"


with ThreadPoolExecutor(3) as ex:
    res = list(ex.map(dry, sample))
tab = collections.defaultdict(lambda: collections.Counter())
for (t, a, v, m), r in res:
    tab[t.split(".", 1)[1]][r] += 1
print(f"sampled {len(sample)} of {sum(len(v) for v in by_size.values())} types across {len(by_size)} size names\n")
print(f"{'size':14s} allowed denied other")
order = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "3xlarge", "4xlarge", "6xlarge", "8xlarge", "9xlarge", "12xlarge", "16xlarge", "18xlarge", "24xlarge", "32xlarge", "48xlarge"]
for s in sorted(tab, key=lambda x: (order.index(x) if x in order else 99, x)):
    c = tab[s]
    other = {k: v for k, v in c.items() if k not in ("allowed", "denied")}
    print(f"{s:14s} {c['allowed']:>7} {c['denied']:>6} {other or ''}")
print("\nallowed sizes:", sorted({t.split('.', 1)[1] for (t, a, v, m), r in res if r == 'allowed'}))
