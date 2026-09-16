#!/usr/bin/env python3
"""Assess one ECS/Fargate service inventory for Cloud Run. Standard library only.

Usage:
  assess.py --inventory inventory.json [--src DIR] [--rules references/rules.json] [--out assessment.json]

Every finding that makes a claim about Cloud Run comes from a row in rules.json and carries that
row's url and quote. Findings with reason denied, source-unavailable or not-covered are about AWS
evidence and carry no citation. Reason `stale` marks a `supported` row whose citation failed doc
drift: the finding downgrades to needs-investigation and keeps its url and quote.
Missing evidence never produces `supported`.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "references", "rules.json")
ORDER = {"supported": 0, "needs-investigation": 1, "blocked": 2}

# Cloud Run memory bounds (MiB) per vCPU tier, from the documented table:
# https://docs.cloud.google.com/run/docs/configuring/services/memory-limits
# Fargate sizes below 1 vCPU are rounded up to 1 so the <1 vCPU constraints never apply.
MEM_BOUNDS = {1: (128, 4096), 2: (128, 8192), 4: (2048, 16384), 8: (4096, 32768)}

JS = r"\.(js|ts|mjs|cjs|jsx|tsx)$"
JVM = r"\.(java|kt|scala)$"
SDK_PATTERNS = [
    (r"\.py$", r"""\b\w+\.(?:client|resource)\(\s*['"]([a-z0-9-]+)['"]"""),
    (JS, r"""@aws-sdk/client-([a-z0-9-]+)"""),
    (JS, r"""aws-sdk/clients/([a-z0-9]+)"""),
    (JS, r"""new\s+AWS\.([A-Za-z0-9]+)\("""),
    (r"\.go$", r"""github\.com/aws/aws-sdk-go(?:-v2)?/service/([a-z0-9]+)"""),
    (JVM, r"""software\.amazon\.awssdk\.services\.([a-z0-9]+)"""),
    (JVM, r"""com\.amazonaws\.services\.([a-z0-9]+)"""),
    (r"\.cs$", r"""using\s+Amazon\.([A-Za-z0-9]+)\s*;"""),
]
CS_NOT_SERVICES = {"runtime", "extensions", "util"}
SKIP_DIRS = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist", "build"}

# Field paths the checks below evaluate, at three depths only: top-level taskDefinition keys,
# containerDefinitions[] keys, and top-level service keys. Nested keys under a covered path are
# not walked. Anything else at those depths must be in rules.json "ignore", otherwise it becomes a
# not-covered finding. Silently skipping a field is never allowed.
COVERED = {
    "cpu", "memory", "containerDefinitions", "volumes", "taskRoleArn", "runtimePlatform",
    "containerDefinitions[].image", "containerDefinitions[].portMappings",
    "containerDefinitions[].entryPoint", "containerDefinitions[].command",
    "containerDefinitions[].environment", "containerDefinitions[].secrets",
    "containerDefinitions[].healthCheck", "containerDefinitions[].logConfiguration",
    "service.desiredCount", "service.networkConfiguration", "service.loadBalancers",
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


def not_covered(subject, evidence):
    return aws_finding("needs-investigation", "not-covered", subject, evidence)


def secret_name(value_from):
    """ARN of a Secrets Manager secret or SSM parameter -> a valid Secret Manager id."""
    if ":secret:" in value_from:
        tail = value_from.split(":secret:", 1)[1].split(":")[0]
        # AWS appends -XXXXXX (mixed case/digits); an all-lowercase tail such as -secret is part of the name.
        tail = re.sub(r"-(?![a-z]{6}$)[A-Za-z0-9]{6}$", "", tail)
    else:
        tail = value_from.split(":parameter/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_-]", "-", tail).strip("-")


def size_units(value):
    """'1024' -> 1024; '1 vCPU' / '2 GB' -> 1024 / 2048; missing or unparseable -> None."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(vcpu|gb)?", str(value), re.I)
    if not m:
        return None
    n = float(m.group(1))
    return int(n * 1024) if m.group(2) else int(n)


def fargate_size(td):
    """(vcpu, mib, cr_cpu); each is None when its source value is missing or unparseable."""
    cpu, mib = size_units(td.get("cpu")), size_units(td.get("memory"))
    vcpu = cpu / 1024 if cpu is not None else None
    cr_cpu = max(int(vcpu), 1) if vcpu is not None else None
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
    """Return ({aws_service: [relative/file:line, ...]}, files_scanned) for SDK client usage in the source tree."""
    hits, scanned = {}, 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            pats = [p for ext, p in SDK_PATTERNS if re.search(ext, fn)]
            if not pats:
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            scanned += 1
            for i, line in enumerate(lines, 1):
                for p in pats:
                    for m in re.finditer(p, line):
                        name = m.group(1).lower()
                        if fn.endswith(".cs"):
                            name = re.sub(r"v\d+$", "", name)
                            if name in CS_NOT_SERVICES:
                                continue
                        hits.setdefault(name, []).append(f"{os.path.relpath(path, src_dir)}:{i}")
    return hits, scanned


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

    if src_dir is None or not os.path.isdir(src_dir):
        ev = "no source tree given" if src_dir is None else f"not a directory: {src_dir}"
        F.append(aws_finding("needs-investigation", "source-unavailable", src_dir or "", [ev]))
        src_dir = None
    for d in inv.get("denied", []):
        F.append(aws_finding("blocked", "denied", d["call"], [d.get("error", "")]))
    if not cds:
        if not inv.get("denied"):
            F.append(aws_finding("blocked", "denied", "taskDefinition", ["empty task definition"]))
        return F

    ing = ingress_container(inv)

    # Ports: only plain TCP mappings can become the Cloud Run port; anything else is not covered
    # but still counts as "exposes a port" so the task is not misread as a background worker.
    tcp = {}  # container name -> [containerPort]
    has_port = False
    for c in cds:
        for pm in c.get("portMappings", []):
            where = f"containerDefinitions[{c.get('name')}].portMappings[]"
            proto = str(pm.get("protocol") or "tcp").lower()
            app = str(pm.get("appProtocol") or "").lower()
            if pm.get("containerPortRange"):
                has_port = True
                F.append(not_covered("containerDefinitions[].portMappings[].containerPortRange", [f"{where}.containerPortRange={pm['containerPortRange']}"]))
                continue
            if not pm.get("containerPort"):
                continue
            has_port = True
            if proto != "tcp":
                F.append(not_covered(f"containerDefinitions[].portMappings[].protocol={proto}", [f"{where}.protocol={proto} containerPort={pm['containerPort']}"]))
                continue
            if app in ("grpc", "http2"):
                F.append(not_covered(f"containerDefinitions[].portMappings[].appProtocol={app}", [f"{where}.appProtocol={app} containerPort={pm['containerPort']}"]))
            tcp.setdefault(c.get("name"), []).append(pm["containerPort"])

    # Workload type
    if not has_port:
        if inv.get("scheduledRules"):
            ev = [f"scheduledRules[]: {r.get('name')} {r.get('scheduleExpression', '')}".strip() for r in inv["scheduledRules"]]
            F.append(finding(R["workload.scheduled"], ev))
        else:
            F.append(finding(R["workload.background"], ["no containerDefinitions[].portMappings"]))
    elif tcp:
        own = tcp.get(ing.get("name")) or next(iter(tcp.values()))
        lb_port = ((svc.get("loadBalancers") or [{}])[0]).get("containerPort")
        port = lb_port if lb_port in own else own[0]
        ev = [f"containerDefinitions[{ing.get('name')}].portMappings[].containerPort={port}"]
        if len(own) > 1:
            ev.append(f"multiple ports: {own}; Cloud Run exposes one")
        F.append(finding(R["container.port"], ev, value=port))

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
    for c in cds:
        drv = (c.get("logConfiguration") or {}).get("logDriver")
        if c.get("logConfiguration") and drv != "awslogs":
            F.append(not_covered(f"containerDefinitions[].logConfiguration.logDriver={drv}", [f"containerDefinitions[{c.get('name')}].logConfiguration.logDriver={drv}"]))

    # Resources: smallest Cloud Run CPU tier >= the Fargate vCPU whose memory range holds the task memory
    vcpu, mib, cr_cpu = fargate_size(td)
    ev = [f"cpu={td.get('cpu')} memory={td.get('memory')}"]
    if vcpu is None or mib is None:
        F.append(finding(R["resources.unsupported-pair"], [ev[0] + " (missing or unparseable)"], subject=ev[0]))
    else:
        tier = next((t for t in sorted(MEM_BOUNDS) if t >= cr_cpu and MEM_BOUNDS[t][0] <= mib <= MEM_BOUNDS[t][1]), None)
        if tier is None:
            F.append(finding(R["resources.unsupported-pair"], ev, subject=f"{vcpu:g} vCPU / {mib} MiB"))
        else:
            if tier != vcpu:
                ev.append(f"Cloud Run cpu={tier} chosen for {vcpu:g} vCPU / {mib} MiB")
            F.append(finding(R["resources.cpu-memory"], ev, value={"cpu": str(tier), "memory": f"{mib}Mi"}))

    # Health
    tgs = inv.get("targetGroups", [])
    tg_http = [tg for tg in tgs
               if str(tg.get("healthCheckProtocol", "")).upper() in ("HTTP", "HTTPS") and tg.get("healthCheckPath")]
    if tg_http:
        F.append(finding(R["health.http-probe"], [f"targetGroups[].healthCheckPath={tg_http[0]['healthCheckPath']}"], value=tg_http[0]["healthCheckPath"]))
    elif tgs:
        for tg in tgs:
            proto = tg.get("healthCheckProtocol")
            F.append(not_covered(f"targetGroups[].healthCheckProtocol={proto}", [f"targetGroups[{tg.get('targetGroupArn')}].healthCheckProtocol={proto} healthCheckPath={tg.get('healthCheckPath')}"]))
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
            keys = ",".join(sorted(k for k in v if k != "name")) or "(name only)"
            F.append(not_covered(f"volumes[].{keys}", [f"volumes[{v.get('name')}]"]))

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
    if src_dir is not None:
        hits, scanned = scan(src_dir)
        if scanned == 0:
            F.append(aws_finding("needs-investigation", "source-unavailable", src_dir, ["no files with a recognized source extension"]))
        for s, locs in hits.items():
            deps.setdefault(s, []).extend(f"src: {l}" for l in locs)
    for s in sorted(deps):
        F.append(finding(R["deps.aws-service"], deps[s], subject=s))

    # Unmatched fields
    for p in uncovered(inv, rulesdoc.get("ignore", [])):
        F.append(not_covered(p, [p]))
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
    with open(a.inventory) as fh:
        inv = json.load(fh)
    with open(a.rules) as fh:
        rulesdoc = json.load(fh)
    findings = assess(inv, a.src, rulesdoc)
    roll = rollup(findings)
    with open(a.out, "w") as fh:
        json.dump({"meta": inv.get("meta", {}), "rollup": roll, "findings": findings}, fh, indent=2)
    print(summary(findings, roll))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
