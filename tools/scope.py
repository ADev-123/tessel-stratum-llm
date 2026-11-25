import re, sys, collections
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

R = "us-east-1"
CFG = Config(retries={"max_attempts": 8, "mode": "adaptive"}, connect_timeout=8, read_timeout=25)
DENY = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess",
        "AuthorizationError", "ForbiddenException", "AuthFailure"}


def cl(s):
    return boto3.client(s, region_name=R, config=CFG)


def mask(s):
    return re.sub(r"\d{12}", "<acct>", s or "")


ec2 = cl("ec2")
print("role:", cl("sts").get_caller_identity()["Arn"].split(":")[-1].split("/")[1])

print("\n== Instances now")
for p in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]):
    for r in p["Reservations"]:
        for i in r["Instances"]:
            tags = {t["Key"]: mask(t["Value"]) for t in i.get("Tags", [])}
            keep = {k: v for k, v in tags.items() if k == "Name" or k.startswith("aws:")}
            prof = mask((i.get("IamInstanceProfile") or {}).get("Arn", "").split("/")[-1])
            print(f"  {i['InstanceId']} {i['InstanceType']:10s} {i['State']['Name']:8s} profile={prof} tags={keep}")
print("  autoscaling groups:", [g["AutoScalingGroupName"] for g in cl("autoscaling").describe_auto_scaling_groups()["AutoScalingGroups"]])
try:
    st = cl("cloudformation").describe_stacks()["Stacks"]
    print("  cloudformation stacks:", [(mask(s["StackName"]), s["StackStatus"]) for s in st])
except ClientError as e:
    print("  cloudformation:", e.response["Error"]["Code"])

ssm = cl("ssm")
ami = {a: ssm.get_parameter(Name=f"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{a}")["Parameter"]["Value"]
       for a in ("x86_64", "arm64")}

types = []
for p in ec2.get_paginator("describe_instance_types").paginate():
    for t in p["InstanceTypes"]:
        arch = t["ProcessorInfo"]["SupportedArchitectures"]
        a = "arm64" if "arm64" in arch else ("x86_64" if "x86_64" in arch else None)
        if a:
            types.append((t["InstanceType"], a, t["VCpuInfo"]["DefaultVCpus"], round(t["MemoryInfo"]["SizeInMiB"] / 1024, 1)))
print(f"\n== Full EC2 sweep: {len(types)} instance types (x86_64 + arm64, includes older generations)")


def dry(item):
    t, a, v, m = item
    try:
        ec2.run_instances(ImageId=ami[a], InstanceType=t, MinCount=1, MaxCount=1, DryRun=True)
        return item, "allowed"
    except ClientError as e:
        c = e.response["Error"]["Code"]
        return item, "allowed" if c == "DryRunOperation" else ("denied" if c in DENY else c)
    except Exception as e:
        return item, type(e).__name__


with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(dry, types))
cnt = collections.Counter(r for _, r in res)
print("result counts:", dict(cnt))
allowed = sorted([i for i, r in res if r == "allowed"], key=lambda i: (i[3], i[0]))
print("ALLOWED types:")
for t, a, v, m in allowed:
    print(f"  {t:14s} {a:7s} {v:>3} vCPU {m:>6} GiB")
big = [i[0] for i in allowed if i[3] >= 8]
print("allowed with >= 8 GiB:", big or "none")
other = collections.defaultdict(list)
for i, r in res:
    if r not in ("allowed", "denied"):
        other[r].append(i[0])
for c, ts in other.items():
    print(f"other code {c}: {len(ts)} types, e.g. {ts[:6]}")

print("\n== Other compute-capable services (read-only list calls)")


def probe(name, fn):
    try:
        fn()
        return "accessible"
    except ClientError as e:
        c = e.response["Error"]["Code"]
        return "denied" if c in DENY or "Denied" in c or "NotAuthorized" in c or "Unauthorized" in c else f"error:{c}"
    except Exception as e:
        return f"error:{type(e).__name__}"


probes = {
    "sagemaker": lambda: cl("sagemaker").list_notebook_instances(MaxResults=1),
    "sagemaker endpoints": lambda: cl("sagemaker").list_endpoints(MaxResults=1),
    "emr": lambda: cl("emr").list_clusters(),
    "ecs": lambda: cl("ecs").list_clusters(),
    "eks": lambda: cl("eks").list_clusters(),
    "batch": lambda: cl("batch").describe_compute_environments(maxResults=1),
    "lightsail": lambda: cl("lightsail").get_instances(),
    "apprunner": lambda: cl("apprunner").list_services(MaxResults=1),
    "elasticbeanstalk": lambda: cl("elasticbeanstalk").describe_applications(),
    "glue": lambda: cl("glue").get_databases(MaxResults=1),
    "bedrock": lambda: cl("bedrock").list_foundation_models(),
    "lambda account": lambda: cl("lambda").get_account_settings(),
}
for k, f in probes.items():
    print(f"  {k:22s} {probe(k, f)}")
