---
name: fargate-to-cloudrun
description: Inspect an application's ECS/Fargate deployment and source, adapt it for Google Cloud Run, deploy and test it, and carry out a reviewed production cutover. Use when someone wants to move an existing AWS container application to Cloud Run without learning the Google Cloud platform first.
---

# Fargate → Cloud Run

Own the migration, not just the assessment. Discover the application, choose a suitable target,
make the necessary code and infrastructure changes, deploy, test real behavior, and complete the
approved production switch. Staging is a checkpoint. A report or a responding URL is not completion.

Explain decisions in terms of downtime, cost, behavior, and rollback. Do not ask the user to choose
Google product names or debug provider configuration you can investigate yourself. Recommend a
specific approach, explain material tradeoffs, and ask only for missing access or consequential decisions.

The scripts beside this file are deterministic helpers for the simple HTTP path. They are not the
boundary of your capabilities. A blocked generator means its automatic mapping cannot safely proceed;
it does not mean stop helping or tell the user to learn Google Cloud.

## Working files and authorization

Export `$SKILL_DIR` once; every command below uses it:

```bash
export SKILL_DIR=/path/to/fargate-to-cloudrun   # the directory containing this file
```

Use a migration working directory in the user's
project, with a separate subdirectory for each service. Keep raw inventories, assessments, credentials,
and deployment state out of version control. Read-only discovery and local preparation need no additional
confirmation within the requested scope.

Keep `migration.md` current: source account/region/services, destination project/region, dependency map,
chosen approach, unresolved findings, artifact/image identities, test results, and the next action.
Read [migration-workflow.md](references/migration-workflow.md) when planning or executing the migration.

The user may authorize a reviewed batch of destination changes; do not ask before every harmless CLI
command. Confirm scope and estimated cost before creating billable infrastructure. Obtain explicit
approval for transferring secret values, production traffic/data-writer cutover, and eventual AWS deletion.
Preserve existing authorization. Never infer cutover or teardown approval from staging approval.

## 1. Discover the app

Start with the user's stated scope and the current repository. Inspect source, Dockerfiles, dependencies,
CI/CD, infrastructure, configuration names, health endpoints, and existing tests. Ask what matters only
if not already known: acceptable downtime, cost ceiling, data location constraints, and who can approve
production changes. Offer a reasonable default with its consequence.

Infrastructure-as-code describes what was declared, not what is running. A task definition in Terraform,
CDK or CloudFormation may carry a placeholder image, a size the service never adopted, or a revision the
service ignores. Read it for intent and for the things the AWS API does not expose, then take the inventory
from the live service. If you cannot reach the AWS API, say the inventory is unverified and record how it
was built -- do not present a declared task definition as the deployed one.

Check `aws sts get-caller-identity` and the configured region/profile. If the user does not know service
names, use `aws ecs list-clusters`, `list-services`, and `describe-services` to find candidates. Confirm
which application when there are unrelated services. Do not require gcloud, billing, or Docker just to
assess AWS. Inventory each service belonging to the application:

```bash
python3 "$SKILL_DIR/scripts/inventory.py" --cluster CLUSTER --service SERVICE --region REGION --out inventory.json
```

Without AWS credentials, have the owner export `aws ecs describe-services` and
`describe-task-definition` for the running service and pass `--service-file` / `--taskdef-file`.
The inventory records in `meta.provenance` which parts did not come from the live API, and says
so on stderr. That is the only substitute; a declared task definition is not one.

Environment values are withheld by default. After reviewing configuration names and source usage,
repeat with `--include-env NAME` for explicitly non-secret values that are needed. Do not dump raw task
definitions or credential-bearing URLs into the conversation. Other configuration fields use best-effort
redaction; treat inventories as sensitive and inspect what you share.

Follow the evidence beyond the inventory helper: ALB listeners/rules, service discovery, security groups,
routes, autoscaling, schedules, running image digests, database endpoints, queues, storage, outbound IP
allowlists, and AWS SDK calls. Read `coverage.not_collected` and close applicable gaps yourself. Absence
of a regex hit is not proof of absence. Avoid unrelated account-wide collection.

Two gaps are common enough to check by name, because nothing in the inventory will raise them:

- **Who authenticates the user?** An `authenticate-cognito` or `authenticate-oidc` listener action means the
  application never implemented its own login and an ALB has been gating it. Cloud Run cannot be an ALB
  target, so this becomes IAP or application-level auth. Find any path that is deliberately exempt today
  (a webhook, a cron endpoint, a health check): IAP enabled on the Cloud Run service is service-scoped and
  cannot exempt a path, so exemptions decide the design -- see the Front-door auth row in
  [migration-workflow.md](references/migration-workflow.md). A single identity provider shared by several
  services also means migrating one service can remove its users from that shared sign-in.
- **What triggers the periodic work?** Not every schedule is an EventBridge rule. A database extension, an
  external SaaS webhook, a partner cron, or another service can call in over HTTP on a timer, and none of
  them appear anywhere in the AWS account. Ask what calls this service and from where, and repoint each
  caller at cutover. Ask what each caller sends in the `Authorization` header too: a private Cloud Run
  service consumes that header for its own IAM check, so a caller with its own bearer token is rejected
  before the container sees it. `X-Serverless-Authorization` carries the ID token instead, but only if the
  caller can be configured to send it; when it cannot, that path has to stay reachable without Cloud Run
  IAM auth. The Inbound schedules row in [migration-workflow.md](references/migration-workflow.md) has
  the full consequence.

## 2. Assess, then resolve

```bash
python3 "$SKILL_DIR/scripts/assess.py" --inventory inventory.json --src /path/to/app --out assessment.json
```

`references/rules.json` provides initial mappings, official links, and reviewed excerpts. Quotes support
individual platform facts; they do not prove the application will work. The helper's internal
`rollup: supported` means only that the checked fields map; its human-facing result is a candidate for
validation. Review coverage limitations even for that result.

For every unresolved finding, determine the underlying requirement, investigate current official AWS or
Google documentation, and implement the resolution. Research is allowed and required when the bundled
rules are insufficient or stale. Record the source, decision, and a test of the chosen behavior.

Some findings cannot be cleared by collecting more evidence: a denied AWS call, a source tree you cannot
read, a field outside the mappings that you have investigated and judged safe. Adjudicate those in a
resolutions file and re-run the assessment with it. The decision is recorded in the assessment and printed
above the findings; it is never made by editing findings by hand.

```bash
cat > resolutions.json <<'JSON'
{"denied:ecs:DescribeServices": {"decision": "service read from an operator-exported service.json",
                                 "evidence": ["service.json sha256 ...", "desiredCount confirmed with the owner"]}}
JSON
python3 "$SKILL_DIR/scripts/assess.py" --inventory inventory.json --src /path/to/app \
  --resolutions resolutions.json --out assessment.json
```

The key is `rule` or `rule:subject`, exactly as the summary and the generator's refusal print it. A key that
matches no open finding is an error, not a no-op: the inventory changed and the decision needs re-checking.
Resolve a finding because you established the answer, never to make the generator proceed.

Typical work includes rebuilding for Linux amd64; adapting startup, health checks and ports; migrating
configuration; configuring private connectivity and identities; retaining AWS dependencies with appropriate
authentication; or implementing and testing replacements when moving those dependencies is approved.
Choose services, finite jobs, or worker pools based on actual execution behavior—not just exposed ports.
If Cloud Run cannot satisfy a requirement, explain that specific mismatch and propose the smallest viable
alternative. Do not silently replace the user's target or remove required functionality.

Make local code changes and deployment artifacts yourself. Preserve the original inventory as evidence;
never delete findings, alter the assessment verdict, or invent source facts to force the generator to pass.
For workloads outside its mappings, author the correct configuration directly from verified documentation,
with a finding-to-change record and tests. Do not weaken the reusable checks for one application.

### A dependency you keep in AWS needs a credential path

On Fargate the task role authenticates every AWS call the container makes. **That role does not
exist on Cloud Run, and a Google service account cannot assume it.** Any retained AWS dependency —
SES, S3, DynamoDB, SQS — stops working at cutover unless you give it credentials explicitly. Decide
this while resolving `deps.aws-service` findings, not after the first failure in production.

Scope whatever you create to exactly what the task role granted, and no wider: reproduce its
conditions (`ses:FromAddress`, a bucket ARN, a metric namespace), and leave out any grant the
deployed source never exercises. A grant nobody calls is easy to carry across and hard to remove
later. Whatever the credential is, it belongs in Secret Manager and reaches the container the same
way every other secret does; it never goes in the manifest, a build argument, or a log line.

## 3. Prepare Google Cloud and secrets

Now check gcloud authentication, destination project and billing, region, permissions, and Docker if needed.
Select a region based on users, dependencies, data constraints, and available services. Explain and help
perform sign-in/setup; leave interactive credentials to the user. Request narrowly scoped permissions for
the chosen operations rather than requiring project Owner.

Prepare a private validation service or isolated revision, real image, runtime identity, network, and
required dependencies. Inspect any existing target service and IAM before modifying it. Default private
creation does not prove an existing service is private. Keep a source configuration snapshot and use the
running image digest when copying; pull/build for `linux/amd64` and verify the destination digest.

For supported Secrets Manager/SSM references, plan the transfer (no provider calls or value reads):

```bash
python3 "$SKILL_DIR/scripts/transfer_secrets.py" --assessment assessment.json --project PROJECT
```

A secret that exists with no version -- common where infrastructure code creates the container and the
value is set out of band -- cannot be read, and the helper reports the provider error in summary form.
Confirm each source secret has a version before the transfer, and check that the identity running it can
read values at all; a plan-only or CI role often deliberately cannot.

Explain which accounts and references will be read and which project will receive them. After explicit
approval, enable Secret Manager if needed and run the same command with `--apply --versions-out secret-versions.json`.
This helper preserves JSON-key/version selectors, sends values through pipes, and writes only destination
version metadata. A repeat apply creates new versions; retain the returned metadata. If partial failure
occurs, report which copies completed and resolve the cause before retrying. It never changes AWS secrets.

Withheld plain environment values need a separate secure transfer into Secret Manager or an explicitly
reviewed non-secret configuration value. Perform any approved transfer through a local process/pipe, not
by displaying the values in a tool result. Never put credentials in command arguments, generated manifests,
logs, or chat. Non-ARN references must be resolved to their full AWS ARNs before using the transfer helper.

## 4. Deploy and verify

For the simple HTTP path, once findings are resolved and required secret versions exist:

```bash
python3 "$SKILL_DIR/scripts/generate.py" --inventory inventory.json --assessment assessment.json \
  --project PROJECT --region REGION --secret-versions secret-versions.json --out-dir out
```

`--registry-repo` selects the Artifact Registry repository the image is copied into; point it at an
existing one rather than creating a repository per migration. The destination image keeps the *source
image's* name, not the ECS service name. `--ingress` writes `run.googleapis.com/ingress` explicitly and
defaults to `all`, which is also Cloud Run's own default: it is what lets the generated smoke test reach
the `run.app` URL with an identity token while IAM keeps the service private. Pass
`internal-and-cloud-load-balancing` once the service sits behind a load balancer -- `run.app` then stops
answering, and the script emits an ingress check in place of that smoke test. `--min-instances` sets
minScale, which defaults to 0: the ECS desired count is reported as evidence, never copied, because a fixed task
count is not a floor on idle instances. Raise it only to buy away cold starts, having weighed idle
cost. When the inventory carries the image digest, the manifest pins the digest and the script
verifies the copy matches; a tag can be repointed after the revision exists.

Omit `--secret-versions` if the app has no secret references. Review the generated files and chosen target
before executing destination changes. Run approved steps with `set -euo pipefail`. The script does not
contain its own approval UI. For a manually adapted workload, apply and verify its reviewed artifacts instead.

Do not stop at the generated HTTP smoke check. Run existing application tests and at least one real
critical flow: authentication, a representative request, database access, queue processing, or file access,
as applicable. Use isolated test data; prevent staging from sending real emails, charging customers, or
competing for production work. Test timeouts, concurrency, scaling, background execution, and error paths.
Compare results with the AWS baseline and record commands and outcomes without sensitive output.

Fix failures and repeat the affected checks. Show what is verified and any remaining blocker. Do not
invent successful deployment, performance, compatibility, or cross-agent validation results.

## 5. Complete the approved migration

Use the production and rollback procedure in [migration-workflow.md](references/migration-workflow.md).
Prepare exact commands for the actual front door, routing, data-writer ownership, monitoring, and rollback.
Run read-only prechecks and show the user the concrete change, expected interruption, cost, and rollback
boundary. Ask for production cutover approval only when the plan is ready to execute.

After approval, execute it, verify traffic and critical application flows on the real hostname, observe
agreed metrics, and roll back if the agreed thresholds fail. Keep AWS capacity available through the agreed
rollback window. Update deployment automation so the next application release reaches the new target.
AWS decommissioning is a later, separately approved operation with backups and dependency checks.

Close with the working application URL, what moved, what intentionally remains in AWS, test evidence,
operating/deployment instructions, actual remaining costs/resources, and rollback status. If access or a
user decision prevents completion, name that specific blocker and the exact next action; do not call a
staging deployment a completed migration.
