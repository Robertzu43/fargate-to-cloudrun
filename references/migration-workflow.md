# Executing a migration

This guide is for the agent. The operator supplies access and business constraints; the agent investigates,
implements, and executes the approved work. Keep a short `migration.md` in the working project so another
session can resume without rediscovering decisions. Populate it with actual evidence, not checked boxes
copied from this document.

## Discover and choose the target

Record AWS account/profile/region, each service and task-definition revision, repository commit, running
image digest, front door, domains, and destination project/region. Match the source code to the deployed
image where possible. A mutable tag is not evidence that the examined source is the deployed application.

Draw the application's real dependency map. For each dependency, record **retain**, **move**, **replace**,
or **not needed**, with evidence. Preserve behavior unless a change is approved. An AWS SDK call is not
fixed by changing the runtime service account. Keeping an AWS dependency requires a working credential
and network path; replacing it requires code, data/semantic mapping, and tests.

| Requirement | Investigate and implement |
|---|---|
| HTTP/API | Listening address and port, TLS termination, auth, headers, request duration, concurrency, sessions, health paths and timing. |
| Background work | Whether work ends, runs continuously, or follows requests; shutdown/retry/idempotency behavior; jobs or worker pools as appropriate. A health port does not make a worker an HTTP application. |
| Database | Engine/version, extensions, network, pooling, schema, backups, restore, replication lag, writer ownership. Retaining it temporarily may be the smallest reliable first step. |
| Queue/events | Delivery semantics, consumer identity, duplicate handling, ordering, retries, dead letters, schedules, and preventing two active consumers from double-processing. |
| Files | Persistence, locking, permissions, throughput and backup. Do not replace file storage with object storage without checking application semantics. |
| Sidecars | Actual responsibility, required startup order, shared resources, credentials and platform alternatives. Do not omit a required sidecar. |
| Front-door auth | Authentication terminated at the load balancer (`authenticate-cognito`, `authenticate-oidc`) does not move with the container: Cloud Run cannot be an ALB target. Map it to a Google load balancer with IAP or to application-level auth, carry every deliberate path exemption across as an explicit rule, and tell the user plainly if the move takes the service out of a shared sign-in. |
| Inbound schedules | A trigger outside AWS -- a database extension, a SaaS webhook, a partner cron -- is invisible in the account and has to be repointed at cutover, with the old endpoint kept until it is. |
| Network/security | ALB listeners and auth, routing, private endpoints, security groups, outbound IPs, allowlists, service discovery, ingress/IAM. Public IP enabled does not imply no private dependencies. |
| Operations | Image build/release workflow, logs/metrics/alerts, autoscaling, quotas, cost baseline, graceful termination and on-call access. |

Use current official documentation for behavior beyond the bundled mapping rules:
[Cloud Run contract](https://docs.cloud.google.com/run/docs/container-contract),
[services/jobs/worker pools](https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run),
[identity](https://docs.cloud.google.com/run/docs/securing/service-identity),
[private connectivity](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc),
[secrets](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

## Prepare an executable plan

For every finding, record the observed requirement, implemented change, artifact or source file, official
reference, and how it will be tested. For generator limitations, write a reviewed manifest, Terraform, or
CLI script directly; do not change source evidence to suppress the finding. Preserve existing repository
infrastructure conventions. Avoid introducing a new IaC framework just for one service.

Decide minimum/maximum instances, concurrency, CPU allocation, memory, timeout and probes explicitly.
The helper's defaults are starting values, not equivalents of ECS policies. Estimate the whole destination
cost, including idle capacity, database, network/egress, image storage, secrets, logs and overlap during
migration. Use current provider pricing and observed usage; never promise a fixed cents-per-day cost.

Before applying, inspect destination resources and service IAM. A fresh service is private by default;
a previously public one can remain public. Verify unauthenticated invocation is rejected and authorized
invocation succeeds when the intended target is private. Back up existing service configuration before an
approved replacement. Do not overwrite a production service as a staging experiment.

The deployment identity needs permissions for the operations actually used: Cloud Run deployment,
acting as the runtime identity, image publishing, API enablement, and any approved IAM/secret creation.
Scope grants to resources where possible. Project Owner is not a prerequisite. Separate provisioning
privileges from the runtime service account's access.

## Transfer secrets without exposing values

`transfer_secrets.py` accepts supported `secrets.env` mappings in an assessment. Its default is a local
plan. `--apply` reads the named AWS secrets and creates/adds Google versions. It supports Secrets Manager
string/binary values, JSON string selectors and version-stage/version-id selectors, and SSM values.
It requires full ARNs and appropriate source decryption permissions. Resolve names to ARNs first.

Approve the transfer scope separately. The helper holds values in process memory and passes them on
stdin; it does not print them or persist them as files. This is not a claim that process memory is erased.
Provider errors are intentionally summarized so payloads cannot enter tool output. Inspect permission or
resource metadata to debug failures. Do not enable shell tracing around secret operations.

Review the metadata returned in `secret-versions.json`, then regenerate the manifest with
`--secret-versions secret-versions.json`. Verify the selected versions are enabled. Reapplying creates
new versions; use the newly returned version numbers. If a run partially fails, already-created resources
and versions remain; the file records completed copies. Copying a value does not automatically rotate it
or revoke the AWS value. Inspect source/destination policy and application behavior after transfer.

Plain environment values are withheld by the inventory helper. For a non-secret, the agent can re-collect
an explicitly reviewed name using `--include-env`. For a credential, prepare a local transfer that reads
the exact task-definition/environment field and sends it to Secret Manager on stdin, then reference its
numeric version in the reviewed manifest. Do not make the user paste a password into chat.

## Rehearse with the real application

Use the actual built image and necessary dependencies. A hello-world container validates infrastructure,
not the migrated application. Run the same critical flows against AWS and the private Google deployment,
using isolated test data. Record the image digest, configuration version, commands, expected results, and
observed results. The example in `examples/configured-api/` demonstrates why health and functional checks
must be separate; it is not a production migration certification.

Include auth and authorization failures, required configuration, persistence, queue semantics, retries,
external APIs, timeouts, representative load, startup/shutdown, and log/alert visibility where applicable.
Check post-response/background work explicitly. Set acceptance thresholds from the application's existing
baseline and the user's requirements. Do not invent performance targets and treat them as approved.

## Production cutover

Prepare these concrete details before asking to switch:

- Source and destination image/config identities, verified production configuration and data endpoints.
- Exact hostname/front-door records or routing resources, current values, proposed values, and owners.
- Whether both runtimes can safely operate concurrently against the chosen data source.
- Accepted downtime and change window, validation commands, error/latency thresholds, observation window.
- Exact rollback commands and prerequisites, retained source capacity, and the point beyond which data
  recovery is required instead of a traffic-only rollback.

Cloud Run revision traffic splitting controls revisions within one Cloud Run service. It does not split
traffic between ECS and Cloud Run. Inspect the existing DNS/load-balancing arrangement and implement the
cross-cloud route appropriate to it. Account for TLS, hostname routing, caching/TTL, sticky sessions,
long-lived connections, and callback URLs. Do not assume changing a DNS record moves every client at once.

For data moves, identify the single authoritative writer, replication direction/lag, final catch-up and
write-freeze steps if needed, consistency checks, and rollback after new writes. Switching traffic back
does not restore writes that exist only on the new database. Google Database Migration Service does not
provide a generic dual-write solution; inspect the actual engine and supported migration path in the
[official database migration documentation](https://docs.cloud.google.com/database-migration/docs/overview).
Rehearse the data procedure and restoration before approving irreversible steps.

After explicit cutover approval, apply only the reviewed changes, monitor both sides, run functional
checks through the production hostname, and enact the agreed rollback if thresholds fail. If new evidence
invalidates the rollback plan, stop the next irreversible step and explain the specific decision needed.

## Finish and retire

Record the production URL, verification results, deployment pipeline change, runtime identity, secret
rotation procedure, observability links, dependencies still in AWS, and the rollback-window end. If AWS
resources remain by design, call it a compute migration and state that boundary plainly.

After the observation window and separate teardown approval, check DNS/traffic, consumers, connections,
backups/restore and shared resources before removing anything. Never delete the AWS cluster, database,
registry, or shared network simply because one service moved.

Cleanup must include every resource actually created, with its owner and whether it is shared: Cloud Run
services/jobs/worker pools, image repositories, secrets/versions, service accounts, networking, databases,
logs and temporary artifacts. Deleting a Cloud Run service alone does not remove or stop charges for all
of them. Show proposed deletions; execute only the approved resource list.
