# fargate-to-cloudrun Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent skill that assesses one ECS/Fargate service for Cloud Run with every verdict cited to the Cloud Run docs, then guides a private staging deploy.

**Architecture:** A standard `SKILL.md` drives a guided workflow in Claude Code, Gemini CLI, and Codex. Three standard-library Python scripts do the deterministic work: `inventory.py` (read-only aws CLI → JSON), `assess.py` (rules from `references/rules.json` + source scan → findings), `generate.py` (findings → `service.yaml` + `deploy.sh`). A fourth, `docdrift.py`, is a maintainer tool that checks every quoted sentence still exists on its cited page. Six synthetic fixtures and one `unittest` file enforce no false passes.

**Tech Stack:** Python 3.9+ standard library only (`json`, `re`, `subprocess`, `urllib`, `unittest`). Bash for the generated deploy script. No pip, no pytest, no YAML library.

**Spec:** `docs/superpowers/specs/2026-09-16-fargate-to-cloudrun-design.md`. Read it first. Two small deviations from the spec are made here and should be reflected back into the spec at the end (Task 11): the ECR-specific deploy steps (repo create, ECR login, Docker auth, copy) are all skipped when the image is not in ECR, not just two of them; and listener rules are not collected in v1 because no rule consumes them.

**Source of truth after review rounds:** code review of each task may change files beyond what the plan's code blocks show. Once a task has passed both reviews, the file on disk is authoritative and the plan's block for it is historical. Later tasks build on disk, not on the plan's copy.

**Repo:** `/Users/robertozuniga/Desktop/code/fargate-to-cloudrun` (already a git repo with the spec committed). All paths below are relative to it. Run all commands from the repo root.

**Docs corpus (for the implementer, not shipped):** the 313 Cloud Run pages as markdown live at `/private/tmp/claude-501/-Users-robertozuniga-Desktop-code-blm-infra-platform/25ba96c8-074e-4f6d-b426-358bd88fa6ca/scratchpad/cloudrun/md/`. Every quote in `rules.json` below was copied from there on the 2026-09-12 snapshot. If you need to add a rule, take the quote from that folder, never from memory.

---

## File structure

| File | Responsibility |
|---|---|
| `SKILL.md` | Trigger, invariants, the five phases, guided-mode rule, preflight paragraphs, fixture replay line |
| `references/rules.json` | Single source of truth (18 rules): every mapping row and blocker rule with url + quote + snapshot + stale, plus the `ignore` list of ECS fields with no Cloud Run consequence |
| `scripts/inventory.py` | Read-only aws CLI calls → normalized `inventory.json`; redaction; denied-call recording |
| `scripts/assess.py` | Loads rules, runs one check per rule id, scans source for AWS SDK clients, walks unmatched fields → `assessment.json` |
| `scripts/generate.py` | `assessment.json` + `inventory.json` → `service.yaml` + `deploy.sh`; refuses on blocked; emits supported only |
| `scripts/docdrift.py` | Maintainer: fetch each cited url, confirm quote present, `--write` flips `stale` |
| `fixtures/<name>/inventory.json`, `src/`, `expected.json` | Six synthetic services; `stateless-http/golden/` holds expected generator output |
| `tests/test_assess.py` | Verdict equality, no-false-pass, citation presence, rule/check parity, golden files, pure helpers |
| `.github/workflows/docdrift.yml` | Weekly cron running `docdrift.py`; fails on stale |
| `README.md`, `LICENSE`, `NOTICE` | Public-facing docs, Apache 2.0, CC-BY attribution |

---

### Task 1: Repo scaffold

**Files:**
- Create: `LICENSE`, `NOTICE`, `README.md`, `.gitignore`

- [ ] **Step 1: Fetch the Apache 2.0 license text**

```bash
curl -sL https://www.apache.org/licenses/LICENSE-2.0.txt -o LICENSE
head -3 LICENSE
```
Expected: first line is `                                 Apache License`.

- [ ] **Step 2: Write NOTICE**

```text
fargate-to-cloudrun
Copyright 2026 the fargate-to-cloudrun contributors

Licensed under the Apache License, Version 2.0.

This project adapts and quotes sentences from the Google Cloud Run documentation
(https://docs.cloud.google.com/run/docs), which Google publishes under the
Creative Commons Attribution 4.0 License (https://creativecommons.org/licenses/by/4.0/).
Each quoted sentence in references/rules.json carries the URL of the page it was
taken from and the date of the snapshot. Google Cloud, Cloud Run, and related marks
are trademarks of Google LLC and are not covered by that license; this project is
not affiliated with or endorsed by Google or Amazon.
```

- [ ] **Step 3: Write a README stub and .gitignore**

`README.md`:
```markdown
# fargate-to-cloudrun

Assess one Amazon ECS/Fargate service for Google Cloud Run, with every verdict cited to
the Cloud Run documentation, then guide a private staging deploy.

Status: pre-release. See `docs/superpowers/specs/` for the design.
```

`.gitignore`:
```
__pycache__/
*.pyc
/out/
/inventory.json
/assessment.json
```

- [ ] **Step 4: Commit**

```bash
git add LICENSE NOTICE README.md .gitignore
git commit -m "chore: scaffold repo with Apache 2.0, NOTICE, README stub

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: rules.json

**Files:**
- Create: `references/rules.json`

Every quote below was copied verbatim from the docs corpus snapshot of 2026-09-12, with inline code markup removed so it matches the page text after HTML tags are stripped.

- [ ] **Step 1: Write the file**

```json
{
  "ignore": [
    "taskDefinitionArn", "revision", "status", "registeredAt", "registeredBy", "deregisteredAt",
    "requiresAttributes", "compatibilities", "requiresCompatibilities", "family", "tags",
    "placementConstraints", "networkMode", "executionRoleArn",
    "containerDefinitions[].name", "containerDefinitions[].essential",
    "containerDefinitions[].cpu", "containerDefinitions[].memory", "containerDefinitions[].memoryReservation",
    "containerDefinitions[].logConfiguration", "containerDefinitions[].dockerLabels",
    "containerDefinitions[].startTimeout", "containerDefinitions[].stopTimeout",
    "containerDefinitions[].mountPoints", "containerDefinitions[].volumesFrom",
    "containerDefinitions[].dependsOn", "containerDefinitions[].systemControls"
  ],
  "rules": [
    {
      "id": "container.image",
      "category": "container",
      "ecs_field": "containerDefinitions[].image",
      "cloudrun_field": "spec.template.spec.containers[].image",
      "verdict": "supported",
      "explain": "Cloud Run pulls the container image from Artifact Registry, so an ECR image is copied there first; the image itself is not rebuilt.",
      "url": "https://docs.cloud.google.com/run/docs/deploying",
      "quote": "Google recommends the use of Artifact Registry.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "container.port",
      "category": "container",
      "ecs_field": "containerDefinitions[].portMappings[].containerPort",
      "cloudrun_field": "spec.template.spec.containers[].ports[].containerPort",
      "verdict": "supported",
      "explain": "Cloud Run sends requests to one port on the container and injects it as the PORT environment variable; the ECS container port becomes that port.",
      "url": "https://docs.cloud.google.com/run/docs/container-contract",
      "quote": "By default, requests are sent to 8080, but you can configure Cloud Run to send requests to the port of your choice.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "container.command",
      "category": "container",
      "ecs_field": "containerDefinitions[].entryPoint, containerDefinitions[].command",
      "cloudrun_field": "spec.template.spec.containers[].command, .args",
      "verdict": "supported",
      "explain": "The ECS entry point and command become the Cloud Run command and args, overriding the image defaults the same way.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/containers",
      "quote": "If you want to override the image's default entrypoint and command arguments, you can use the command and args fields in the container configuration.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "container.multiple",
      "category": "container",
      "ecs_field": "containerDefinitions[] (more than one)",
      "cloudrun_field": "spec.template.spec.containers[] (sidecars)",
      "verdict": "needs-investigation",
      "explain": "Cloud Run can run sidecar containers, but only one receives traffic and start order must be declared; confirm each extra container is still needed on Cloud Run.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/containers",
      "quote": "To specify container start up order in a sidecar deployment, you use the container dependencies feature.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "container.architecture",
      "category": "container",
      "ecs_field": "runtimePlatform.cpuArchitecture, runtimePlatform.operatingSystemFamily",
      "cloudrun_field": "(none)",
      "verdict": "blocked",
      "explain": "Cloud Run runs Linux x86_64 containers only; an ARM64 (Graviton) or Windows image must be rebuilt for x86_64 Linux before it can run.",
      "url": "https://docs.cloud.google.com/run/docs/container-contract",
      "quote": "Cloud Run specifically supports the Linux x86_64 ABI format.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "resources.cpu-memory",
      "category": "resources",
      "ecs_field": "cpu, memory (task level)",
      "cloudrun_field": "spec.template.spec.containers[].resources.limits",
      "verdict": "supported",
      "explain": "Cloud Run sets CPU and memory per instance; the Fargate task size maps to a documented Cloud Run pair, rounding CPU up to at least 1 vCPU so the sub-1-vCPU constraints never apply.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/cpu",
      "quote": "A minimum of 0.5 vCPU is needed to set a memory limit greater than 512MiB.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "resources.unsupported-pair",
      "category": "resources",
      "ecs_field": "cpu, memory (task level)",
      "cloudrun_field": "spec.template.spec.containers[].resources.limits",
      "verdict": "blocked",
      "explain": "This CPU and memory pair is outside the combinations the Cloud Run documentation lists.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/memory-limits",
      "quote": "Cloud Run instances that exceed their allowed memory limit are terminated.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "health.http-probe",
      "category": "health",
      "ecs_field": "targetGroups[].healthCheckPath (HTTP/HTTPS)",
      "cloudrun_field": "spec.template.spec.containers[].startupProbe.httpGet",
      "verdict": "supported",
      "explain": "The load balancer's HTTP health check path becomes a Cloud Run startup probe on the same path.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/healthchecks",
      "quote": "Startup probes determine whether the container has started and is ready to accept traffic.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "health.command-probe",
      "category": "health",
      "ecs_field": "containerDefinitions[].healthCheck.command",
      "cloudrun_field": "(none)",
      "verdict": "needs-investigation",
      "explain": "ECS runs a shell command inside the container as its health check; Cloud Run probes are HTTP, TCP, or gRPC only, so the check must be re-expressed as an endpoint.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/healthchecks",
      "quote": "You can configure HTTP, TCP, and gRPC probes using Google Cloud console, YAML, or Terraform:",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "config.env",
      "category": "config",
      "ecs_field": "containerDefinitions[].environment",
      "cloudrun_field": "spec.template.spec.containers[].env[].value",
      "verdict": "supported",
      "explain": "Plain environment variables carry over unchanged.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/environment-variables",
      "quote": "You can set a maximum of 1000 environment variables for a Cloud Run service.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "secrets.env",
      "category": "secrets",
      "ecs_field": "containerDefinitions[].secrets[].valueFrom",
      "cloudrun_field": "spec.template.spec.containers[].env[].valueFrom.secretKeyRef",
      "verdict": "supported",
      "explain": "AWS Secrets Manager and SSM references become Secret Manager references pinned to a version; the runtime service account needs the Secret Accessor role, and you add the values yourself.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/secrets",
      "quote": "Environment variables are resolved at instance startup time, so if you use this method, Google recommends that you pin the secret to a particular version instead of using latest as the version.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "storage.efs",
      "category": "storage",
      "ecs_field": "volumes[].efsVolumeConfiguration",
      "cloudrun_field": "spec.template.spec.volumes[].nfs",
      "verdict": "needs-investigation",
      "explain": "Cloud Run can mount an NFS share such as Filestore, but without file locking and with a 30-second mount budget at startup; v1 omits the volume because no Filestore exists yet.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/nfs-volume-mounts",
      "quote": "Cloud Run does not support NFS locking. NFS volumes are automatically mounted in no-lock mode.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "network.private-subnets",
      "category": "network",
      "ecs_field": "service.networkConfiguration.awsvpcConfiguration (assignPublicIp DISABLED)",
      "cloudrun_field": "spec.template.metadata.annotations run.googleapis.com/network-interfaces",
      "verdict": "needs-investigation",
      "explain": "The task reached private resources through VPC subnets; Cloud Run does the same with Direct VPC egress, but a Google Cloud VPC holding those resources must exist first, so v1 omits it.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc",
      "quote": "You can enable your Cloud Run service, function, job, or worker pool to send traffic to a VPC network by using Direct VPC egress with no Serverless VPC Access connector required.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "scaling.min-instances",
      "category": "scaling",
      "ecs_field": "service.desiredCount",
      "cloudrun_field": "spec.template.metadata.annotations autoscaling.knative.dev/minScale",
      "verdict": "supported",
      "explain": "Cloud Run scales to zero when idle unless minimum instances are set; the ECS desired count becomes the minimum, and those instances are billed while idle.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/min-instances",
      "quote": "With minimum instances set, Cloud Run keeps at least the number of minimum instances running, even if they're not processing requests.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "timeout.request",
      "category": "timeout",
      "ecs_field": "(none in v1 inventory)",
      "cloudrun_field": "spec.template.spec.timeoutSeconds",
      "verdict": "supported",
      "explain": "Cloud Run closes a request that gets no response within the timeout; the documented default is used because ECS has no equivalent field.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/request-timeout",
      "quote": "The timeout is set by default to 5 minutes (300 seconds) and can be extended up to 60 minutes (3600 seconds).",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "workload.scheduled",
      "category": "workload",
      "ecs_field": "scheduledRules[] with no containerDefinitions[].portMappings",
      "cloudrun_field": "Cloud Run job + Cloud Scheduler",
      "verdict": "blocked",
      "explain": "This task runs on a schedule and exits; on Cloud Run that is a job triggered by Cloud Scheduler, not a service, and v1 only deploys services.",
      "url": "https://docs.cloud.google.com/run/docs/create-jobs",
      "quote": "Unlike a Cloud Run service, which listens for and serves requests, a Cloud Run job only runs its tasks and exits when finished.",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "workload.background",
      "category": "workload",
      "ecs_field": "no containerDefinitions[].portMappings and no scheduledRules[]",
      "cloudrun_field": "Cloud Run worker pool",
      "verdict": "blocked",
      "explain": "This task exposes no port and runs continuously; on Cloud Run that is a worker pool, not a service, and v1 only deploys services.",
      "url": "https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run",
      "quote": "Handles always-on background workloads such as workloads from message queues (Kafka, Pub/Sub, RabbitMQ).",
      "snapshot": "2026-09-12",
      "stale": false
    },
    {
      "id": "deps.aws-service",
      "category": "dependencies",
      "ecs_field": "taskRoleArn policy actions and AWS SDK clients in source",
      "cloudrun_field": "spec.template.spec.serviceAccountName (Google service account)",
      "verdict": "needs-investigation",
      "explain": "Cloud Run runs as a Google service account, which carries no AWS credentials; every AWS service the code still calls needs its own plan before this service can work.",
      "url": "https://docs.cloud.google.com/run/docs/configuring/services/service-identity",
      "quote": "In Cloud Run, the service identity is a service account that is both a resource and a principal.",
      "snapshot": "2026-09-12",
      "stale": false
    }
  ]
}
```

- [ ] **Step 2: Validate it parses and has 18 rules**

```bash
python3 -c "import json; d=json.load(open('references/rules.json')); print(len(d['rules']), len(d['ignore']))"
```
Expected: `18 27`

- [ ] **Step 3: Commit**

```bash
git add references/rules.json
git commit -m "feat: rules.json with 18 cited Cloud Run rules and ECS ignore list

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Fixtures

**Files:**
- Create: `fixtures/<name>/inventory.json`, `fixtures/<name>/src/*`, `fixtures/<name>/expected.json` for six fixtures

All six share this `inventory.json` shape, which `inventory.py` (Task 7) must also produce:

```
meta{account, identity, region, cluster, service, collected}
service{desiredCount, launchType, networkConfiguration, loadBalancers}
taskDefinition{...raw describe-task-definition output, environment values redacted...}
targetGroups[{targetGroupArn, healthCheckProtocol, healthCheckPath, port}]
taskRoleActions[], executionRoleActions[], scheduledRules[{name, scheduleExpression}], denied[{call, error}]
```

`expected.json` shape: `{"rollup": ..., "findings": [{"rule", "verdict", "subject"}]}` sorted by rule then subject.

- [ ] **Step 1: fixtures/stateless-http**

`fixtures/stateless-http/inventory.json`:
```json
{
  "meta": {"account": "123456789012", "identity": "arn:aws:iam::123456789012:user/demo", "region": "us-east-1", "cluster": "demo", "service": "web", "collected": "2026-09-16T00:00:00+00:00"},
  "service": {
    "desiredCount": 2,
    "launchType": "FARGATE",
    "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-aaa", "subnet-bbb"], "securityGroups": ["sg-111"], "assignPublicIp": "ENABLED"}},
    "loadBalancers": [{"targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/web/abc", "containerName": "web", "containerPort": 8080}]
  },
  "taskDefinition": {
    "family": "web",
    "taskRoleArn": "arn:aws:iam::123456789012:role/web-task",
    "executionRoleArn": "arn:aws:iam::123456789012:role/web-exec",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "containerDefinitions": [
      {
        "name": "web",
        "image": "us-docker.pkg.dev/cloudrun/container/hello",
        "essential": true,
        "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
        "environment": [
          {"name": "APP_ENV", "value": "staging"},
          {"name": "API_KEY", "value": "<redacted>"}
        ],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db-url-AbCdEf"}],
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/web"}}
      }
    ]
  },
  "targetGroups": [{"targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/web/abc", "healthCheckProtocol": "HTTP", "healthCheckPath": "/", "port": 8080}],
  "taskRoleActions": [],
  "executionRoleActions": ["ecr:GetAuthorizationToken", "ecr:BatchGetImage", "logs:CreateLogStream", "logs:PutLogEvents", "secretsmanager:GetSecretValue"],
  "scheduledRules": [],
  "denied": []
}
```

`fixtures/stateless-http/src/app.py`:
```python
from http.server import BaseHTTPRequestHandler, HTTPServer
import os

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), H).serve_forever()
```

`fixtures/stateless-http/expected.json`:
```json
{
  "rollup": "supported",
  "findings": [
    {"rule": "config.env", "verdict": "supported", "subject": ""},
    {"rule": "container.image", "verdict": "supported", "subject": ""},
    {"rule": "container.port", "verdict": "supported", "subject": ""},
    {"rule": "health.http-probe", "verdict": "supported", "subject": ""},
    {"rule": "resources.cpu-memory", "verdict": "supported", "subject": ""},
    {"rule": "scaling.min-instances", "verdict": "supported", "subject": ""},
    {"rule": "secrets.env", "verdict": "supported", "subject": ""},
    {"rule": "timeout.request", "verdict": "supported", "subject": ""}
  ]
}
```

- [ ] **Step 2: fixtures/sidecar-datadog**

Copy `stateless-http/inventory.json`, change `meta.service` to `"web-dd"`, remove the `secrets` key from the web container, remove `"secretsmanager:GetSecretValue"` from `executionRoleActions`, and add a second container:
```json
{
  "name": "datadog-agent",
  "image": "public.ecr.aws/datadog/agent:7",
  "essential": false,
  "environment": [{"name": "DD_API_KEY", "value": "<redacted>"}],
  "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/web"}}
}
```
Copy `src/app.py` unchanged.

`expected.json`:
```json
{
  "rollup": "needs-investigation",
  "findings": [
    {"rule": "config.env", "verdict": "supported", "subject": ""},
    {"rule": "container.image", "verdict": "supported", "subject": ""},
    {"rule": "container.multiple", "verdict": "needs-investigation", "subject": "datadog-agent"},
    {"rule": "container.port", "verdict": "supported", "subject": ""},
    {"rule": "health.http-probe", "verdict": "supported", "subject": ""},
    {"rule": "resources.cpu-memory", "verdict": "supported", "subject": ""},
    {"rule": "scaling.min-instances", "verdict": "supported", "subject": ""},
    {"rule": "timeout.request", "verdict": "supported", "subject": ""}
  ]
}
```

- [ ] **Step 3: fixtures/efs-mount**

Copy `stateless-http/inventory.json`, set `meta.service` to `"web-efs"`, remove `secrets` and `"secretsmanager:GetSecretValue"` from `executionRoleActions`, add to `taskDefinition`:
```json
"volumes": [{"name": "data", "efsVolumeConfiguration": {"fileSystemId": "fs-0123456789abcdef0", "rootDirectory": "/"}}]
```
and to the web container: `"mountPoints": [{"sourceVolume": "data", "containerPath": "/data"}]`. Copy `src/app.py`.

`expected.json`: same as stateless-http minus `secrets.env`, plus
`{"rule": "storage.efs", "verdict": "needs-investigation", "subject": "data"}`, rollup `needs-investigation`. Keep the list sorted by rule.

- [ ] **Step 4: fixtures/sqs-worker**

`inventory.json`:
```json
{
  "meta": {"account": "123456789012", "identity": "arn:aws:iam::123456789012:user/demo", "region": "us-east-1", "cluster": "demo", "service": "worker", "collected": "2026-09-16T00:00:00+00:00"},
  "service": {
    "desiredCount": 1,
    "launchType": "FARGATE",
    "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-private-a"], "securityGroups": ["sg-222"], "assignPublicIp": "DISABLED"}},
    "loadBalancers": []
  },
  "taskDefinition": {
    "family": "worker",
    "taskRoleArn": "arn:aws:iam::123456789012:role/worker-task",
    "executionRoleArn": "arn:aws:iam::123456789012:role/worker-exec",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "1024",
    "memory": "2048",
    "containerDefinitions": [
      {
        "name": "worker",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/worker:1.4.0",
        "essential": true,
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/ecs/worker"}}
      }
    ]
  },
  "targetGroups": [],
  "taskRoleActions": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
  "executionRoleActions": ["ecr:GetAuthorizationToken", "logs:PutLogEvents"],
  "scheduledRules": [],
  "denied": []
}
```

`src/worker.py`:
```python
import boto3
sqs = boto3.client("sqs")
while True:
    for m in sqs.receive_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/jobs").get("Messages", []):
        sqs.delete_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/jobs", ReceiptHandle=m["ReceiptHandle"])
```

`expected.json`:
```json
{
  "rollup": "blocked",
  "findings": [
    {"rule": "container.image", "verdict": "supported", "subject": ""},
    {"rule": "deps.aws-service", "verdict": "needs-investigation", "subject": "sqs"},
    {"rule": "network.private-subnets", "verdict": "needs-investigation", "subject": ""},
    {"rule": "resources.cpu-memory", "verdict": "supported", "subject": ""},
    {"rule": "scaling.min-instances", "verdict": "supported", "subject": ""},
    {"rule": "timeout.request", "verdict": "supported", "subject": ""},
    {"rule": "workload.background", "verdict": "blocked", "subject": ""}
  ]
}
```

- [ ] **Step 5: fixtures/long-running-batch**

`inventory.json`: like sqs-worker with `meta.service` `"nightly-report"`, `desiredCount` `0`, `assignPublicIp` `"ENABLED"`, `taskRoleActions` `[]`, image `"123456789012.dkr.ecr.us-east-1.amazonaws.com/report:2.0"`, container gets `"command": ["python", "report.py"]`, and
```json
"scheduledRules": [{"name": "nightly-report", "scheduleExpression": "cron(0 3 * * ? *)"}]
```

`src/report.py`:
```python
print("report generated")
```

`expected.json`:
```json
{
  "rollup": "blocked",
  "findings": [
    {"rule": "container.command", "verdict": "supported", "subject": ""},
    {"rule": "container.image", "verdict": "supported", "subject": ""},
    {"rule": "resources.cpu-memory", "verdict": "supported", "subject": ""},
    {"rule": "scaling.min-instances", "verdict": "supported", "subject": ""},
    {"rule": "timeout.request", "verdict": "supported", "subject": ""},
    {"rule": "workload.scheduled", "verdict": "blocked", "subject": ""}
  ]
}
```

- [ ] **Step 6: fixtures/denied-permission**

`inventory.json`:
```json
{
  "meta": {"account": "123456789012", "identity": "arn:aws:iam::123456789012:user/readonly", "region": "us-east-1", "cluster": "demo", "service": "web", "collected": "2026-09-16T00:00:00+00:00"},
  "service": {"desiredCount": 1, "launchType": "FARGATE", "networkConfiguration": {"awsvpcConfiguration": {"subnets": ["subnet-aaa"], "assignPublicIp": "ENABLED"}}, "loadBalancers": []},
  "taskDefinition": {},
  "targetGroups": [],
  "taskRoleActions": [],
  "executionRoleActions": [],
  "scheduledRules": [],
  "denied": [{"call": "ecs describe-task-definition", "error": "An error occurred (AccessDeniedException) when calling the DescribeTaskDefinition operation: User is not authorized"}]
}
```
`src/` is an empty directory (add `src/.keep`).

`expected.json`:
```json
{
  "rollup": "blocked",
  "findings": [
    {"rule": "denied", "verdict": "blocked", "subject": "ecs describe-task-definition"}
  ]
}
```

- [ ] **Step 7: Validate all six parse**

```bash
for f in fixtures/*/inventory.json fixtures/*/expected.json; do python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" || echo "BAD $f"; done; ls fixtures
```
Expected: no `BAD` lines; six directories listed.

- [ ] **Step 8: Commit**

```bash
git add fixtures
git commit -m "test: six synthetic ECS fixtures with expected verdicts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Failing tests for assess.py

**Files:**
- Create: `tests/test_assess.py`, `tests/__init__.py` (empty)

- [ ] **Step 1: Write the test file**

```python
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import assess  # noqa: E402

FIXTURES = os.path.join(ROOT, "fixtures")
EXEMPT = {"denied", "source-unavailable", "not-covered"}


def load(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return json.load(fh)


RULES = load("references", "rules.json")
FIXTURE_NAMES = sorted(n for n in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, n)))


def run_fixture(name):
    inv = load("fixtures", name, "inventory.json")
    findings = assess.assess(inv, os.path.join(FIXTURES, name, "src"), RULES)
    return inv, findings, assess.rollup(findings)


def simplify(findings):
    return sorted(
        ({"rule": f["rule"], "verdict": f["verdict"], "subject": f.get("subject", "")} for f in findings),
        key=lambda x: (x["rule"], x["subject"]),
    )


class TestVerdicts(unittest.TestCase):
    def test_every_fixture_matches_expected(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                expected = load("fixtures", name, "expected.json")
                _, findings, roll = run_fixture(name)
                self.assertEqual(roll, expected["rollup"])
                self.assertEqual(simplify(findings), expected["findings"])

    def test_no_false_pass(self):
        for name in FIXTURE_NAMES:
            expected = load("fixtures", name, "expected.json")
            if expected["rollup"] == "supported":
                continue
            with self.subTest(fixture=name):
                _, _, roll = run_fixture(name)
                self.assertNotEqual(roll, "supported")

    def test_every_cloud_run_finding_is_cited(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                _, findings, _ = run_fixture(name)
                for f in findings:
                    if f.get("reason") in EXEMPT:
                        continue
                    with self.subTest(rule=f["rule"]):
                        self.assertTrue(f.get("url"), "missing url")
                        self.assertTrue(f.get("quote"), "missing quote")

    def test_arm64_or_windows_is_blocked(self):
        for rp in ({"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
                   {"cpuArchitecture": "X86_64", "operatingSystemFamily": "WINDOWS_SERVER_2022_CORE"}):
            with self.subTest(runtimePlatform=rp):
                inv = load("fixtures", "stateless-http", "inventory.json")
                inv["taskDefinition"]["runtimePlatform"] = rp
                findings = assess.assess(inv, os.path.join(FIXTURES, "stateless-http", "src"), RULES)
                self.assertEqual(assess.rollup(findings), "blocked")
                self.assertIn("container.architecture", {f["rule"] for f in findings})

    def test_x86_linux_platform_is_not_a_finding(self):
        inv = load("fixtures", "stateless-http", "inventory.json")
        inv["taskDefinition"]["runtimePlatform"] = {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"}
        findings = assess.assess(inv, os.path.join(FIXTURES, "stateless-http", "src"), RULES)
        self.assertEqual(assess.rollup(findings), "supported")

    def test_rules_and_checks_match(self):
        with open(os.path.join(ROOT, "scripts", "assess.py")) as fh:
            src = fh.read()
        used = set(re.findall(r'R\["([a-z.-]+)"\]', src))
        declared = {r["id"] for r in RULES["rules"]}
        self.assertEqual(used, declared)

    def test_missing_source_is_needs_investigation(self):
        inv = load("fixtures", "stateless-http", "inventory.json")
        findings = assess.assess(inv, None, RULES)
        reasons = {f.get("reason") for f in findings}
        self.assertIn("source-unavailable", reasons)
        self.assertEqual(assess.rollup(findings), "needs-investigation")


class TestHelpers(unittest.TestCase):
    def test_secret_name(self):
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:prod/db-url-AbCdEf"), "prod-db-url")
        self.assertEqual(assess.secret_name("arn:aws:ssm:us-east-1:1:parameter/prod/api/key"), "prod-api-key")
        self.assertEqual(assess.secret_name("arn:aws:secretsmanager:us-east-1:1:secret:x-AbCdEf:json_key::"), "x")

    def test_fargate_size_rounds_cpu_up_to_one(self):
        self.assertEqual(assess.fargate_size({"cpu": "256", "memory": "512"}), (0.25, 512, 1))
        self.assertEqual(assess.fargate_size({"cpu": "4096", "memory": "8192"}), (4.0, 8192, 4))
        self.assertEqual(assess.fargate_size({"cpu": "16384", "memory": "32768"}), (16.0, 32768, 16))

    def test_scan_finds_python_and_js_clients(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.py"), "w") as fh:
                fh.write('import boto3\ns3 = boto3.client("s3")\n')
            with open(os.path.join(d, "b.ts"), "w") as fh:
                fh.write('import { SQSClient } from "@aws-sdk/client-sqs";\n')
            hits = assess.scan(d)
        self.assertEqual(sorted(hits), ["s3", "sqs"])
        self.assertEqual(hits["s3"], ["a.py:2"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it and confirm it fails for the right reason**

```bash
touch tests/__init__.py
python3 -m unittest tests.test_assess -v 2>&1 | grep -E 'ModuleNotFound|FAILED'
```
Expected: `ModuleNotFoundError: No module named 'assess'` and `FAILED (errors=1)`.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests
git commit -m "test: verdict, no-false-pass, citation, parity and helper tests for assess (red)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: assess.py

**Files:**
- Create: `scripts/assess.py`

- [ ] **Step 1: Write the script**

```python
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
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.read().splitlines()
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
```

- [ ] **Step 2: Run the tests**

```bash
python3 -m unittest tests.test_assess -v 2>&1 | tail -15
```
Expected: all tests `ok`, ending `OK` (17 tests after Task 6 and 7 additions; 9 now). If a fixture fails, compare the printed `simplify` output to `expected.json`; fix the fixture only if the script's behavior matches the spec, otherwise fix the script.

- [ ] **Step 3: Run the CLI on one fixture and read the summary**

```bash
python3 scripts/assess.py --inventory fixtures/sqs-worker/inventory.json --src fixtures/sqs-worker/src --out /tmp/a.json | head -30
```
Expected: `ROLLUP: blocked`, a `workload.background` entry with a docs URL and quote, a `deps.aws-service [sqs]` entry whose evidence lists both `iam:` and `src:` lines.

- [ ] **Step 4: Commit**

```bash
chmod +x scripts/assess.py
git add scripts/assess.py
git commit -m "feat: assess.py rules engine with source scan and unmatched-field findings (green)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: generate.py and golden files

**Files:**
- Create: `scripts/generate.py`, `fixtures/stateless-http/golden/service.yaml`, `fixtures/stateless-http/golden/deploy.sh`
- Modify: `tests/test_assess.py`

- [ ] **Step 1: Add the golden and refusal tests (red)**

Append to `tests/test_assess.py` before `if __name__`:

```python
import generate  # noqa: E402


class TestGenerate(unittest.TestCase):
    def test_golden_stateless_http(self):
        inv, findings, roll = run_fixture("stateless-http")
        assessment = {"meta": inv["meta"], "rollup": roll, "findings": findings}
        yaml_text, sh_text = generate.generate(assessment, inv, "my-project", "us-central1")
        g = os.path.join(FIXTURES, "stateless-http", "golden")
        with open(os.path.join(g, "service.yaml")) as fh:
            self.assertEqual(yaml_text, fh.read())
        with open(os.path.join(g, "deploy.sh")) as fh:
            self.assertEqual(sh_text, fh.read())

    def test_refuses_blocked(self):
        inv, findings, roll = run_fixture("sqs-worker")
        with self.assertRaises(SystemExit):
            generate.generate({"meta": inv["meta"], "rollup": roll, "findings": findings}, inv, "p", "r")

    def test_omits_non_supported(self):
        inv, findings, roll = run_fixture("efs-mount")
        yaml_text, _ = generate.generate({"meta": inv["meta"], "rollup": roll, "findings": findings}, inv, "p", "us-central1")
        self.assertIn("# OMITTED: data — see finding storage.efs", yaml_text)
        self.assertNotIn("nfs:", yaml_text)
```

Move the `import generate` line up next to `import assess`. Run:
```bash
python3 -m unittest tests.test_assess 2>&1 | tail -3
```
Expected: `ModuleNotFoundError: No module named 'generate'`.

- [ ] **Step 2: Write generate.py**

```python
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
```

- [ ] **Step 3: Produce the golden output and review it by hand**

```bash
python3 scripts/assess.py --inventory fixtures/stateless-http/inventory.json --src fixtures/stateless-http/src --out /tmp/a.json >/dev/null
python3 scripts/generate.py --assessment /tmp/a.json --inventory fixtures/stateless-http/inventory.json --project my-project --region us-central1 --out-dir /tmp/gen
cat /tmp/gen/service.yaml; echo =====; cat /tmp/gen/deploy.sh
```

Check against the spec, section 7, before accepting:
- `service.yaml` has minScale `"2"`, `timeoutSeconds: 300`, the hello image unchanged, port 8080, cpu `"1"`, memory `1024Mi`, env `APP_ENV` and `API_KEY` (value `"<redacted>"`), a `secretKeyRef` to `prod-db-url` with key `"1"`, a startup probe on `/`, and **no** `# OMITTED:` lines.
- `deploy.sh` has 9 steps in this order: enable APIs, service account, secrets create, IAM grant, PAUSE, replace, URL, smoke, logs. No ECR/Docker steps because the image is public. The IAM grant is before the PAUSE, and the PAUSE is before replace.
- `bash -n /tmp/gen/deploy.sh` exits 0.

- [ ] **Step 4: Freeze the golden files and run the tests**

```bash
mkdir -p fixtures/stateless-http/golden
cp /tmp/gen/service.yaml /tmp/gen/deploy.sh fixtures/stateless-http/golden/
python3 -m unittest tests.test_assess -v 2>&1 | tail -6
```
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
chmod +x scripts/generate.py
git add scripts/generate.py fixtures/stateless-http/golden tests/test_assess.py
git commit -m "feat: generate.py emits service.yaml + deploy.sh; golden files pin step order

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: inventory.py

**Files:**
- Create: `scripts/inventory.py`
- Modify: `tests/test_assess.py`

- [ ] **Step 1: Add helper tests (red)**

Append to `tests/test_assess.py`, with `import inventory` next to the other imports:

```python
class TestInventoryHelpers(unittest.TestCase):
    def test_redact_matches_secret_like_keys(self):
        env = [{"name": "APP_ENV", "value": "prod"}, {"name": "DB_PASSWORD", "value": "x"},
               {"name": "Api-Key", "value": "y"}, {"name": "PRIVATE_KEY_PATH", "value": "/k"}]
        out = inventory.redact(env)
        self.assertEqual([e["value"] for e in out], ["prod", "<redacted>", "<redacted>", "<redacted>"])

    def test_policy_actions_flattens_allow_statements(self):
        doc = {"Statement": [
            {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage"], "Resource": "*"},
            {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"},
            {"Effect": "Deny", "Action": "iam:*", "Resource": "*"},
        ]}
        self.assertEqual(inventory.policy_actions(doc), ["s3:GetObject", "sqs:DeleteMessage", "sqs:ReceiveMessage"])
```
Run: `python3 -m unittest tests.test_assess 2>&1 | tail -2` → `No module named 'inventory'`.

- [ ] **Step 2: Write inventory.py**

```python
#!/usr/bin/env python3
"""Collect one ECS service's configuration with read-only aws CLI calls. Standard library only.

Usage:
  inventory.py --cluster CLUSTER --service SERVICE [--region REGION] [--out inventory.json]

Only describe/list/get calls are made. Never calls secretsmanager get-secret-value or
ssm get-parameter. Environment values whose key looks secret are replaced with "<redacted>".
Every failed call is recorded under "denied" so nothing missing is ever assumed present.
"""
import argparse
import datetime
import json
import re
import subprocess

SECRET_KEY = re.compile(r"(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential)")
DENIED = []


def aws(*args, region=None):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        DENIED.append({"call": " ".join(args[:2]), "error": p.stderr.strip()[:300]})
        return None
    return json.loads(p.stdout) if p.stdout.strip() else {}


def redact(env):
    return [{"name": e["name"], "value": "<redacted>" if SECRET_KEY.search(e["name"]) else e.get("value", "")}
            for e in env or []]


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--region")
    ap.add_argument("--out", default="inventory.json")
    a = ap.parse_args()
    R = a.region

    ident = aws("sts", "get-caller-identity", region=R) or {}
    svcs = (aws("ecs", "describe-services", "--cluster", a.cluster, "--services", a.service, region=R) or {}).get("services", [])
    svc = svcs[0] if svcs else {}
    td = {}
    if svc.get("taskDefinition"):
        td = (aws("ecs", "describe-task-definition", "--task-definition", svc["taskDefinition"], region=R) or {}).get("taskDefinition", {})
    for c in td.get("containerDefinitions", []):
        if "environment" in c:
            c["environment"] = redact(c["environment"])

    tgs = []
    arns = [lb["targetGroupArn"] for lb in svc.get("loadBalancers", []) if lb.get("targetGroupArn")]
    if arns:
        for tg in (aws("elbv2", "describe-target-groups", "--target-group-arns", *arns, region=R) or {}).get("TargetGroups", []):
            tgs.append({"targetGroupArn": tg["TargetGroupArn"], "healthCheckProtocol": tg.get("HealthCheckProtocol"),
                        "healthCheckPath": tg.get("HealthCheckPath"), "port": tg.get("Port")})

    sched = []
    if svc.get("clusterArn"):
        for rn in (aws("events", "list-rule-names-by-target", "--target-arn", svc["clusterArn"], region=R) or {}).get("RuleNames", []):
            targets = (aws("events", "list-targets-by-rule", "--rule", rn, region=R) or {}).get("Targets", [])
            fam = td.get("family")
            if fam and any(fam in (t.get("EcsParameters") or {}).get("TaskDefinitionArn", "") for t in targets):
                rule = aws("events", "describe-rule", "--name", rn, region=R) or {}
                sched.append({"name": rn, "scheduleExpression": rule.get("ScheduleExpression", "")})

    inv = {
        "meta": {"account": ident.get("Account", ""), "identity": ident.get("Arn", ""), "region": R or "",
                 "cluster": a.cluster, "service": a.service,
                 "collected": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
        "service": {k: svc[k] for k in ("desiredCount", "launchType", "networkConfiguration", "loadBalancers") if k in svc},
        "taskDefinition": td,
        "targetGroups": tgs,
        "taskRoleActions": role_actions(td.get("taskRoleArn"), R),
        "executionRoleActions": role_actions(td.get("executionRoleArn"), R),
        "scheduledRules": sched,
        "denied": DENIED,
    }
    json.dump(inv, open(a.out, "w"), indent=2)
    redacted = sum(1 for c in td.get("containerDefinitions", []) for e in c.get("environment", []) if e["value"] == "<redacted>")
    print(f"wrote {a.out}: {len(td.get('containerDefinitions', []))} container(s), {len(tgs)} target group(s), "
          f"{len(sched)} schedule(s), {redacted} env value(s) redacted, {len(DENIED)} denied call(s)")
    for d in DENIED:
        print(f"  denied: {d['call']}: {d['error'][:120]}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run tests and a no-credentials smoke**

```bash
python3 -m unittest tests.test_assess 2>&1 | tail -2
AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=y python3 scripts/inventory.py --cluster demo --service web --region us-east-1 --out /tmp/inv.json; python3 -c "import json; d=json.load(open('/tmp/inv.json')); print(len(d['denied']), d['taskDefinition'])"
```
Expected: tests `OK`. The smoke prints at least 2 denied calls and `{}` for the task definition. If `aws` is not installed, the second command fails with a Python `FileNotFoundError`; wrap the `subprocess.run` in `try/except FileNotFoundError` that appends a denied entry `{"call": "aws", "error": "aws CLI not found"}` and returns `None`, then re-run.

- [ ] **Step 4: Commit**

```bash
chmod +x scripts/inventory.py
git add scripts/inventory.py tests/test_assess.py
git commit -m "feat: inventory.py read-only aws collection with redaction and denied recording

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: docdrift.py and the weekly workflow

**Files:**
- Create: `scripts/docdrift.py`, `.github/workflows/docdrift.yml`

- [ ] **Step 1: Write docdrift.py**

```python
#!/usr/bin/env python3
"""Maintainer tool. Re-fetch every page cited in rules.json and check each quoted sentence still exists.

Usage:
  docdrift.py [--rules references/rules.json] [--write]

Exits 1 and lists the rule ids whose quote is gone. With --write, sets "stale": true on those rows
(and false on the rest) in place. Never run by the skill during the guided workflow.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "..", "references", "rules.json")
_CACHE = {}


def norm(t):
    return re.sub(r"\s+", " ", html.unescape(t)).replace("’", "'").strip()


def page_text(url):
    if url not in _CACHE:
        req = urllib.request.Request(url, headers={"User-Agent": "fargate-to-cloudrun docdrift"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        _CACHE[url] = norm(re.sub(r"<[^>]+>", "", raw))
    return _CACHE[url]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    with open(a.rules) as fh:
        doc = json.load(fh)
    stale = []
    for r in doc["rules"]:
        try:
            ok = norm(r["quote"]) in page_text(r["url"])
        except Exception as e:  # network or HTTP error counts as not verified
            print(f"{r['id']}: fetch failed: {e}")
            ok = False
        if not ok:
            stale.append(r["id"])
    if a.write:
        for r in doc["rules"]:
            r["stale"] = r["id"] in stale
        with open(a.rules, "w") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    if stale:
        print("STALE:", *stale)
        sys.exit(1)
    print(f"all {len(doc['rules'])} citations present")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it once against the live docs**

```bash
python3 scripts/docdrift.py
```
Expected: `all 18 citations present`. If any id prints as STALE, open that URL, find the current sentence that supports the same claim, update `quote` in `rules.json`, and re-run. Do not weaken the claim to make the check pass; if the docs no longer support it, change the rule's `verdict` and `explain` to what the docs now say.

- [ ] **Step 3: Write the workflow**

`.github/workflows/docdrift.yml`:
```yaml
name: docdrift
on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 scripts/docdrift.py
```

- [ ] **Step 4: Commit**

```bash
chmod +x scripts/docdrift.py
git add scripts/docdrift.py .github/workflows/docdrift.yml
git commit -m "feat: docdrift.py citation check and weekly workflow

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: SKILL.md

**Files:**
- Create: `SKILL.md`

- [ ] **Step 1: Write the skill**

```markdown
---
name: fargate-to-cloudrun
description: Assess one Amazon ECS/Fargate service for Google Cloud Run compatibility with evidence quoted from the Cloud Run documentation, then guide a private staging deploy. Use when the user wants to move, migrate, port, or compare an ECS or Fargate service to Cloud Run or Google Cloud.
---

# fargate-to-cloudrun

You are guiding someone who knows their AWS app but may know nothing about Google Cloud.
Assume that for every operator. Explain each Google Cloud concept the first time it appears,
in one or two plain sentences, and link the documentation. One ECS service per run.

## Two invariants. Never break them.

1. **Nothing writes to AWS.** Only `describe`, `list`, and `get` calls. Never `secretsmanager
   get-secret-value`, never `ssm get-parameter`. `aws ecr get-login-password` is allowed: it only
   obtains a pull token.
2. **Nothing that creates, changes, or deletes anything on Google Cloud runs without the user
   saying yes to that specific command, immediately before it runs.** Read-only checks
   (`gcloud auth list`, `gcloud config get project`, `gcloud billing projects describe`) need no
   confirmation.

## Grounding rule

Every statement you make about how Cloud Run behaves comes from `references/rules.json`: the
`explain` sentence, the `url`, and the `quote`. If the user asks something no row covers, say
"that is not covered by the referenced documentation" and stop there. Do not search the web. Do
not answer from memory. This is what makes the tool trustworthy.

Explanations of Google Cloud concepts come from three places, in this order: the matching row in
`rules.json`; the comment block on the matching step in the generated `deploy.sh`; and, for the
preflight concepts below, the fixed paragraphs in this file.

## Preflight concepts (fixed text)

- **gcloud** is Google Cloud's command-line tool, the equivalent of the aws CLI. Install:
  https://docs.cloud.google.com/sdk/docs/install. Log in with `gcloud auth login`.
- **A project** is the container for everything you create on Google Cloud, like an AWS
  account. It must have **billing enabled** before Cloud Run will deploy anything. Creating one:
  https://docs.cloud.google.com/resource-manager/docs/creating-managing-projects. A staging deploy
  of one small service costs cents per day while it runs; the closing report tells you how to
  delete it.
- **Docker** is needed only when the image lives in ECR, to copy it. Install:
  https://docs.docker.com/get-docker/.

## Phase 1: Preflight

Run and report, in this order. Stop at the first missing item, explain it with the paragraph
above, and wait.

1. `aws sts get-caller-identity` — report the account and identity. Region comes from the
   profile; ask only if unset.
2. `gcloud auth list` and `gcloud config get project` — if gcloud is missing or not logged in,
   stop. If no project is set, ask which project to use; do not create one.
3. `gcloud billing projects describe <project>` — if billing is not enabled, stop.
4. `docker --version` — if missing and the service image is in ECR, stop.
5. Ask for: ECS cluster name, service name, and the path to the service's source tree. If the
   user has no source, record that; SDK-usage findings will be needs-investigation.

## Phase 2: Inventory

```
python3 scripts/inventory.py --cluster <cluster> --service <service> [--region <region>] --out inventory.json
```
Tell the user what was collected, how many environment values were redacted, and every denied
call by name. A denied call is a finding, not a gap to paper over.

## Phase 3: Assess

```
python3 scripts/assess.py --inventory inventory.json --src <source-dir> --out assessment.json
```
Omit `--src` if there is no source. Show the printed summary. Then, for each finding, restate it
in plain words using the row's `explain`, and show the `url` and `quote`.

- Rollup **blocked**: explain what would unblock each blocked finding (a job, a worker pool, a
  supported CPU/memory pair, the missing AWS permission). Stop. No generation, no deploy.
- Rollup **needs-investigation**: list what the user must verify. Say that those items will be
  omitted from the manifest with `# OMITTED:` markers. Ask whether to continue.
- Rollup **supported**: ask whether to continue.

## Phase 4: Generate

```
python3 scripts/generate.py --assessment assessment.json --inventory inventory.json --project <project> --region <region> --out-dir out
```
Show `out/service.yaml` and `out/deploy.sh` in full. Walk through each `deploy.sh` step's comment
block. Ask whether to proceed.

## Phase 5: Staging deploy

Run `out/deploy.sh` **one step at a time, yourself**, not by executing the file. Before each
step: say what it does, what it costs, and wait for yes. On any failure: stop, show the error
verbatim, show the step's docs link. Never retry silently.

The PAUSE step is the user's: print the `gcloud secrets versions add` commands and wait until
they confirm every secret has a value. You never see or handle secret values.

The smoke step calls the service with the user's own identity token. The service stays private.
If the user wants a public URL, that is a separate, explained confirmation after the smoke test:
`gcloud run services add-iam-policy-binding <service> --region <region> --member=allUsers --role=roles/run.invoker`.

## Closing report

- Service URL and smoke result, with output.
- Every remaining needs-investigation finding and every `# OMITTED:` item.
- Not done, by design: production traffic, database migration, AWS teardown.
- To stop paying: `gcloud run services delete <service> --region <region>`.

## Replaying a fixture (portability check)

To exercise this workflow without an AWS account, start at Phase 3 with
`--inventory fixtures/<name>/inventory.json --src fixtures/<name>/src`, proceed through Phase 4,
and decline the first deploy.sh confirmation. No Google Cloud resource is created.
```

- [ ] **Step 2: Check the frontmatter parses and the file is under 300 lines**

```bash
head -4 SKILL.md; wc -l SKILL.md
```
Expected: frontmatter with `name:` and `description:`; line count under 300.

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "feat: SKILL.md guided workflow with invariants, grounding rule, and preflight text

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Write the full README**

```markdown
# fargate-to-cloudrun

An agent skill that assesses **one** Amazon ECS/Fargate service for Google Cloud Run, with every
verdict quoted from the Cloud Run documentation, and then guides a **private staging deploy**.

It runs in Claude Code, Gemini CLI, and Codex.

## What it does

1. **Inventory** your ECS service with read-only aws CLI calls. Secret-looking values are
   redacted; secret values are never read.
2. **Assess** each part of the task definition against rules in `references/rules.json`. Every
   rule carries the docs URL, the exact quoted sentence, and the snapshot date. Verdicts are
   `supported`, `needs-investigation`, or `blocked`. Missing evidence never passes.
3. **Generate** a Cloud Run `service.yaml` and a numbered `deploy.sh`, then walk you through the
   deploy one confirmed step at a time. The service stays private; a smoke test uses your own
   identity token.

Not done in v1, by design: Lambda, queues, databases, production traffic, AWS teardown.

## Install

```
npx skills add https://github.com/<owner>/fargate-to-cloudrun
```
Requirements on your machine: Python 3.9+, aws CLI (read-only), gcloud, and Docker if the image
is in ECR.

## Try it without AWS

```
python3 scripts/assess.py --inventory fixtures/sqs-worker/inventory.json --src fixtures/sqs-worker/src
```

## Tests

```
python3 -m unittest tests.test_assess -v
```
The suite asserts that every fixture yields its expected verdicts, that no incompatible fixture
ever rolls up to `supported`, that every Cloud Run finding carries a citation, and that the
generated deploy for the demo fixture is byte-identical to the golden files.

## Keeping citations honest

`scripts/docdrift.py` re-fetches every cited page weekly (GitHub Actions) and fails if a quoted
sentence is gone. Stale rules downgrade their findings until a maintainer reviews them.

## License

Apache 2.0. Quoted documentation is © Google, CC-BY 4.0; see `NOTICE`. Not affiliated with
Google or Amazon.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Final verification and spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-09-16-fargate-to-cloudrun-design.md`

- [ ] **Step 1: Full test run and script sanity**

```bash
python3 -m unittest tests.test_assess -v 2>&1 | tail -20
for s in scripts/*.py; do python3 "$s" --help >/dev/null && echo "ok $s"; done
bash -n fixtures/stateless-http/golden/deploy.sh && echo "deploy.sh parses"
git status --short
```
Expected: `OK` with all tests passing, four `ok scripts/...` lines, `deploy.sh parses`, clean tree.

- [ ] **Step 2: Sync the two deviations into the spec**

In section 7 "Generated artifacts", change the note that "steps 3 and 5 are skipped" for the demo fixture to: all four ECR-related steps (repository create, ECR login, Docker auth, image copy) are skipped when the image is not in ECR. In section 5 Phase 2, remove "target groups and listener rules" in favor of "target groups" only, and add a line under section 3 out-of-scope: "ALB listener rules and path routing (no v1 rule consumes them)". In section 7 "Generated artifacts", change "min/max instances, concurrency, timeout" to "min instances, timeout": max instances and concurrency have no rule row, so under the grounding rule the manifest leaves them at Cloud Run defaults.

- [ ] **Step 3: Commit and tag**

```bash
git add docs/superpowers/specs/2026-09-16-fargate-to-cloudrun-design.md
git commit -m "docs: sync spec with implementation (ECR steps, listener rules)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git tag v0.1.0
git log --oneline | head -12
```

- [ ] **Step 4: Report**

State plainly: tests passing (paste the last line), docdrift result, and that the repo has not yet been pushed anywhere. Pushing to GitHub, the client-contract confirmation, and the portability check in Gemini CLI and Codex are the user's next decisions, not part of this plan.
