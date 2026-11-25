import json, os, re, sys, datetime
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CFG = Config(retries={"max_attempts": 6, "mode": "adaptive"}, connect_timeout=8, read_timeout=25)
HOME = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
OUT = sys.argv[1]
DENY = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess",
        "AuthorizationError", "ForbiddenException", "OptInRequired", "AuthFailure"}


def cl(svc, region=HOME):
    return boto3.client(svc, region_name=region, config=CFG)


def mask(s):
    return re.sub(r"\d{12}", "<acct>", s or "")[:160]


def probe(fn):
    try:
        fn()
        return {"status": "allowed"}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = mask(e.response["Error"].get("Message", ""))
        if code == "DryRunOperation":
            return {"status": "allowed"}
        if code in DENY:
            return {"status": "denied", "code": code, "msg": msg}
        return {"status": "error", "code": code, "msg": msg}
    except Exception as e:
        return {"status": "error", "code": type(e).__name__, "msg": mask(str(e))}


def find_ami(region):
    try:
        v = cl("ssm", region).get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")["Parameter"]["Value"]
        return v
    except Exception:
        pass
    try:
        imgs = cl("ec2", region).describe_images(
            Owners=["amazon"],
            Filters=[{"Name": "name", "Values": ["al2023-ami-2023*-x86_64"]},
                     {"Name": "state", "Values": ["available"]}])["Images"]
        imgs.sort(key=lambda i: i["CreationDate"])
        return imgs[-1]["ImageId"] if imgs else None
    except Exception:
        return None


def dry_run(region, itype, ami):
    ec2 = cl("ec2", region)
    return probe(lambda: ec2.run_instances(ImageId=ami, InstanceType=itype,
                                           MinCount=1, MaxCount=1, DryRun=True))


result = {"discovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
          "home_region": HOME}

arn = cl("sts").get_caller_identity()["Arn"]
parts = arn.split(":")[-1].split("/")
result["identity"] = {"kind": parts[0], "role": parts[1] if len(parts) > 1 else ""}

# ---- regions
try:
    regions = sorted(r["RegionName"] for r in cl("ec2").describe_regions(AllRegions=False)["Regions"])
except Exception:
    regions = ["us-east-1", "us-east-2", "us-west-1", "us-west-2"]
result["regions_enabled_for_account"] = regions


def region_check(region):
    ami = find_ami(region)
    if not ami:
        return region, {"ami": None, "run_instances_t3_micro": {"status": "error", "code": "NoAmi"}}
    return region, {"ami": ami, "run_instances_t3_micro": dry_run(region, "t3.micro", ami)}


with ThreadPoolExecutor(6) as ex:
    result["region_checks"] = dict(ex.map(region_check, regions))

home_ami = result["region_checks"].get(HOME, {}).get("ami") or find_ami(HOME)

# ---- instance type catalog and dry-runs in the home region
fams = ["t3", "t3a", "m5", "m6i", "m6a", "m7i", "c5", "c6i", "c7i", "r5", "r6i"]
sizes = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge"]
want = {f"{f}.{s}" for f in fams for s in sizes} | {"g4dn.xlarge", "g5.xlarge", "p3.2xlarge"}
catalog = {}
try:
    pg = cl("ec2").get_paginator("describe_instance_types")
    for page in pg.paginate():
        for t in page["InstanceTypes"]:
            if t["InstanceType"] in want and "x86_64" in t["ProcessorInfo"]["SupportedArchitectures"]:
                catalog[t["InstanceType"]] = {
                    "vcpu": t["VCpuInfo"]["DefaultVCpus"],
                    "memory_gib": round(t["MemoryInfo"]["SizeInMiB"] / 1024, 2),
                    "burstable": t.get("BurstablePerformanceSupported", False),
                    "network": t["NetworkInfo"]["NetworkPerformance"],
                }
except Exception as e:
    result["catalog_error"] = type(e).__name__

types_to_try = sorted(catalog)
with ThreadPoolExecutor(4) as ex:
    res = list(ex.map(lambda t: (t, dry_run(HOME, t, home_ami)), types_to_try))
result["instance_types"] = {t: {**catalog[t], **r} for t, r in res}

# ---- account limits
lim = {}
try:
    attrs = cl("ec2").describe_account_attributes()["AccountAttributes"]
    lim["ec2_account_attributes"] = {a["AttributeName"]: [v["AttributeValue"] for v in a["AttributeValues"]] for a in attrs}
except Exception as e:
    lim["ec2_account_attributes"] = {"error": type(e).__name__}
for code, name in [("L-1216C47A", "running_on_demand_standard_vcpus"), ("L-0263D0A3", "elastic_ips")]:
    try:
        q = cl("service-quotas").get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]
        lim[name] = q["Value"]
    except Exception as e:
        lim[name] = {"error": getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)}
result["limits"] = lim

# ---- current state / network
net = {}
try:
    ec2 = cl("ec2")
    net["vpcs"] = [{"cidr": v["CidrBlock"], "default": v["IsDefault"]} for v in ec2.describe_vpcs()["Vpcs"]]
    net["subnets"] = [{"az": s["AvailabilityZone"], "az_id": s["AvailabilityZoneId"], "cidr": s["CidrBlock"],
                       "default_for_az": s["DefaultForAz"]} for s in ec2.describe_subnets()["Subnets"]]
    net["azs"] = [{"name": a["ZoneName"], "id": a["ZoneId"]} for a in ec2.describe_availability_zones()["AvailabilityZones"]]
    insts = [i for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]
             if i["State"]["Name"] in ("pending", "running", "stopping", "stopped")]
    net["existing_instances"] = len(insts)
except Exception as e:
    net["error"] = type(e).__name__
result["network"] = net

# ---- service probes
ec2 = cl("ec2")
probes = {
    "ec2_create_vpc": lambda: ec2.create_vpc(CidrBlock="10.77.0.0/16", DryRun=True),
    "ec2_create_security_group": lambda: ec2.create_security_group(GroupName="tessel-probe", Description="probe", DryRun=True),
    "ec2_create_vpc_peering": lambda: ec2.create_vpc_peering_connection(VpcId="vpc-00000000", PeerVpcId="vpc-00000001", DryRun=True),
    "ec2_create_placement_group": lambda: ec2.create_placement_group(GroupName="tessel-probe", Strategy="cluster", DryRun=True),
    "ec2_create_key_pair": lambda: ec2.create_key_pair(KeyName="tessel-probe", DryRun=True),
    "ec2_create_launch_template": lambda: ec2.create_launch_template(LaunchTemplateName="tessel-probe", LaunchTemplateData={"InstanceType": "t3.micro"}, DryRun=True),
    "ec2_create_image": lambda: ec2.create_image(InstanceId="i-00000000000000000", Name="tessel-probe", DryRun=True),
    "autoscaling": lambda: cl("autoscaling").describe_auto_scaling_groups(MaxRecords=1),
    "elbv2": lambda: cl("elbv2").describe_load_balancers(PageSize=1),
    "ssm": lambda: cl("ssm").describe_instance_information(MaxResults=5),
    "s3": lambda: cl("s3").list_buckets(),
    "ecr": lambda: cl("ecr").describe_repositories(maxResults=1),
    "lambda": lambda: cl("lambda").list_functions(MaxItems=1),
    "dynamodb": lambda: cl("dynamodb").list_tables(Limit=1),
    "cloudwatch": lambda: cl("cloudwatch").describe_alarms(MaxRecords=1),
    "secretsmanager": lambda: cl("secretsmanager").list_secrets(MaxResults=1),
    "iam_list_roles": lambda: cl("iam").list_roles(MaxItems=1),
    "route53": lambda: cl("route53").list_hosted_zones(MaxItems="1"),
}
result["service_probes"] = {k: probe(f) for k, f in probes.items()}
try:
    result["iam_instance_profiles"] = [p["InstanceProfileName"] for p in cl("iam").list_instance_profiles()["InstanceProfiles"]][:10]
except Exception as e:
    result["iam_instance_profiles"] = {"error": getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)}

with open(OUT, "w") as f:
    json.dump(result, f, indent=2, sort_keys=True)

# ---- summary
print("identity role:", result["identity"]["role"], "| home region:", HOME)
ok = [r for r, v in result["region_checks"].items() if v["run_instances_t3_micro"]["status"] == "allowed"]
no = {r: v["run_instances_t3_micro"].get("code", v["run_instances_t3_micro"]["status"]) for r, v in result["region_checks"].items()
      if v["run_instances_t3_micro"]["status"] != "allowed"}
print("regions where launching is allowed:", ok)
print("regions blocked/other:", no)
allowed = {t: v for t, v in result["instance_types"].items() if v["status"] == "allowed"}
print(f"\ninstance types allowed in {HOME}: {len(allowed)} of {len(result['instance_types'])} checked")
for t, v in sorted(allowed.items(), key=lambda kv: (kv[1]["memory_gib"], kv[0])):
    print(f"  {t:14s} {v['vcpu']:>2} vCPU {v['memory_gib']:>6} GiB  burstable={v['burstable']}")
den = sorted(t for t, v in result["instance_types"].items() if v["status"] == "denied")
oth = {t: v.get("code") for t, v in result["instance_types"].items() if v["status"] == "error"}
print("denied:", den)
print("other errors:", oth)
print("\nlimits:", json.dumps(result["limits"]))
print("network:", json.dumps({k: v for k, v in net.items() if k != "subnets"}))
print("subnets:", [(s["az"], s["az_id"]) for s in net.get("subnets", [])])
print("\nservices:")
for k, v in result["service_probes"].items():
    print(f"  {k:28s} {v['status']}" + (f"  ({v.get('code')})" if v.get("code") else ""))
print("instance profiles:", result["iam_instance_profiles"])
