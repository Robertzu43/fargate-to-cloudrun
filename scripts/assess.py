#!/usr/bin/env python3
"""Assess one ECS/Fargate service inventory for Cloud Run. Standard library only.

Usage:
  assess.py --inventory inventory.json [--src DIR] [--rules references/rules.json] [--out assessment.json]

Every finding that makes a claim about Cloud Run comes from a row in rules.json and carries that
row's url and quote. Findings with a `reason` (denied, source-unavailable, not-covered) are about
AWS evidence and carry no citation. Missing evidence never produces `supported`.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "references", "rules.json")
ORDER = {"supported": 0, "needs-investigation": 1, "blocked": 2}

# Cloud Run memory bounds (MiB) per vCPU, from the documented table:
# https://docs.cloud.google.com/run/docs/configuring/services/memory-limits
# Fargate sizes below 1 vCPU are rounded up to 1 so the <1 vCPU constraints never apply.
MEM_BOUNDS = {1: (128, 4096), 2: (128, 8192), 4: (2048, 16384), 8: (4096, 32768)}

SDK_PATTERNS = [
    (r"\.py$", r"""boto3\.(?:client|resource)\(\s*['"]([a-z0-9-]+)['"]"""),
    (r"\.(js|ts|mjs|cjs|jsx|tsx)$", r"""@aws-sdk/client-([a-z0-9-]+)"""),
    (r"\.(js|ts|mjs|cjs|jsx|tsx)$", r"""new\s+AWS\.([A-Za-z0-9]+)\("""),
    (r"\.go$", r"""github\.com/aws/aws-sdk-go(?:-v2)?/service/([a-z0-9]+)"""),
    (r"\.(java|kt|scala)$", r"""software\.amazon\.awssdk\.services\.([a-z0-9]+)"""),
    (r"\.cs$", r"""using\s+Amazon\.([A-Za-z0-9]+)\s*;"""),
]
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}

# Field paths the checks below evaluate. Anything else must be in rules.json "ignore",
# otherwise it becomes a not-covered finding. Silently skipping a field is never allowed.
COVERED = {
    "cpu", "memory", "containerDefinitions", "volumes", "taskRoleArn", "runtimePlatform",
    "containerDefinitions[].image", "containerDefinitions[].portMappings",
    "containerDefinitions[].entryPoint", "containerDefinitions[].command",
    "containerDefinitions[].environment", "containerDefinitions[].secrets",
    "containerDefinitions[].healthCheck",
    "service.desiredCount", "service.launchType", "service.networkConfiguration", "service.loadBalancers",
}


def finding(rule, evidence, subject="", value=None):
    f = {
        "rule": rule["id"], "verdict": rule["verdict"], "subject": subject, "evidence": evidence,
        "explain": rule["explain"], "url": rule["url"], "quote": rule["quote"],
    }
    if rule.get("stale") and rule["verdict"] == "supported":
        f["verdict"] = "needs-investigation"
        f["reason"] = "stale"
    if value is not None:
        f["value"] = value
    return f


def aws_finding(verdict, reason, subject, evidence):
    return {"rule": reason, "verdict": verdict, "subject": subject, "evidence": evidence, "reason": reason}


def secret_name(value_from):
    """ARN of a Secrets Manager secret or SSM parameter -> a valid Secret Manager id."""
    if ":secret:" in value_from:
        tail = value_from.split(":secret:", 1)[1].split(":")[0]
        tail = re.sub(r"-[A-Za-z0-9]{6}$", "", tail)
    else:
        tail = value_from.split(":parameter/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "-", tail).strip("-")


def fargate_size(td):
    vcpu = int(td.get("cpu", "0")) / 1024
    mib = int(td.get("memory", "0"))
    cr_cpu = int(vcpu) if vcpu >= 1 else 1
    return vcpu, mib, cr_cpu


def ingress_container(inv):
    cds = inv["taskDefinition"].get("containerDefinitions", [])
    lbs = inv.get("service", {}).get("loadBalancers") or []
    if lbs:
        for c in cds:
            if c.get("name") == lbs[0].get("containerName"):
                return c
    for c in cds:
        if c.get("portMappings"):
            return c
    return cds[0] if cds else {}


def scan(src_dir):
    """Return {aws_service: [relative/file:line, ...]} for SDK client usage in the source tree."""
    hits = {}
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            pats = [p for ext, p in SDK_PATTERNS if re.search(ext, fn)]
            if not pats:
                continue
            path = os.path.join(root, fn)
            try:
                lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                for p in pats:
                    for m in re.finditer(p, line):
                        hits.setdefault(m.group(1).lower(), []).append(f"{os.path.relpath(path, src_dir)}:{i}")
    return hits


def uncovered(inv, ignore):
    td = inv["taskDefinition"]
    paths = set()
    for k, v in td.items():
        if k == "containerDefinitions":
            for c in v:
                paths.update(f"containerDefinitions[].{ck}" for ck in c)
        else:
            paths.add(k)
    paths.update(f"service.{k}" for k in inv.get("service", {}))
    skip = COVERED | set(ignore)
    return sorted(p for p in paths if p not in skip)


def assess(inv, src_dir, rulesdoc):
    R = {r["id"]: r for r in rulesdoc["rules"]}
    td = inv.get("taskDefinition") or {}
    svc = inv.get("service") or {}
    cds = td.get("containerDefinitions", [])
    F = []

    for d in inv.get("denied", []):
        F.append(aws_finding("blocked", "denied", d["call"], [d.get("error", "")]))
    if src_dir is None:
        F.append(aws_finding("needs-investigation", "source-unavailable", "", ["no source tree given"]))
    if not cds:
        if not inv.get("denied"):
            F.append(aws_finding("blocked", "denied", "taskDefinition", ["empty task definition"]))
        return F

    ing = ingress_container(inv)
    ports = [pm["containerPort"] for c in cds for pm in c.get("portMappings", []) if pm.get("containerPort")]

    # Workload type
    if not ports:
        if inv.get("scheduledRules"):
            ev = [f"scheduledRules[]: {r.get('name')} {r.get('scheduleExpression', '')}".strip() for r in inv["scheduledRules"]]
            F.append(finding(R["workload.scheduled"], ev))
        else:
            F.append(finding(R["workload.background"], ["no containerDefinitions[].portMappings"]))
    else:
        own = [pm["containerPort"] for pm in ing.get("portMappings", []) if pm.get("containerPort")]
        port = own[0] if own else ports[0]
        F.append(finding(R["container.port"], [f"containerDefinitions[{ing.get('name')}].portMappings[].containerPort={port}"], value=port))

    # Platform: Cloud Run is Linux x86_64 only
    rp = td.get("runtimePlatform") or {}
    arch = str(rp.get("cpuArchitecture") or "X86_64").upper()
    osf = str(rp.get("operatingSystemFamily") or "LINUX").upper()
    if arch != "X86_64" or not osf.startswith("LINUX"):
        F.append(finding(R["container.architecture"], [f"runtimePlatform={rp}"], subject=f"{osf}/{arch}"))

    # Containers
    F.append(finding(R["container.image"], [f"containerDefinitions[{ing.get('name')}].image={ing.get('image')}"], value=ing.get("image")))
    if len(cds) > 1:
        others = [c.get("name", "") for c in cds if c is not ing]
        F.append(finding(R["container.multiple"], [f"containerDefinitions[] count={len(cds)}"], subject=",".join(others)))
    if ing.get("entryPoint") or ing.get("command"):
        F.append(finding(
            R["container.command"],
            [f"containerDefinitions[{ing.get('name')}].entryPoint={ing.get('entryPoint')} command={ing.get('command')}"],
            value={"command": ing.get("entryPoint") or [], "args": ing.get("command") or []},
        ))

    # Resources
    vcpu, mib, cr_cpu = fargate_size(td)
    bounds = MEM_BOUNDS.get(cr_cpu)
    ev = [f"cpu={td.get('cpu')} memory={td.get('memory')}"]
    if bounds and bounds[0] <= mib <= bounds[1]:
        F.append(finding(R["resources.cpu-memory"], ev, value={"cpu": str(cr_cpu), "memory": f"{mib}Mi"}))
    else:
        F.append(finding(R["resources.unsupported-pair"], ev, subject=f"{vcpu:g} vCPU / {mib} MiB"))

    # Health
    tg_http = [tg for tg in inv.get("targetGroups", [])
               if str(tg.get("healthCheckProtocol", "")).upper() in ("HTTP", "HTTPS") and tg.get("healthCheckPath")]
    if tg_http:
        F.append(finding(R["health.http-probe"], [f"targetGroups[].healthCheckPath={tg_http[0]['healthCheckPath']}"], value=tg_http[0]["healthCheckPath"]))
    for c in cds:
        if c.get("healthCheck"):
            F.append(finding(R["health.command-probe"], [f"containerDefinitions[{c.get('name')}].healthCheck.command={c['healthCheck'].get('command')}"], subject=c.get("name", "")))

    # Env and secrets on the ingress container
    if ing.get("environment"):
        F.append(finding(R["config.env"], [f"containerDefinitions[{ing.get('name')}].environment ({len(ing['environment'])} vars)"], value=ing["environment"]))
    if ing.get("secrets"):
        names = [{"name": s["name"], "secret": secret_name(s["valueFrom"])} for s in ing["secrets"]]
        F.append(finding(R["secrets.env"], [f"containerDefinitions[{ing.get('name')}].secrets[].valueFrom={s['valueFrom']}" for s in ing["secrets"]], value=names))

    # Storage
    for v in td.get("volumes", []):
        if v.get("efsVolumeConfiguration"):
            F.append(finding(R["storage.efs"], [f"volumes[{v.get('name')}].efsVolumeConfiguration.fileSystemId={v['efsVolumeConfiguration'].get('fileSystemId')}"], subject=v.get("name", "")))
        else:
            keys = ",".join(sorted(k for k in v if k != "name"))
            F.append(aws_finding("needs-investigation", "not-covered", f"volumes[].{keys}", [f"volumes[{v.get('name')}]"]))

    # Network
    awsvpc = (svc.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
    if str(awsvpc.get("assignPublicIp", "")).upper() == "DISABLED":
        F.append(finding(R["network.private-subnets"], [f"service.networkConfiguration.awsvpcConfiguration.subnets={awsvpc.get('subnets')}"]))

    # Scaling and timeout
    if "desiredCount" in svc:
        F.append(finding(R["scaling.min-instances"], [f"service.desiredCount={svc['desiredCount']}"], value=svc["desiredCount"]))
    F.append(finding(R["timeout.request"], ["no ECS source field; Cloud Run documented default"], value=300))

    # AWS dependencies: task-role actions ∪ SDK clients in source, one finding per AWS service
    deps = {}
    for a in inv.get("taskRoleActions", []):
        deps.setdefault(a.split(":")[0].lower(), []).append(f"iam: {a}")
    if src_dir is not None and os.path.isdir(src_dir):
        for s, locs in scan(src_dir).items():
            deps.setdefault(s, []).extend(f"src: {l}" for l in locs)
    for s in sorted(deps):
        F.append(finding(R["deps.aws-service"], deps[s], subject=s))

    # Unmatched fields
    for p in uncovered(inv, rulesdoc.get("ignore", [])):
        F.append(aws_finding("needs-investigation", "not-covered", p, [p]))
    return F


def rollup(findings):
    if not findings:
        return "blocked"
    return max((f["verdict"] for f in findings), key=ORDER.__getitem__)


def summary(findings, roll):
    out = [f"ROLLUP: {roll}", ""]
    for v in ("blocked", "needs-investigation", "supported"):
        group = [f for f in findings if f["verdict"] == v]
        if not group:
            continue
        out.append(f"== {v} ({len(group)})")
        for f in group:
            head = f"- {f['rule']}" + (f" [{f['subject']}]" if f.get("subject") else "")
            out.append(head)
            for e in f["evidence"]:
                out.append(f"    evidence: {e}")
            if f.get("reason"):
                out.append(f"    reason: {f['reason']}")
            if f.get("url"):
                out.append(f"    docs: {f['url']}")
                out.append(f"    quote: \"{f['quote']}\"")
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--src", help="path to the service's source tree; omit if unavailable")
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--out", default="assessment.json")
    a = ap.parse_args()
    inv = json.load(open(a.inventory))
    rulesdoc = json.load(open(a.rules))
    findings = assess(inv, a.src, rulesdoc)
    roll = rollup(findings)
    json.dump({"meta": inv.get("meta", {}), "rollup": roll, "findings": findings}, open(a.out, "w"), indent=2)
    print(summary(findings, roll))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
