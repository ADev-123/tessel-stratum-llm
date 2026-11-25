import json, os, re, time
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

R = "us-east-1"
CFG = Config(retries={"max_attempts": 6, "mode": "adaptive"})
ec2 = boto3.client("ec2", region_name=R, config=CFG)
ssm = boto3.client("ssm", region_name=R, config=CFG)
TAGS = [{"ResourceType": "instance", "Tags": [
    {"Key": "Name", "Value": "tessel-limit-test"},
    {"Key": "tessel:managed", "Value": "true"},
    {"Key": "tessel:purpose", "Value": "limit-test"}]}]
ACTIVE = ["pending", "running", "stopping", "stopped", "shutting-down"]


def code(e):
    return e.response["Error"]["Code"]


def msg(e):
    return re.sub(r"\d{12}", "<acct>", e.response["Error"].get("Message", ""))[:150]


def live():
    out = []
    for p in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ACTIVE}]):
        for r in p["Reservations"]:
            out.extend(r["Instances"])
    return out


def snap():
    return {i["InstanceId"]: (i["InstanceType"], i["State"]["Name"]) for i in live()}


def running(s):
    return sum(1 for v in s.values() if v[1] == "running")


ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")["Parameter"]["Value"]
subs = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
subnet = next(s for s in subs if s["AvailabilityZoneId"] == "use1-az2")["SubnetId"]
try:
    ec2.describe_key_pairs(KeyNames=["vockey"])
    print("key pair vockey: present")
except ClientError as e:
    print("key pair vockey:", code(e))


def dry(itype, **extra):
    try:
        ec2.run_instances(ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1,
                          SubnetId=subnet, DryRun=True, **extra)
        return "allowed"
    except ClientError as e:
        return "allowed" if code(e) == "DryRunOperation" else code(e)


before = snap()
print("\nPRE-EXISTING instances:", before)
max_running = running(before)

print("\n== Phase 1: dry-run map of t2/t3/t3a sizes")
ok, no = [], []
for fam in ("t2", "t3", "t3a"):
    for size in ("nano", "micro", "small", "medium", "large", "xlarge", "2xlarge"):
        t = f"{fam}.{size}"
        (ok if dry(t) == "allowed" else no).append(t)
print("allowed:", ok)
print("denied/other:", no)

print("\n== Phase 1b: root volume sizes on t3.medium (dry-run)")
for size, vtype in [(8, "gp2"), (30, "gp2"), (35, "gp2"), (36, "gp2"), (100, "gp2"), (35, "gp3")]:
    bdm = [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": size, "VolumeType": vtype}}]
    print(f"  {size:>3} GiB {vtype}: {dry('t3.medium', BlockDeviceMappings=bdm)}")

mine, extra = [], []
LAUNCH = dict(ImageId=ami, MinCount=1, MaxCount=1, KeyName=os.environ.get("TESSEL_KEY_NAME", "vockey"), SubnetId=subnet,
              IamInstanceProfile={"Name": os.environ.get("TESSEL_INSTANCE_PROFILE", "LabInstanceProfile")}, TagSpecifications=TAGS,
              MetadataOptions={"HttpTokens": "required"})
try:
    print("\n== Phase 2: real launch, one of each allowed type")
    for t in ["t3.medium", "t3a.medium", "t2.medium", "t3.small", "t2.small", "t3.micro", "t2.micro"]:
        try:
            iid = ec2.run_instances(InstanceType=t, **LAUNCH)["Instances"][0]["InstanceId"]
            mine.append((iid, t))
            print(f"  launched {t:11s} {iid}")
        except ClientError as e:
            print(f"  FAILED   {t:11s} {code(e)}: {msg(e)}")
    ids = [i for i, _ in mine]
    if ids:
        try:
            ec2.get_waiter("instance_running").wait(InstanceIds=ids, WaiterConfig={"Delay": 5, "MaxAttempts": 36})
        except Exception as e:
            print("  wait error:", type(e).__name__)
    s = snap()
    max_running = max(max_running, running(s))
    print(f"  running now (all, incl. pre-existing): {running(s)} | mine: {sum(1 for i in ids if s.get(i, ('', ''))[1] == 'running')}/{len(ids)}")

    print("\n== Phase 2b: SSM registration via LabInstanceProfile (up to 150 s)")
    online, deadline = set(), time.time() + 150
    while ids and time.time() < deadline and len(online) < len(ids):
        info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": ids}])["InstanceInformationList"]
        online = {i["InstanceId"] for i in info if i["PingStatus"] == "Online"}
        time.sleep(10)
    print(f"  SSM online: {len(online)}/{len(ids)}", sorted(t for i, t in mine if i in online))

    print("\n== Phase 3: try to go past the cap (3 extra t3.nano)")
    for n in range(3):
        try:
            iid = ec2.run_instances(InstanceType="t3.nano", **LAUNCH)["Instances"][0]["InstanceId"]
            extra.append(iid)
            print(f"  extra {n + 1} accepted by API: {iid}")
        except ClientError as e:
            print(f"  extra {n + 1} REFUSED {code(e)}: {msg(e)}")
    for tick in range(7):
        time.sleep(15)
        s = snap()
        max_running = max(max_running, running(s))
        st = {i: s.get(i, ("", "gone"))[1] for i in extra}
        print(f"  t+{(tick + 1) * 15:>3}s running={running(s)} extras={list(st.values())}")
    final = snap()
    reaped = [i for i, _ in mine if final.get(i, ("", "gone"))[1] not in ("running", "pending")]
    print("  my base instances reaped by the lab:", reaped)
    print("  pre-existing unchanged:", all(final.get(i) == v for i, v in before.items()))
finally:
    print("\n== Cleanup")
    tagged = []
    for i in live():
        tg = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
        if tg.get("tessel:purpose") == "limit-test":
            tagged.append(i["InstanceId"])
    if tagged:
        ec2.terminate_instances(InstanceIds=tagged)
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=tagged, WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        except Exception as e:
            print("  wait error:", type(e).__name__)
    after = snap()
    print(f"  terminated {len(tagged)} test instances")
    print("  pre-existing after cleanup:", {i: after.get(i) for i in before})
    print("  any test instances still alive:", [i for i in tagged if i in after])
    print(f"\nMAX concurrently running observed (incl. pre-existing): {max_running}")
