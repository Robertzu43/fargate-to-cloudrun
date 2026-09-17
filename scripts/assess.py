#!/usr/bin/env python3
"""Assess one ECS/Fargate service inventory for Cloud Run. Standard library only.

Usage:
  assess.py --inventory inventory.json [--src DIR] [--rules references/rules.json]
            [--resolutions resolutions.json] [--out assessment.json]

Every finding that makes a claim about Cloud Run comes from a row in rules.json and carries that
row's url and quote. Findings with reason denied, source-unavailable or not-covered are about AWS
evidence and carry no citation. Reason `stale` marks a `supported` row whose citation failed doc
drift: the finding downgrades to needs-investigation and keeps its url and quote.
Supported findings describe checked fields only; inventory gaps and runtime validation remain explicit.

A finding that cannot become supported by collecting more evidence -- a denied AWS call, an absent source
tree, a field outside the mappings -- is adjudicated in a resolutions file, not by editing the assessment:

  {"denied:ecs:DescribeServices": {"decision": "why this is safe to proceed without",
                                   "evidence": ["what was checked instead"]}}

Key is `rule` or `rule:subject`, exactly as the summary and the generator's refusal print it. A resolved
finding keeps its original verdict in `original_verdict` and carries the decision into the assessment.
"""
import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "references", "rules.json")
ORDER = {"supported": 0, "needs-investigation": 1, "blocked": 2}

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
ANNOTATION = re.compile(r"(?:^|\.)(?:NOTE|WARNING|_)")

# Field paths the checks below evaluate, at three depths only: top-level taskDefinition keys,
# containerDefinitions[] keys, and top-level service keys. Nested keys under a covered path are
# not walked. Anything else at those depths must be in rules.json "ignore", otherwise it becomes a
# not-covered finding. Nested semantics still need the agent's migration review.
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


def secret_mappings(secrets):
    """Destination ids for one container's secrets. Readable by default: the sha256 of the full
    reference (which includes account, region, JSON selector and version selector) is appended only
    where two sources normalize to the same id, so operators can recognize an id in the console."""
    base = [(s, (secret_name(s["valueFrom"]) or "secret")[:200]) for s in secrets]
    counts = {}
    for _, b in base:
        counts[b] = counts.get(b, 0) + 1
    out = []
    for s, b in base:
        sid = b if counts[b] == 1 else b + "-" + hashlib.sha256(s["valueFrom"].encode()).hexdigest()[:16]
        out.append({"name": s["name"], "secret": sid, "source": s["valueFrom"]})
    return out


def size_units(value):
    """'1024' -> 1024; '1 vCPU' / '2 GB' -> 1024 / 2048; '512 MiB' -> 512; missing or unparseable -> None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(vcpu|gb|mb|mib)?\s*", str(value), re.I)
    if not m:
        return None
    n = float(m.group(1))
    return int(n * 1024) if m.group(2) and m.group(2).lower() in ("vcpu", "gb") else int(n)


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
    # NOTE_/WARNING_/_ keys are the agent's own provenance notes on a hand-built inventory, not fields.
    return sorted(p for p in paths if p not in skip and not ANNOTATION.search(p))


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

    for key in ("service", "targetGroups", "taskRoleActions", "scheduledRules", "denied"):
        if key not in inv:
            F.append(not_covered("inventory." + key, ["required evidence was not collected"]))

    ing = ingress_container(inv)
    collected_tgs = {tg.get("targetGroupArn") for tg in inv.get("targetGroups", [])}
    for lb in svc.get("loadBalancers", []):
        if lb.get("targetGroupArn") and lb["targetGroupArn"] not in collected_tgs:
            F.append(not_covered("targetGroups missing", [lb["targetGroupArn"]]))

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
            if type(pm["containerPort"]) is not int or not 1 <= pm["containerPort"] <= 65535:
                F.append(not_covered("containerDefinitions[].portMappings[].containerPort", ["invalid port; expected an integer from 1 to 65535"]))
                continue
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
            F.append(not_covered("multiple container ports", [str(own)]))
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
    argv = list(ing.get("entryPoint") or []) + list(ing.get("command") or [])
    if argv:
        ev = [f"containerDefinitions[{ing.get('name')}].entryPoint={ing.get('entryPoint')} command={ing.get('command')}"]
        if any(a == "<redacted>" or str(a).endswith("=<redacted>") for a in argv):
            F.append(not_covered("containerDefinitions[].command contains <redacted>", ev))
        else:
            F.append(finding(R["container.command"], ev,
                             value={"command": ing.get("entryPoint") or [], "args": ing.get("command") or []}))
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
        bounds = {int(k): v for k, v in R["resources.cpu-memory"]["constraints"]["memory_mib_by_cpu"].items()}
        tier = next((t for t in sorted(bounds) if t >= cr_cpu and bounds[t][0] <= mib <= bounds[t][1]), None)
        if tier is None:
            F.append(finding(R["resources.unsupported-pair"], ev, subject=f"{vcpu:g} vCPU / {mib} MiB"))
        else:
            if tier != vcpu:
                ev.append(f"Cloud Run cpu={tier} chosen for {vcpu:g} vCPU / {mib} MiB")
            if vcpu < 1:
                # This table starts at 1 vCPU, so every sub-vCPU task is silently rounded up.
                ev.append(f"raises the allocation {tier / vcpu:g}x: the task is {vcpu:g} vCPU and this table's "
                          "smallest tier is 1. Cloud Run also offers sub-vCPU allocations with their own "
                          "concurrency and feature constraints -- verify current limits against measured "
                          "utilization before accepting cpu=1")
            F.append(finding(R["resources.cpu-memory"], ev, value={"cpu": str(tier), "memory": f"{mib}Mi"}))

    # Health: a target group is HTTP only if it carries HTTP traffic AND has an HTTP health-check path.
    # An NLB passthrough group (protocol TCP) with an HTTP health check is still non-HTTP traffic.
    # A missing traffic protocol was not collected and is never assumed HTTP.
    probed = False
    for tg in inv.get("targetGroups", []):
        proto = str(tg["protocol"]).upper() if tg.get("protocol") else "<missing>"
        hc = str(tg.get("healthCheckProtocol") or "").upper()
        path = tg.get("healthCheckPath")
        ev = f"targetGroups[{tg.get('targetGroupArn')}].protocol={proto} healthCheckProtocol={hc} healthCheckPath={path}"
        if proto in ("HTTP", "HTTPS") and hc in ("HTTP", "HTTPS") and path:
            if not probed:
                F.append(finding(R["health.http-probe"], [f"targetGroups[].healthCheckPath={path}"], value=path))
                probed = True
        elif proto not in ("HTTP", "HTTPS"):
            F.append(not_covered(f"targetGroups[].protocol={proto}", [ev]))
        else:
            F.append(not_covered(f"targetGroups[].healthCheckProtocol={hc}", [ev]))
    for c in cds:
        if c.get("healthCheck"):
            F.append(finding(R["health.command-probe"], [f"containerDefinitions[{c.get('name')}].healthCheck.command={c['healthCheck'].get('command')}"], subject=c.get("name", "")))

    # Env and secrets on the ingress container
    safe_env = []
    for e in ing.get("environment", []):
        if "<redacted>" in str(e.get("value", "")):
            F.append(not_covered(f"environment.{e['name']}", ["value withheld; restore a reviewed non-secret value or configure a Secret Manager reference"]))
        else:
            safe_env.append(e)
    if safe_env:
        F.append(finding(R["config.env"], [f"containerDefinitions[{ing.get('name')}].environment ({len(safe_env)} reviewed values)"], value=safe_env))
    if ing.get("secrets"):
        names = secret_mappings(ing["secrets"])
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


def finding_key(f):
    """The identity a resolution addresses, printed identically by the summary and the generator."""
    return f["rule"] + (":" + f["subject"] if f.get("subject") else "")


def load_resolutions(path):
    with open(path) as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise SystemExit(f"{path}: expected an object mapping finding key -> resolution")
    for k, v in doc.items():
        if not isinstance(v, dict) or not str(v.get("decision", "")).strip():
            raise SystemExit(f"{path}: resolution for {k!r} needs a non-empty \"decision\"")
    return doc


def apply_resolutions(findings, resolutions):
    """Mark adjudicated findings supported, preserving what they were and why they were cleared.

    An unmatched key is an error: it means the inventory changed under a resolution written for an
    earlier run, and silently ignoring it would let a stale adjudication clear nothing while looking
    like it cleared something."""
    used = set()
    for f in findings:
        k = finding_key(f)
        if f["verdict"] == "supported" or k not in resolutions:
            continue
        r = resolutions[k]
        used.add(k)
        f["original_verdict"] = f["verdict"]
        f["verdict"] = "supported"
        f["resolved"] = {"decision": r["decision"], "evidence": list(r.get("evidence", []))}
    unmatched = sorted(set(resolutions) - used)
    if unmatched:
        raise SystemExit("resolution keys match no open finding (re-check against the current assessment): "
                         + ", ".join(unmatched))
    return findings


def rollup(findings):
    if not findings:
        return "blocked"
    return max((f["verdict"] for f in findings), key=ORDER.__getitem__)


def inventory_hash(inv):
    return hashlib.sha256(json.dumps(inv, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def summary(findings, roll):
    label = "candidate-for-validation" if roll == "supported" else roll
    out = [f"RESULT: {label}", "Field mappings are not proof of runtime compatibility or production readiness.", ""]
    resolved = [f for f in findings if f.get("resolved")]
    if resolved:
        out.append(f"== resolved by decision ({len(resolved)}) -- adjudicated, not re-checked")
        for f in resolved:
            out.append(f"- {finding_key(f)} (was {f['original_verdict']}): {f['resolved']['decision']}")
            for e in f["resolved"]["evidence"]:
                out.append(f"    evidence: {e}")
        out.append("")
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
    ap.add_argument("--resolutions", help="JSON mapping finding key -> {decision, evidence[]} for findings "
                                          "adjudicated by a human; see the module docstring")
    ap.add_argument("--out", default="assessment.json")
    a = ap.parse_args()
    with open(a.inventory) as fh:
        inv = json.load(fh)
    with open(a.rules) as fh:
        rulesdoc = json.load(fh)
    findings = assess(inv, a.src, rulesdoc)
    if a.resolutions:
        findings = apply_resolutions(findings, load_resolutions(a.resolutions))
    roll = rollup(findings)
    with open(a.out, "w") as fh:
        json.dump({"meta": inv.get("meta", {}), "rollup": roll, "findings": findings,
                   "inventory_sha256": inventory_hash(inv),
                   "readiness": "candidate-for-validation" if roll == "supported" else roll,
                   "coverage": inv.get("coverage", {}),
                   "resolved": [finding_key(f) for f in findings if f.get("resolved")],
                   "limitations": ["SDK scan is heuristic; no matches do not prove absence of dependencies.",
                                   "Runtime behavior, concurrency, background work, networking, and data consistency require validation."]}, fh, indent=2)
    print(summary(findings, roll))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
