#!/usr/bin/env python3
"""Generate service.yaml and deploy.sh from assessment.json + inventory.json. Standard library only.

Usage:
  generate.py --inventory inventory.json --assessment assessment.json --project P --region R [--out-dir out]

Refuses (exit 2) when findings are unresolved or secret versions are unspecified. A finding that cannot
become supported by collecting more evidence is cleared through assess.py --resolutions, which records the
decision in the assessment; it is never cleared by editing findings by hand.
Produces configuration for validation, not certification of production readiness. No network calls.
"""
import argparse
import json
import os
import re
import shlex
from assess import inventory_hash

REPO = "fargate-to-cloudrun"
DOCS = {
    "apis": "https://docs.cloud.google.com/run/docs/setup",
    "registry": "https://docs.cloud.google.com/run/docs/deploying",
    "identity": "https://docs.cloud.google.com/run/docs/configuring/services/service-identity",
    "secrets": "https://docs.cloud.google.com/run/docs/configuring/services/secrets",
    "deploy": "https://docs.cloud.google.com/run/docs/deploying",
    "invoke": "https://docs.cloud.google.com/run/docs/authenticating/developers",
    "logs": "https://docs.cloud.google.com/run/docs/logging",
}


def q(s):
    """YAML double-quoted scalar."""
    return json.dumps(str(s), ensure_ascii=False)


def names(service, project, region, src_image):
    """Derive Cloud Run / IAM / Artifact Registry identifiers from the ECS service name and image."""
    slug = re.sub(r"[^a-z0-9-]+", "-", service.lower()).strip("-")[:49]
    sa_id = (slug + "-run")[:30].rstrip("-")
    if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", slug) or len(sa_id) < 6:
        raise SystemExit(f"cannot derive a Cloud Run service name from {service!r}")
    if src_image.split("/")[0] == "public.ecr.aws":
        raise SystemExit("refusing to generate: ECR Public images cannot be pulled by Cloud Run; copy the image to Artifact Registry first")
    m = re.search(r"^([0-9]{12}\.dkr\.ecr(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?)/", src_image)
    is_ecr = bool(m)
    return {
        "slug": slug, "sa_id": sa_id, "sa_email": f"{sa_id}@{project}.iam.gserviceaccount.com",
        "image": f"{region}-docker.pkg.dev/{project}/{REPO}/{slug}:migrated" if is_ecr else src_image,
        "is_ecr": is_ecr, "ecr_host": m.group(1) if m else None, "ecr_region": m.group(2) if m else None,
    }


def generate(assessment, inv, project, region, out_dir="out", secret_versions=None):
    if assessment.get("inventory_sha256") and assessment["inventory_sha256"] != inventory_hash(inv):
        raise SystemExit("assessment belongs to a different inventory; re-run assessment")
    unresolved = [f for f in assessment["findings"] if f["verdict"] != "supported"]
    if assessment["rollup"] != "supported" or unresolved or not assessment["findings"]:
        raise SystemExit("refusing executable generation: resolve findings first: " +
                         ", ".join(f["rule"] + (":" + f.get("subject", "") if f.get("subject") else "") for f in unresolved))
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise SystemExit(f"invalid Google Cloud project id: {project!r}")
    if not re.fullmatch(r"[a-z]+-[a-z]+\d", region):
        raise SystemExit(f"invalid Cloud Run region: {region!r}")
    sup = {f["rule"]: f for f in assessment["findings"] if f["verdict"] == "supported"}
    service = inv["meta"]["service"]
    src_image = sup.get("container.image", {}).get("value")
    if not src_image:
        raise SystemExit("refusing to generate: container.image is not a supported finding with a value")
    n = names(service, project, region, src_image)
    slug, sa_id, sa, image = n["slug"], n["sa_id"], n["sa_email"], n["image"]
    port = sup["container.port"]["value"] if "container.port" in sup else 8080
    env = sup.get("config.env", {}).get("value", [])
    secrets = sup.get("secrets.env", {}).get("value", [])
    if any("<redacted>" in str(e.get("value", "")) for e in env):
        raise SystemExit("refusing to deploy redacted environment values")
    secret_versions = secret_versions or {}
    for s in secrets:
        version = str(secret_versions.get(s["secret"], ""))
        if not re.fullmatch(r"[1-9][0-9]*", version):
            raise SystemExit("provide --secret-versions with a verified numeric version for " + s["secret"])
    health_path = sup["health.http-probe"]["value"] if "health.http-probe" in sup else "/"
    proj = f"--project={project}"

    y = ["apiVersion: serving.knative.dev/v1", "kind: Service", "metadata:",
         f"  name: {q(slug)}  # ecs: service={service}", "spec:", "  template:"]
    if "scaling.min-instances" in sup:
        f = sup["scaling.min-instances"]
        y += ["    metadata:", "      annotations:",
              f"        autoscaling.knative.dev/minScale: {q(f['value'])}  # ecs: service.desiredCount [scaling.min-instances] {f['explain']}"]
    y += ["    spec:", f"      serviceAccountName: {sa}  # created in deploy.sh; the runtime identity"]
    if "timeout.request" in sup:
        f = sup["timeout.request"]
        y.append(f"      timeoutSeconds: {f['value']}  # Cloud Run documented default [timeout.request] {f['quote']}")
    y += ["      containers:", f"      - image: {q(image)}  # ecs: containerDefinitions[].image={src_image} [container.image]"]
    if "container.port" in sup:
        y += ["        ports:", f"        - containerPort: {port}  # ecs: portMappings[].containerPort [container.port]"]
    if "container.command" in sup:
        v = sup["container.command"]["value"]
        if v["command"]:
            y.append(f"        command: {json.dumps(v['command'], ensure_ascii=False)}  # ecs: entryPoint [container.command]")
        if v["args"]:
            y.append(f"        args: {json.dumps(v['args'], ensure_ascii=False)}  # ecs: command [container.command]")
    if "resources.cpu-memory" in sup:
        v = sup["resources.cpu-memory"]["value"]
        ev = sup["resources.cpu-memory"]["evidence"]
        y += ["        resources:", "          limits:",
              *[f"          # {e}" for e in ev[1:]],
              f"            cpu: {q(v['cpu'])}  # ecs: {ev[0]} [resources.cpu-memory]",
              f"            memory: {v['memory']}"]
    if env or secrets:
        y.append("        env:")
        for e in env:
            y += [f"        - name: {q(e['name'])}", f"          value: {q(e.get('value', ''))}  # ecs: environment [config.env]"]
        for s in secrets:
            y += [f"        - name: {q(s['name'])}", "          valueFrom:", "            secretKeyRef:",
                  f"              name: {q(s['secret'])}", f"              key: {q(secret_versions[s['secret']])}  # pinned Secret Manager version"]
    if "health.http-probe" in sup:
        y += ["        startupProbe:",
              "          # Cloud Run probe timing defaults apply (periodSeconds 10, failureThreshold 3, timeoutSeconds 1); the v1 inventory carries no ECS health-check timing",
              "          httpGet:",
              f"            path: {q(health_path)}  # ecs: targetGroups[].healthCheckPath [health.http-probe]",
              f"            port: {port}"]
    yaml_text = "\n".join(y) + "\n"

    steps = []

    def step(purpose, explain, url, *cmds):
        steps.append((purpose, explain, url, list(cmds)))

    step("Enable the Cloud Run, Artifact Registry and Secret Manager APIs",
         "Google Cloud switches services on per project; nothing below works until these are enabled. Enabling is free.",
         DOCS["apis"],
         f"gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com {proj}")
    if n["is_ecr"]:
        ecr_host, ecr_region = n["ecr_host"], n["ecr_region"]
        step("Create the Artifact Registry repository if it does not exist",
             "Artifact Registry is Google Cloud's container registry; Cloud Run pulls images from it. Storage is billed per GiB.",
             DOCS["registry"],
             f'gcloud artifacts repositories describe {REPO} --location={region} {proj} >/dev/null 2>&1 || gcloud artifacts repositories create {REPO} --repository-format=docker --location={region} {proj}')
        step("Log Docker in to ECR (read-only on AWS: this only obtains a pull token)",
             "The image is pulled from your existing ECR repository; nothing on AWS is changed.",
             DOCS["registry"],
             f"aws ecr get-login-password --region {ecr_region} | docker login --username AWS --password-stdin {ecr_host}")
        step("Let Docker push to Artifact Registry",
             "This edits only your local Docker config so docker push can authenticate to Google Cloud.",
             DOCS["registry"],
             f"gcloud auth configure-docker {region}-docker.pkg.dev --quiet {proj}")
        step("Copy the image from ECR to Artifact Registry",
             "Cloud Run needs the image in Artifact Registry. The image is copied, not rebuilt.",
             DOCS["registry"],
             f"docker pull --platform linux/amd64 {shlex.quote(src_image)}", f"docker tag {shlex.quote(src_image)} {image}", f"docker push {image}")
    step("Create the runtime service account if it does not exist",
         "Every Cloud Run service runs as a Google service account, its identity when calling Google APIs. It carries no AWS credentials.",
         DOCS["identity"],
         f'gcloud iam service-accounts describe {sa} {proj} >/dev/null 2>&1 || gcloud iam service-accounts create {sa_id} --display-name={shlex.quote(service + " on Cloud Run")} {proj}')
    if secrets:
        secret_ids = " ".join(shlex.quote(s["secret"]) for s in secrets)
        step("Grant the runtime service account access to each secret",
             "The docs: to allow Cloud Run to access the secret, the service identity must have the Secret Manager Secret Accessor role.",
             DOCS["secrets"],
             f'for s in {secret_ids}; do gcloud secrets add-iam-policy-binding "$s" --member=serviceAccount:{sa} --role=roles/secretmanager.secretAccessor {proj} >/dev/null; done')
        step("Verify every pinned secret version is enabled",
             "Import the approved secrets before deployment, then regenerate with --secret-versions. This check reads metadata only.",
             DOCS["secrets"],
             *[f'test "$(gcloud secrets versions describe {secret_versions[s["secret"]]} --secret={shlex.quote(s["secret"])} {proj} --format=\'value(state)\')" = ENABLED' for s in secrets])
    step("Deploy the service from service.yaml",
         "gcloud run services replace applies the manifest; the first run creates the service, later runs create a new revision.",
         DOCS["deploy"],
         f"gcloud run services replace {shlex.quote(os.path.join(out_dir, 'service.yaml'))} --region={region} {proj}")
    step("Fetch the service URL",
         "Every Cloud Run service gets a stable HTTPS URL on run.app.",
         DOCS["deploy"],
         f"gcloud run services describe {slug} --region={region} {proj} --format='value(status.url)'")
    step("Smoke test with your own identity token",
         "The caller needs Cloud Run Invoker. This checks HTTP reachability only; run application tests before cutover.",
         DOCS["invoke"],
         f'curl -sf --retry 5 --retry-delay 3 -H "Authorization: Bearer $(gcloud auth print-identity-token)" '
         f'"$(gcloud run services describe {slug} --region={region} {proj} --format=\'value(status.url)\')"{shlex.quote(health_path)} '
         '>/dev/null && echo "SMOKE OK" || { echo "SMOKE FAILED"; exit 1; }')
    step("Show the last 20 log lines",
         "Cloud Run captures stdout and stderr as logs automatically; no agent to install.",
         DOCS["logs"],
         f"gcloud run services logs read {slug} --region={region} {proj} --limit=20")

    sh = ["#!/usr/bin/env bash",
          "# Generated by fargate-to-cloudrun. Read every step before running it.",
          "# Every step is self-contained (its own --project and --region, no shared variables), so the agent",
          "# can run them one at a time. Use set -euo pipefail in each shell; review the project and existing service first.",
          "set -euo pipefail",
          ""]
    for i, (purpose, explain, url, cmds) in enumerate(steps, 1):
        sh += [f"# ---- Step {i}: {purpose}", f"# {explain}", f"# Docs: {url}"] + cmds + [""]
    return yaml_text, "\n".join(sh)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assessment", required=True)
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--secret-versions", help="JSON mapping destination secret ids to verified numeric versions")
    a = ap.parse_args()
    with open(a.assessment) as fh:
        assessment = json.load(fh)
    with open(a.inventory) as fh:
        inv = json.load(fh)
    try:
        versions = None
        if a.secret_versions:
            with open(a.secret_versions) as fh:
                versions = json.load(fh)
        yaml_text, sh_text = generate(assessment, inv, a.project, a.region, a.out_dir, versions)
    except SystemExit as e:
        print(e)
        raise SystemExit(2)
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "service.yaml"), "w", encoding="utf-8") as fh:
        fh.write(yaml_text)
    with open(os.path.join(a.out_dir, "deploy.sh"), "w", encoding="utf-8") as fh:
        fh.write(sh_text)
    print(f"wrote {a.out_dir}/service.yaml and {a.out_dir}/deploy.sh")


if __name__ == "__main__":
    main()
