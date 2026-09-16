# fargate-to-cloudrun: design

Date: 2026-09-16
Status: draft for review
License: Apache 2.0. Public GitHub repo. No Google or AWS marks in name or logo.

## 1. Purpose

An agent skill that assesses one Amazon ECS/Fargate service for Google Cloud Run
compatibility, explains every blocker with evidence from the official Cloud Run
documentation, and deploys a supported service to a Cloud Run staging project with
a smoke test.

The promise is narrow on purpose:

> Assess one ECS service for Cloud Run compatibility, explain blockers with
> documented evidence, and generate a staging deployment for a supported
> stateless HTTP workload.

Credibility rests on one rule: every verdict cites a specific page of the Cloud
Run documentation. The skill never answers a Cloud Run behavior question from web
search or model memory.

## 2. Decisions already made

| Decision | Value |
|---|---|
| v1 compute path | ECS/Fargate -> Cloud Run only. `scan.py` reports SDK usage of other AWS services (Lambda, SQS, DynamoDB, S3, ...) as out-of-scope findings; only the one ECS service is assessed and deployed. |
| Operator mode | Guided, always. Every operator is treated as a developer with no Google Cloud knowledge. |
| Execution depth | Assess + staging deploy. Production traffic, database moves, AWS teardown are detected and explained, never executed. |
| AWS input | Live account via aws CLI (read-only) plus the app source tree. |
| Agents | Claude Code, Gemini CLI, Codex. Standard SKILL.md format; no agent-specific tools in the workflow. |
| Repo | New public GitHub repo, Apache 2.0, synthetic fixtures only. |
| Packaging | Skill + standard-library Python 3 scripts (Approach 1). No CLI package, no MCP server in v1. |

## 3. Out of scope for v1

Detected and explained when found, never executed:

- AWS Lambda, SQS, SNS, EventBridge, DynamoDB, S3 as migration targets
- Database migration of any kind
- Production traffic switching, DNS, rollback of writes
- AWS decommissioning
- Multi-service applications in one run (one service per run)
- Terraform output (v1 emits Cloud Run service YAML plus a gcloud script)
- Image rebuilds (v1 copies the existing image)

## 4. Repo layout

```
fargate-to-cloudrun/
  SKILL.md                  # trigger, guided workflow, rules of engagement (<= ~300 lines)
  references/
    mapping.md              # ECS field -> Cloud Run field; one row each; docs URL + quote + snapshot date
    blockers.md             # every rule assess.py applies, with reasoning and citation
    glossary.md             # plain-language GCP concepts for guided mode
  scripts/
    inventory.py            # aws CLI -> inventory.json (redacted); --from-fixture <dir>
    scan.py                 # source tree -> sdk-usage.json
    assess.py               # inventory.json + sdk-usage.json -> assessment.json + assessment.md
    generate.py             # assessment.json + inventory.json -> service.yaml + deploy.sh
    smoke.sh                # URL + path -> 2xx check + last 20 log lines
    docdrift.py             # re-fetch cited pages, confirm quoted sentences still exist
  fixtures/
    stateless-http/         # supported; demo; the only fixture that deploys
    sidecar-datadog/        # needs-investigation
    efs-mount/              # needs-investigation
    sqs-worker/             # blocked (service) -> worker pools
    long-running-batch/     # blocked (service) -> jobs
    denied-permission/      # blocked: incomplete inventory
  tests/
    test_assess.py          # verdict equality per fixture + no-false-pass assertion
    test_docdrift.py        # network test, skipped offline
  README.md
  LICENSE
  NOTICE                    # CC-BY 4.0 attribution for adapted Google documentation
```

Rules:

- SKILL.md holds workflow and guided-mode rules only. Detail lives in references, read on demand.
- Scripts use Python 3 standard library only. No pip install. Each script takes explicit
  paths and writes JSON so the agent can run them in any order and fixtures can replace live calls.
- The scraped docs corpus (313 pages) is a working source, not shipped. What ships is the
  distilled mapping and blockers with a source URL, quoted sentence, and snapshot date on every row.
- Install path for all three agents: `npx skills add <github-url>`.

## 5. Guided workflow

Trigger: the user mentions moving an ECS or Fargate service to Cloud Run or Google Cloud.

Two invariants hold throughout:

1. Nothing writes to AWS. Only describe/list/get calls; never `get-secret-value` or
   `get-parameter --with-decryption`.
2. Nothing runs against Google Cloud without an explicit confirmation immediately before it.

### Phase 0: Preflight
- Check aws CLI present and authenticated (`sts get-caller-identity`). Report the identity.
- Check gcloud present and logged in. If missing: explain what gcloud is, link the install
  page from references, stop until ready.
- Confirm target Google Cloud project and region. If no project: walk through creation and
  billing enablement, state plainly that a staging deploy may cost money.

### Phase 1: Scope
- Ask for AWS account, region, and one ECS service (cluster + service name). One service per run.
- Ask for the path to that service's source. If unavailable, record it; every SDK-usage
  finding then downgrades to needs-investigation.

### Phase 2: Inventory
- Run `inventory.py`. Collects: service, task definition (all containers), target groups and
  listener rules, Secrets Manager / SSM references (names only), task role and execution role
  policy actions, EventBridge scheduled tasks referencing the task definition,
  VPC/subnet/security group ids, EFS volumes.
- Redaction: environment values are kept only when the key does not match a secret-like
  pattern (`(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential)`);
  otherwise the value is replaced with `"<redacted>"`.
- Every failed call is recorded as `{"call": ..., "error": "AccessDenied"}` in `inventory.json["denied"]`.
- Show the user: what was collected, what was redacted, what was denied.

### Phase 3: Scan
- Run `scan.py` over the source tree. Detects AWS SDK usage per language (boto3/botocore,
  @aws-sdk/*, aws-sdk, github.com/aws/aws-sdk-go*, software.amazon.awssdk, AWSSDK.*).
  Emits the set of AWS service clients constructed and the files/lines.
- Show the user which AWS services the code appears to call.

### Phase 4: Assess
- Run `assess.py`. Present findings grouped: supported, needs-investigation, blocked.
- Every finding shows: rule id, evidence (inventory path or file:line), docs URL, quoted sentence.
- If the rollup is `blocked`: explain what would unblock it and stop. No generation, no deploy.
- If the rollup is `needs-investigation`: list the items the user must verify, then ask whether to
  continue to a staging deploy. Items with no Cloud Run mapping in v1 (EFS volumes, extra containers)
  are omitted from the manifest with an explicit `# OMITTED:` marker and repeated in the closing report.

### Phase 5: Generate
- Run `generate.py`. Show `service.yaml` and `deploy.sh` in full.
- Explain each Google Cloud concept the first time it appears, from `glossary.md`.
- Confirm before proceeding.

### Phase 6: Staging deploy
- `deploy.sh` is a plain shell script with one command per numbered comment block. The agent runs
  each command itself, in order, rather than executing the file. Before each command: what it does,
  what it costs, wait for yes. The file is also runnable end to end by a human who has read it.
- Image copy is the first step that touches Google Cloud after API enablement. If the pull from ECR or
  the push to Artifact Registry fails, the run stops there with the error verbatim.
- Any failure: stop, show error verbatim, show docs link for that step. No silent retry.
- Run `smoke.sh`. Report URL, pass/fail with output, last 20 log lines.
- Closing report: URL, smoke result, remaining needs-investigation findings, the three things not
  done (production traffic, database, AWS teardown), and the exact gcloud command to delete the
  staging service.

## 6. Assessment rules and verdict model

### Grounding rule
Each mapping row and each blocker rule carries: docs URL, exact quoted sentence, snapshot date.
`assess.py` emits those three fields with every finding. The agent answers Cloud Run behavior
questions only from `references/`. If not covered there, the finding is needs-investigation with
reason `not covered by referenced documentation`; the agent says so and does not search the web.

### Verdicts
Per finding:

- `supported`: the ECS configuration maps to a documented Cloud Run feature with no documented caveat.
- `needs-investigation`: it maps, but the docs state a limitation the user must check against
  their app, or the source tree was unavailable so SDK usage could not be scanned.
- `blocked`: the docs state Cloud Run services do not do this, or an AWS inventory call was denied
  so the finding cannot be evaluated.

Split rule: denied AWS call -> `blocked`. Missing source tree -> `needs-investigation`.

Service rollup = worst finding. Missing evidence can never produce `supported`.

### Rule categories (v1)
Each rule sourced from the Cloud Run docs corpus:

1. Container shape: single vs multiple containers; PORT; image registry.
2. Resources: CPU and memory vs documented limits and allowed combinations.
3. Health checks: ECS container health check -> startup and liveness probes.
4. Networking: ALB listener rules / target group paths / private subnets -> ingress, Direct VPC egress, connectors.
5. Storage: EFS mounts -> NFS volumes; quote no-lock and mount-timeout caveats.
6. Secrets and config: Secrets Manager / SSM references -> Secret Manager.
7. Identity: **task role** policy actions grouped per AWS service; each group becomes a
   needs-investigation finding about code that still calls AWS. The **execution role** is out of
   scope for this rule: its actions (ECR pull, CloudWatch Logs, `secretsmanager:GetSecretValue`,
   `ssm:GetParameters`, `kms:Decrypt`) are ECS plumbing that Cloud Run replaces, not app dependencies.
8. Workload type: HTTP service / scheduled task / queue consumer -> service / job / worker pool.
   Non-HTTP shapes are `blocked` for a service and point at the correct resource type.
9. Timeouts and lifecycle: request timeout, stop timeout, scale-to-zero implications, CPU allocation.
10. SDK usage from scan: every AWS service the code calls, cross-checked against identity findings.

### Doc drift
`docdrift.py` is a maintainer tool, run by the repo owner or CI before a release. It is never run
during the guided workflow and the skill never invokes it. It re-fetches each cited page and confirms the quoted sentence still exists. If not,
the rule is marked stale in a generated `references/stale.json`; `assess.py` downgrades any finding
from a stale rule to needs-investigation with reason `citation stale, pending review`.

## 7. Generation, staging deploy, smoke test

### generate.py contract
`generate.py` targets the stateless HTTP shape only. It refuses to run on a `blocked` rollup. On
`needs-investigation` it emits the manifest with unmapped items replaced by `# OMITTED: <item> — see
finding <rule id>` comments. It performs no network calls and is fixture-replayable.

### Generated artifacts
- `service.yaml`: Cloud Run service manifest (documented YAML format). Image, PORT, CPU, memory,
  env vars, secret references, probes, min/max instances, concurrency, timeout, VPC egress if the
  inventory showed private subnets. Every value carries a comment naming its source ECS field.
- `deploy.sh`: numbered gcloud steps, one command per step, one-line comment each:
  enable APIs; create Artifact Registry repo; copy image ECR -> Artifact Registry; create service
  account; create Secret Manager secrets as empty shells; apply manifest
  (`gcloud run services replace service.yaml`); fetch URL.

### Image handling
Copy with local Docker or crane, whichever is present. No rebuild. Pullability is not assessed in
advance; the copy step in Phase 6 fails and stops the run with the error if the image cannot be pulled.

### Secrets
Secrets are created by name only. The user adds values through a documented gcloud command shown
to them. The skill never reads or moves secret values.

### Smoke test
`smoke.sh <url> <path>`: GET the ECS health-check path (or `/` if none), assert 2xx within the
documented startup window, then show the last 20 log lines. Pass/fail stated with output.

## 8. Fixtures and tests

### Fixture shape
```
fixtures/<name>/
  aws/inventory.json      # what inventory.py would have written
  src/                    # what scan.py reads
  expected.json           # verdicts assess.py must produce
```
`inventory.py --from-fixture <dir>` skips the aws CLI. Output format is identical to live runs.

### Fixtures
| Fixture | Contents | Expected rollup |
|---|---|---|
| stateless-http | one container, PORT, HTTP health check, env vars, one Secrets Manager ref, task role with no policy actions, execution role with ECR/logs/GetSecretValue | supported (demo; only fixture that deploys) |
| sidecar-datadog | two containers | needs-investigation (multi-container docs quoted) |
| efs-mount | EFS volume | needs-investigation (NFS no-lock, mount timeout quoted) |
| sqs-worker | no port, task role with sqs:ReceiveMessage, source calls SQS | blocked for service -> worker pools; needs-investigation for SQS SDK |
| long-running-batch | scheduled task, runs to completion, no port | blocked for service -> jobs |
| denied-permission | task-definition call denied | blocked: incomplete inventory |

### Tests
- `test_assess.py`: for every fixture, produced verdicts == `expected.json` exactly.
  Second assertion: no fixture other than `stateless-http` rolls up to `supported`.
- `test_docdrift.py`: network test, skipped when offline.

## 9. Validation plan and go criterion

Baseline arm: same agent, no skill, same ECS service and source, a link to the Cloud Run docs.

| Dimension | Values |
|---|---|
| Agents | Claude Code, Gemini CLI, Codex |
| Inputs | stateless-http fixture, sqs-worker fixture, one real service per pilot team |
| Arms | skill installed, no skill |

Measures per run:
- engineer minutes to a verified staging URL or a correct "do not deploy" stop
- blockers found vs expected, misses named
- manual corrections to generated files
- false passes (deployed or declared supported anything the fixture marks otherwise)
- docs grounding: every verdict cites a referenced page, not a web result

Pilots: three teams with a real ECS service they intend to move. Sanitized inventory and source suffice.

Go criterion (all three required):
1. Two independent teams complete the supported workflow with materially less engineer time than baseline.
2. Zero false passes across fixtures and pilots.
3. Every verdict grounded in a cited page.

Precondition before any pilot: written confirmation that the prior client engagement allows
publishing a tool derived from that experience, with none of its configuration or code included.

## 10. Licensing and attribution

- Repo: Apache 2.0.
- Adapted Google Cloud documentation: CC-BY 4.0 attribution in `NOTICE`, applying to adapted
  content as well as quotes. Google trademarks are excluded from that license and are not used
  in the project name or logo.
- Fixtures are synthetic. No client configuration or code.
