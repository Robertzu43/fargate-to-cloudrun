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

`$SKILL_DIR` is the directory containing this file. Use a migration working directory in the user's
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

Check `aws sts get-caller-identity` and the configured region/profile. If the user does not know service
names, use `aws ecs list-clusters`, `list-services`, and `describe-services` to find candidates. Confirm
which application when there are unrelated services. Do not require gcloud, billing, or Docker just to
assess AWS. Inventory each service belonging to the application:

```bash
python3 "$SKILL_DIR/scripts/inventory.py" --cluster CLUSTER --service SERVICE --region REGION --out inventory.json
```

Environment values are withheld by default. After reviewing configuration names and source usage,
repeat with `--include-env NAME` for explicitly non-secret values that are needed. Do not dump raw task
definitions or credential-bearing URLs into the conversation. Other configuration fields use best-effort
redaction; treat inventories as sensitive and inspect what you share.

Follow the evidence beyond the inventory helper: ALB listeners/rules, service discovery, security groups,
routes, autoscaling, schedules, running image digests, database endpoints, queues, storage, outbound IP
allowlists, and AWS SDK calls. Read `coverage.not_collected` and close applicable gaps yourself. Absence
of a regex hit is not proof of absence. Avoid unrelated account-wide collection.

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
