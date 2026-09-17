#!/usr/bin/env python3
"""Collect one ECS service's configuration with read-only aws CLI calls. Standard library only.

Usage:
  inventory.py --cluster CLUSTER --service SERVICE [--region REGION] [--out inventory.json]
  inventory.py --cluster C --service S --service-file svc.json --taskdef-file td.json

The second form reads an operator's `aws ecs describe-services` / `describe-task-definition`
export instead of calling AWS, for when the agent has no AWS credentials. Both files must be
what the API returned for the RUNNING service. Infrastructure-as-code is not a substitute: a
declared task definition can carry a placeholder image or a revision the service never adopted.
The provenance of every part is recorded in meta.provenance and printed on stderr.

Schema: `meta`, `service` (describe-services output minus events/deployments/taskSets/tags),
`taskDefinition` (describe-task-definition output), `targetGroups[]`, `taskRoleActions[]`,
`executionRoleActions[]`, `scheduledRules[]`, `imageDigests{}`, `denied[]`, `coverage{}`.
Keys named NOTE_*, WARNING_* or _* are treated as annotations and never become findings.

Only describe/list/get calls are made; no call ever reads a Secrets Manager secret or an
SSM parameter value. All environment values are withheld unless explicitly allowlisted as
non-secret. Labels, log options, and argument flags receive best-effort name-based redaction.
Every failed call is recorded under "denied" so nothing missing is ever assumed present.
"""
import argparse
import datetime
import json
import re
import subprocess

SECRET_KEY = re.compile(r"(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential)")
REDACTED = "<redacted>"
DENIED = []


def aws(*args, region=None):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    try:
        # CLI errors may include the caller ARN; it is already in meta.identity, so nothing new is persisted.
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        DENIED.append({"call": "aws", "error": "aws CLI not found"})
        return None
    if p.returncode != 0:
        DENIED.append({"call": " ".join(args[:4]), "error": p.stderr.strip()[:300]})
        return None
    return json.loads(p.stdout) if p.stdout.strip() else {}


def redact(env, include_env=()):
    # Names are not a reliable way to identify credentials (DATABASE_URL is a common example).
    return [{"name": e["name"], "value": e.get("value", "") if e["name"] in include_env and not SECRET_KEY.search(e["name"]) else REDACTED}
            for e in env or []]


def redact_map(m):
    return {k: REDACTED if SECRET_KEY.search(k) else v for k, v in (m or {}).items()}


def redact_argv(argv):
    out = list(argv or [])
    i = 0
    while i < len(out):
        a = out[i]
        if isinstance(a, str) and a.startswith("-"):
            flag, eq, _ = a.partition("=")
            if SECRET_KEY.search(flag):
                if eq:
                    out[i] = f"{flag}={REDACTED}"
                elif i + 1 < len(out):
                    out[i + 1] = REDACTED
                    i += 1
        i += 1
    return out


def scrub(c, include_env=()):
    """Withhold environment values and redact secret-looking keys/flags. ARNs/names stay."""
    if "environment" in c:
        c["environment"] = redact(c["environment"], include_env)
    if "dockerLabels" in c:
        c["dockerLabels"] = redact_map(c["dockerLabels"])
    if (c.get("logConfiguration") or {}).get("options"):
        c["logConfiguration"]["options"] = redact_map(c["logConfiguration"]["options"])
    for k in ("command", "entryPoint"):
        if c.get(k):
            c[k] = redact_argv(c[k])
    return c


def count_redacted(c):
    vals = [e["value"] for e in c.get("environment", [])]
    vals += list((c.get("dockerLabels") or {}).values())
    vals += list(((c.get("logConfiguration") or {}).get("options") or {}).values())
    vals += list(c.get("command") or []) + list(c.get("entryPoint") or [])
    return sum(1 for v in vals if v == REDACTED or (isinstance(v, str) and v.endswith(f"={REDACTED}")))


def policy_actions(doc):
    stmts = doc.get("Statement", [])
    if isinstance(stmts, dict):
        stmts = [stmts]
    acts = []
    for st in stmts:
        if st.get("Effect") != "Allow":
            continue
        a = st.get("Action", [])
        acts += a if isinstance(a, list) else [a]
    return sorted(set(acts))


def role_actions(role_arn, region):
    if not role_arn:
        return []
    name = role_arn.split("/")[-1]
    acts = []
    for pn in (aws("iam", "list-role-policies", "--role-name", name, region=region) or {}).get("PolicyNames", []):
        d = aws("iam", "get-role-policy", "--role-name", name, "--policy-name", pn, region=region)
        if d:
            acts += policy_actions(d["PolicyDocument"])
    for ap in (aws("iam", "list-attached-role-policies", "--role-name", name, region=region) or {}).get("AttachedPolicies", []):
        pol = aws("iam", "get-policy", "--policy-arn", ap["PolicyArn"], region=region)
        if not pol:
            continue
        v = aws("iam", "get-policy-version", "--policy-arn", ap["PolicyArn"], "--version-id", pol["Policy"]["DefaultVersionId"], region=region)
        if v:
            acts += policy_actions(v["PolicyVersion"]["Document"])
    return sorted(set(acts))


def load_export(path, key):
    """Read an operator's API export. Accepts the full response or the inner object."""
    with open(path) as fh:
        doc = json.load(fh)
    if key == "service":
        if isinstance(doc, dict) and doc.get("services"):
            return doc["services"][0]
        return doc
    if isinstance(doc, dict) and doc.get("taskDefinition"):
        return doc["taskDefinition"]
    return doc


ECR_IMAGE = re.compile(r"^([0-9]{12}\.dkr\.ecr(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?)/([^:@]+):(.+)$")


def image_digests(td, region):
    """Resolve each ECR image reference to the digest actually stored for that tag.

    A tag is mutable: it can be repointed after the service pulled it, so the tag alone is not
    evidence of what is running. The digest is what the destination manifest should pin."""
    out = {}
    for c in td.get("containerDefinitions", []):
        ref = c.get("image", "")
        m = ECR_IMAGE.match(ref)
        if not m or ref in out:
            continue
        d = aws("ecr", "describe-images", "--repository-name", m.group(3),
                "--image-ids", f"imageTag={m.group(4)}", region=m.group(2) or region)
        details = (d or {}).get("imageDetails") or []
        if details and details[0].get("imageDigest"):
            out[ref] = details[0]["imageDigest"]
    return out


def collect(cluster, service, region, include_env=(), service_file=None, taskdef_file=None):
    DENIED.clear()
    R = region
    ident = aws("sts", "get-caller-identity", region=R) or {}
    prov = {"service": "aws-api", "taskDefinition": "aws-api"}
    if service_file:
        svc = load_export(service_file, "service")
        prov["service"] = "file:" + service_file
    else:
        resp = aws("ecs", "describe-services", "--cluster", cluster, "--services", service, region=R) or {}
        for f in resp.get("failures", []):
            DENIED.append({"call": "ecs describe-services", "error": f"{f.get('arn', service)}: {f.get('reason', 'unknown')}"})
        svcs = resp.get("services", [])
        svc = svcs[0] if svcs else {}
    td = {}
    if taskdef_file:
        td = load_export(taskdef_file, "taskDefinition")
        prov["taskDefinition"] = "file:" + taskdef_file
    elif svc.get("taskDefinition"):
        td = (aws("ecs", "describe-task-definition", "--task-definition", svc["taskDefinition"], region=R) or {}).get("taskDefinition", {})
    for c in td.get("containerDefinitions", []):
        scrub(c, include_env)

    tgs = []
    arns = [lb["targetGroupArn"] for lb in svc.get("loadBalancers", []) if lb.get("targetGroupArn")]
    if arns:
        for tg in (aws("elbv2", "describe-target-groups", "--target-group-arns", *arns, region=R) or {}).get("TargetGroups", []):
            tgs.append({"targetGroupArn": tg["TargetGroupArn"], "protocol": tg.get("Protocol"),
                        "healthCheckProtocol": tg.get("HealthCheckProtocol"),
                        "healthCheckPath": tg.get("HealthCheckPath"), "port": tg.get("Port")})

    sched = []
    fam = td.get("family")
    if svc.get("clusterArn") and fam:
        fam_re = re.compile(rf":task-definition/{re.escape(fam)}(:\d+)?$")
        for rn in (aws("events", "list-rule-names-by-target", "--target-arn", svc["clusterArn"], region=R) or {}).get("RuleNames", []):
            targets = (aws("events", "list-targets-by-rule", "--rule", rn, region=R) or {}).get("Targets", [])
            if any(fam_re.search((t.get("EcsParameters") or {}).get("TaskDefinitionArn", "")) for t in targets):
                rule = aws("events", "describe-rule", "--name", rn, region=R) or {}
                sched.append({"name": rn, "scheduleExpression": rule.get("ScheduleExpression", "")})

    digests = image_digests(td, R)
    return {
        "meta": {"account": ident.get("Account", ""), "identity": ident.get("Arn", ""), "region": R or "",
                 "cluster": cluster, "service": service, "provenance": prov,
                 "collected": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
        # Keep configuration we do not yet map, so assessment can report it. Runtime events and
        # deployments are redundant snapshots and may contain application-generated messages.
        "service": {k: v for k, v in svc.items() if k not in {"events", "deployments", "taskSets", "tags"}},
        "taskDefinition": td,
        "targetGroups": tgs,
        "taskRoleActions": role_actions(td.get("taskRoleArn"), R),
        "executionRoleActions": role_actions(td.get("executionRoleArn"), R),
        "scheduledRules": sched,
        "imageDigests": digests,
        "denied": list(DENIED),
        "coverage": {"not_collected": ["ALB listeners and routing rules", "security-group and route rules",
                     "Application Auto Scaling policies", "EventBridge Scheduler schedules",
                     "database and external-service behavior"]
                     + ([] if digests else ["runtime image digests"])},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--region")
    ap.add_argument("--out", default="inventory.json")
    ap.add_argument("--include-env", action="append", default=[], metavar="NAME",
                    help="include this reviewed non-secret environment value; repeat for each name")
    ap.add_argument("--service-file", help="describe-services export to read instead of calling AWS")
    ap.add_argument("--taskdef-file", help="describe-task-definition export to read instead of calling AWS")
    a = ap.parse_args()

    inv = collect(a.cluster, a.service, a.region, a.include_env, a.service_file, a.taskdef_file)
    with open(a.out, "w") as fh:
        json.dump(inv, fh, indent=2)
    cds = inv["taskDefinition"].get("containerDefinitions", [])
    redacted = sum(count_redacted(c) for c in cds)
    print(f"wrote {a.out}: {len(cds)} container(s), {len(inv['targetGroups'])} target group(s), "
          f"{len(inv['scheduledRules'])} schedule(s), {len(inv['imageDigests'])} image digest(s), "
          f"{redacted} value(s) redacted, {len(DENIED)} denied call(s)")
    for part, src in inv["meta"]["provenance"].items():
        if src != "aws-api":
            print(f"  provenance: {part} came from {src}, NOT the live API -- confirm it is the running "
                  "configuration before trusting this inventory")
    for d in DENIED:
        print(f"  denied: {d['call']}: {d['error'][:120]}")


if __name__ == "__main__":
    main()
