#!/usr/bin/env python3
"""Generate service.yaml and deploy.sh from assessment.json + inventory.json. Standard library only.

Usage:
  generate.py --assessment assessment.json --inventory inventory.json --project P --region R [--out-dir out]

Refuses (exit 2) on a blocked rollup. Emits manifest values only for findings whose verdict is
supported; every other finding becomes an '# OMITTED:' line. No network calls.
"""
import argparse
import json
import os

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
    return json.dumps(str(s))


def generate(assessment, inv, project, region):
    if assessment["rollup"] == "blocked":
        raise SystemExit("refusing to generate: rollup is blocked")
    sup = {f["rule"]: f for f in assessment["findings"] if f["verdict"] == "supported"}
    omitted = [f for f in assessment["findings"] if f["verdict"] != "supported"]
    service = inv["meta"]["service"]
    sa_id = f"{service}-run"[:30]
    sa = f"{sa_id}@{project}.iam.gserviceaccount.com"
    src_image = sup["container.image"]["value"]
    is_ecr = ".dkr.ecr." in src_image
    image = f"{region}-docker.pkg.dev/{project}/{REPO}/{service}:migrated" if is_ecr else src_image
    port = sup["container.port"]["value"] if "container.port" in sup else 8080
    env = sup.get("config.env", {}).get("value", [])
    secrets = sup.get("secrets.env", {}).get("value", [])
    health_path = sup["health.http-probe"]["value"] if "health.http-probe" in sup else "/"

    y = ["apiVersion: serving.knative.dev/v1", "kind: Service", "metadata:", f"  name: {service}",
         "spec:", "  template:", "    metadata:", "      annotations:"]
    if "scaling.min-instances" in sup:
        f = sup["scaling.min-instances"]
        y.append(f"        autoscaling.knative.dev/minScale: {q(f['value'])}  # ecs: service.desiredCount [scaling.min-instances] {f['explain']}")
    y += ["    spec:", f"      serviceAccountName: {sa}  # created in deploy.sh; the runtime identity"]
    if "timeout.request" in sup:
        f = sup["timeout.request"]
        y.append(f"      timeoutSeconds: {f['value']}  # Cloud Run documented default [timeout.request] {f['quote']}")
    y += ["      containers:", f"      - image: {image}  # ecs: containerDefinitions[].image={src_image} [container.image]"]
    if "container.port" in sup:
        y += ["        ports:", f"        - containerPort: {port}  # ecs: portMappings[].containerPort [container.port]"]
    if "container.command" in sup:
        v = sup["container.command"]["value"]
        if v["command"]:
            y.append(f"        command: {json.dumps(v['command'])}  # ecs: entryPoint [container.command]")
        if v["args"]:
            y.append(f"        args: {json.dumps(v['args'])}  # ecs: command [container.command]")
    if "resources.cpu-memory" in sup:
        v = sup["resources.cpu-memory"]["value"]
        ev = sup["resources.cpu-memory"]["evidence"][0]
        y += ["        resources:", "          limits:",
              f"            cpu: {q(v['cpu'])}  # ecs: {ev} [resources.cpu-memory]",
              f"            memory: {v['memory']}"]
    if env or secrets:
        y.append("        env:")
        for e in env:
            y += [f"        - name: {e['name']}", f"          value: {q(e.get('value', ''))}  # ecs: environment [config.env]"]
        for s in secrets:
            y += [f"        - name: {s['name']}", "          valueFrom:", "            secretKeyRef:",
                  f"              name: {s['secret']}", '              key: "1"  # ecs: secrets [secrets.env] pinned to version 1']
    if "health.http-probe" in sup:
        y += ["        startupProbe:", "          httpGet:",
              f"            path: {health_path}  # ecs: targetGroups[].healthCheckPath [health.http-probe]",
              f"            port: {port}"]
    for f in omitted:
        y.append(f"# OMITTED: {f.get('subject') or f['rule']} — see finding {f['rule']}")
    yaml_text = "\n".join(y) + "\n"

    steps = []

    def step(purpose, explain, url, *cmds):
        steps.append((purpose, explain, url, list(cmds)))

    step("Enable the Cloud Run, Artifact Registry and Secret Manager APIs",
         "Google Cloud switches services on per project; nothing below works until these are enabled. Enabling is free.",
         DOCS["apis"],
         "gcloud services enable run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com")
    if is_ecr:
        ecr_host = src_image.split("/")[0]
        ecr_region = ecr_host.split(".")[3]
        step("Create the Artifact Registry repository if it does not exist",
             "Artifact Registry is Google Cloud's container registry; Cloud Run pulls images from it. Storage is billed per GiB.",
             DOCS["registry"],
             f'gcloud artifacts repositories describe {REPO} --location="$REGION" >/dev/null 2>&1 || gcloud artifacts repositories create {REPO} --repository-format=docker --location="$REGION"')
        step("Log Docker in to ECR (read-only on AWS: this only obtains a pull token)",
             "The image is pulled from your existing ECR repository; nothing on AWS is changed.",
             DOCS["registry"],
             f"aws ecr get-login-password --region {ecr_region} | docker login --username AWS --password-stdin {ecr_host}")
        step("Let Docker push to Artifact Registry",
             "This edits only your local Docker config so docker push can authenticate to Google Cloud.",
             DOCS["registry"],
             'gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet')
        step("Copy the image from ECR to Artifact Registry",
             "Cloud Run needs the image in Artifact Registry. The image is copied, not rebuilt.",
             DOCS["registry"],
             f"docker pull {src_image}", f"docker tag {src_image} {image}", f"docker push {image}")
    step("Create the runtime service account if it does not exist",
         "Every Cloud Run service runs as a Google service account, its identity when calling Google APIs. It carries no AWS credentials.",
         DOCS["identity"],
         f'gcloud iam service-accounts describe "{sa}" >/dev/null 2>&1 || gcloud iam service-accounts create {sa_id} --display-name="{service} on Cloud Run"')
    if secrets:
        names = " ".join(s["secret"] for s in secrets)
        step("Create each Secret Manager secret (empty) if it does not exist",
             "Secret Manager replaces AWS Secrets Manager and SSM. Values are added by you in a later step, never by this script.",
             DOCS["secrets"],
             f'for s in {names}; do gcloud secrets describe "$s" >/dev/null 2>&1 || gcloud secrets create "$s" --replication-policy=automatic; done')
        step("Grant the runtime service account access to each secret",
             "The docs: to allow Cloud Run to access the secret, the service identity must have the Secret Manager Secret Accessor role.",
             DOCS["secrets"],
             f'for s in {names}; do gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:{sa}" --role=roles/secretmanager.secretAccessor >/dev/null; done')
        step("PAUSE: add a value to every secret yourself, then continue",
             "The manifest pins each secret to version 1. In another terminal run each command below, type the value, then press Ctrl-D. This script never sees the values.",
             DOCS["secrets"],
             *[f'echo "  gcloud secrets versions add {s["secret"]} --data-file=-"' for s in secrets],
             'read -r -p "Press Enter once every secret above has a version... "')
    step("Deploy the service from service.yaml",
         "gcloud run services replace applies the manifest; the first run creates the service, later runs create a new revision.",
         DOCS["deploy"],
         'gcloud run services replace service.yaml --region="$REGION"')
    step("Fetch the service URL",
         "Every Cloud Run service gets a stable HTTPS URL on run.app.",
         DOCS["deploy"],
         f"URL=\"$(gcloud run services describe {service} --region=\"$REGION\" --format='value(status.url)')\"",
         'echo "$URL"')
    step("Smoke test with your own identity token (the service stays private)",
         "Callers need the Cloud Run Invoker role; as project owner you already have it. No public access is granted here.",
         DOCS["invoke"],
         f'curl -sf --retry 5 --retry-delay 3 -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL{health_path}" >/dev/null && echo "SMOKE OK" || {{ echo "SMOKE FAILED"; exit 1; }}')
    step("Show the last 20 log lines",
         "Cloud Run captures stdout and stderr as logs automatically; no agent to install.",
         DOCS["logs"],
         f'gcloud run services logs read {service} --region="$REGION" --limit=20')

    sh = ["#!/usr/bin/env bash",
          "# Generated by fargate-to-cloudrun. Read every step before running it.",
          "# The agent runs these one step at a time and asks before each. You can also run the whole file.",
          "set -euo pipefail",
          f'export CLOUDSDK_CORE_PROJECT="{project}"',
          f'REGION="{region}"',
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
    a = ap.parse_args()
    assessment = json.load(open(a.assessment))
    inv = json.load(open(a.inventory))
    try:
        yaml_text, sh_text = generate(assessment, inv, a.project, a.region)
    except SystemExit as e:
        print(e)
        raise SystemExit(2)
    os.makedirs(a.out_dir, exist_ok=True)
    open(os.path.join(a.out_dir, "service.yaml"), "w").write(yaml_text)
    open(os.path.join(a.out_dir, "deploy.sh"), "w").write(sh_text)
    print(f"wrote {a.out_dir}/service.yaml and {a.out_dir}/deploy.sh")


if __name__ == "__main__":
    main()
