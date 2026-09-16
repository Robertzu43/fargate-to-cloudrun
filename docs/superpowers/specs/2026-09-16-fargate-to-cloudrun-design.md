# fargate-to-cloudrun: design

Date: 2026-09-16
Status: draft for review (revision 2, after completeness and over-engineering reviews)
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
| v1 compute path | ECS/Fargate -> Cloud Run only. SDK usage of other AWS services (Lambda, SQS, DynamoDB, S3, ...) is reported as out-of-scope findings; only the one ECS service is assessed and deployed. |
| Operator mode | Guided, always. Every operator is treated as a developer with no Google Cloud knowledge. |
| Execution depth | Assess + staging deploy. Production traffic, database moves, AWS teardown are detected and explained, never executed. |
| AWS input | Live account via aws CLI (read-only) plus the app source tree. |
| Agents | Claude Code, Gemini CLI, Codex. Standard SKILL.md format; no agent-specific tools in the workflow. |
| Repo | New public GitHub repo, Apache 2.0, synthetic fixtures only. |
| Packaging | Skill + standard-library Python 3 scripts. No CLI package, no MCP server in v1. |

## 3. Out of scope for v1

Detected and explained when found, never executed:

- AWS Lambda, SQS, SNS, EventBridge, DynamoDB, S3 as migration targets
- Database migration of any kind
- Production traffic switching, DNS, rollback of writes
- AWS decommissioning
- Multi-service applications in one run (one service per run)
- Terraform output (v1 emits Cloud Run service YAML plus a gcloud script)
- Image rebuilds (v1 copies the existing image with Docker)
- Google Cloud project creation and billing setup (an existing project with billing is required)

## 4. Repo layout

```
fargate-to-cloudrun/
  SKILL.md                  # trigger, guided workflow, rules of engagement (<= ~300 lines)
  references/
    rules.json              # the single source of truth: every mapping row and blocker rule
  scripts/
    inventory.py            # aws CLI -> inventory.json (redacted)
    assess.py               # inventory.json + --src <dir> -> assessment.json
    generate.py             # assessment.json + inventory.json -> service.yaml + deploy.sh
    docdrift.py             # maintainer tool: re-fetch cited pages, flip `stale` on rows whose quote is gone
  fixtures/
    stateless-http/         # supported; demo; the only fixture that deploys
    sidecar-datadog/        # needs-investigation
    efs-mount/              # needs-investigation
    sqs-worker/             # blocked (service) -> worker pools
    long-running-batch/     # blocked (service) -> jobs
    denied-permission/      # blocked: incomplete inventory
  tests/
    test_assess.py          # verdict equality per fixture, no-false-pass, every finding cited
  .github/workflows/
    docdrift.yml            # weekly cron: run docdrift.py, fail on stale rows
  README.md
  LICENSE
  NOTICE                    # CC-BY 4.0 attribution for adapted Google documentation
```

Rules:

- SKILL.md holds workflow and guided-mode rules only. Detail lives in `rules.json`, read on demand.
- Scripts use Python 3 standard library only. No pip install. Each script takes explicit
  paths and writes JSON, so fixtures replace live calls by pointing `assess.py` at a fixture's
  `inventory.json`.
- The scraped docs corpus (313 pages) is a working source, not shipped. What ships is `rules.json`
  with a source URL, quoted sentence, and snapshot date on every row.
- Install path for all three agents: `npx skills add <github-url>`.

### rules.json row shape

```json
{
  "id": "storage.efs-volume",
  "category": "storage",
  "ecs_field": "volumes[].efsVolumeConfiguration",
  "cloudrun_field": "spec.template.spec.volumes[].nfs",
  "verdict": "needs-investigation",
  "explain": "Cloud Run can mount an NFS share such as Filestore, but without file locking.",
  "url": "https://docs.cloud.google.com/run/docs/configuring/services/nfs-volume-mounts",
  "quote": "Cloud Run does not support NFS locking. NFS volumes are automatically mounted in no-lock mode.",
  "snapshot": "2026-09-12",
  "stale": false
}
```

`explain` is the plain-language sentence the agent uses in guided mode. `verdict` is the
verdict this row produces when it matches; rows with `verdict: supported` are mapping rows,
the rest are blocker rules. One file, parsed by `assess.py`, read by the agent, walked by `docdrift.py`.

## 5. Guided workflow

Trigger: the user mentions moving an ECS or Fargate service to Cloud Run or Google Cloud.

Two invariants hold throughout:

1. Nothing writes to AWS. Only describe/list/get calls; never `get-secret-value` or
   `get-parameter --with-decryption`.
2. Nothing runs against Google Cloud without an explicit confirmation immediately before it.

Guided-mode rule: the first time a Google Cloud concept appears, the agent explains it in one or
two plain sentences and links the docs URL, using the `explain` and `url` fields of the matching
rule row or the comment on the matching deploy step. Never from memory, never from web search.

### Phase 1: Preflight
- aws CLI present and authenticated (`sts get-caller-identity`). Report account and identity.
  Region comes from the profile; ask only if unset.
- gcloud present and logged in. If missing: two sentences on what it is, link the install page, stop.
- Docker present. If missing: two sentences, link, stop.
- Target Google Cloud project with billing enabled. If none: two sentences, link the docs page, stop.
- Ask for: ECS cluster, service name, path to the service's source tree. One service per run.
  If source is unavailable, record it; SDK-usage findings then become needs-investigation.

### Phase 2: Inventory
- Run `inventory.py`. Collects: service, task definition (all containers), target groups and
  listener rules, Secrets Manager / SSM references (names only), task role and execution role
  policy actions, EventBridge scheduled tasks referencing the task definition, VPC and subnet ids,
  EFS volumes.
- Redaction: environment values are kept only when the key does not match
  `(?i)(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential)`; otherwise the value
  is replaced with `"<redacted>"`.
- Every failed call is recorded in `inventory.json["denied"]` as `{"call": ..., "error": ...}`.
- Show the user: what was collected, what was redacted, what was denied.

### Phase 3: Assess
- Run `assess.py --inventory inventory.json --src <dir>`. The source scan is part of this step:
  walk the tree, match AWS SDK imports and client constructors per language (boto3/botocore,
  @aws-sdk/*, aws-sdk, github.com/aws/aws-sdk-go*, software.amazon.awssdk, AWSSDK.*), record
  service name and file:line.
- Present findings grouped: supported, needs-investigation, blocked. Every finding shows rule id,
  evidence (inventory path or file:line), docs URL, quoted sentence.
- Rollup `blocked`: explain what would unblock it and stop. No generation, no deploy.
- Rollup `needs-investigation`: list the items the user must verify, then ask whether to continue.
  Items with no Cloud Run mapping in v1 (EFS volumes, extra containers) are omitted from the
  manifest with an explicit `# OMITTED:` marker and repeated in the closing report.
- Rollup `supported`: ask whether to continue.

### Phase 4: Generate
- Run `generate.py`. Show `service.yaml` and `deploy.sh` in full.
- Confirm before proceeding.

### Phase 5: Staging deploy
- `deploy.sh` is a plain shell script, one command per numbered comment block. The agent runs
  each command itself, in order, rather than executing the file. Before each command: what it
  does, what it costs, wait for yes. The file is also runnable end to end by a human who has read it.
- Any failure: stop, show the error verbatim, show the docs link from that step's comment. No silent retry.
- The last two steps are the smoke test: `curl -sf --retry` against the health path, then
  `gcloud run services logs read --limit 20`. Pass or fail is stated with the output.
- Closing report: URL, smoke result, remaining needs-investigation findings, every `# OMITTED:`
  item, the three things not done (production traffic, database, AWS teardown), and the exact
  gcloud command to delete the staging service.

## 6. Assessment rules and verdict model

### Grounding rule
Every row in `rules.json` carries URL, quoted sentence, and snapshot date. `assess.py` copies those
fields into every finding. The agent answers Cloud Run behavior questions only from `rules.json`.
If not covered there, the finding is needs-investigation with reason
`not covered by referenced documentation`; the agent says so and does not search the web.

### Verdicts
Per finding:

- `supported`: the ECS configuration maps to a documented Cloud Run feature with no documented caveat.
- `needs-investigation`: it maps, but the docs state a limitation the user must check against
  their app, or the source tree was unavailable so SDK usage could not be scanned.
- `blocked`: the docs state Cloud Run services do not do this, or an AWS inventory call was denied
  so the finding cannot be evaluated.

Split rule: denied AWS call -> `blocked`. Missing source tree -> `needs-investigation`.
Service rollup = worst finding. Missing evidence can never produce `supported`.
A row with `stale: true` downgrades its finding to needs-investigation with reason
`citation stale, pending review`.

### Rule categories (v1)
Each row sourced from the Cloud Run docs corpus:

1. Container shape: single vs multiple containers; PORT; image registry.
2. Resources: CPU and memory vs documented limits and allowed combinations.
3. Health checks: ECS container health check -> startup and liveness probes.
4. Networking: ALB listener rules / target group paths / private subnets -> ingress, Direct VPC egress, connectors.
5. Storage: EFS mounts -> NFS volumes; quote no-lock and mount-timeout caveats.
6. Secrets and config: Secrets Manager / SSM references -> Secret Manager.
7. AWS dependencies: for each AWS service named in (task-role policy actions ∪ SDK clients found
   in source), one needs-investigation finding whose evidence lists both sources. The execution
   role is excluded: its actions (ECR pull, CloudWatch Logs, `secretsmanager:GetSecretValue`,
   `ssm:GetParameters`, `kms:Decrypt`) are ECS plumbing that Cloud Run replaces.
8. Workload type: HTTP service / scheduled task / queue consumer -> service / job / worker pool.
   Non-HTTP shapes are `blocked` for a service and point at the correct resource type.
9. Timeouts: request timeout and stop timeout as field mappings. `desiredCount` -> `min-instances`
   is a mapping row whose `explain` carries the scale-to-zero and CPU-allocation note; it appears
   as a comment in `service.yaml`, not as a separate finding.

### Doc drift
`docdrift.py` is a maintainer tool. A weekly GitHub Actions cron runs it; it re-fetches each
cited page, sets `stale: true` on any row whose quoted sentence is gone, and exits nonzero if
anything changed. It is never run during the guided workflow and the skill never invokes it.

## 7. Generation and staging deploy

### generate.py contract
`generate.py` targets the stateless HTTP shape only. It refuses to run on a `blocked` rollup. On
`needs-investigation` it emits the manifest with unmapped items replaced by
`# OMITTED: <item> — see finding <rule id>` comments. It performs no network calls and is
fixture-replayable.

### Generated artifacts
- `service.yaml`: Cloud Run service manifest in the documented YAML format. Image, PORT, CPU,
  memory, env vars, secret references, probes, min/max instances, concurrency, timeout, VPC egress
  if the inventory showed private subnets. Every value carries a comment naming its source ECS
  field and the rule id it came from.
- `deploy.sh`: numbered steps, one command each. Each step's comment block holds a one-line
  purpose, a one-sentence plain-language explanation of the Google Cloud concept involved, and the
  docs URL. Steps: enable APIs; create Artifact Registry repo; `docker pull` from ECR,
  `docker tag`, `docker push` to Artifact Registry; create service account; create Secret Manager
  secrets as empty shells; `gcloud run services replace service.yaml`; fetch URL; curl smoke;
  tail logs.

### Image handling
Docker only. No rebuild. Pullability is not assessed in advance; the pull step fails and stops
the run with the error if the image cannot be pulled.

### Secrets
Secrets are created by name only. The user adds values through a documented gcloud command shown
to them. The skill never reads or moves secret values.

## 8. Fixtures and tests

### Fixture shape
```
fixtures/<name>/
  inventory.json          # what inventory.py would have written
  src/                    # what assess.py --src scans
  expected.json           # verdicts assess.py must produce
```
Fixtures need no flag: `assess.py --inventory fixtures/<name>/inventory.json --src fixtures/<name>/src`.

### Fixtures
| Fixture | Contents | Expected rollup |
|---|---|---|
| stateless-http | one container, PORT, HTTP health check, env vars, one Secrets Manager ref, task role with no policy actions, execution role with ECR/logs/GetSecretValue | supported (demo; only fixture that deploys) |
| sidecar-datadog | two containers | needs-investigation (multi-container docs quoted) |
| efs-mount | EFS volume | needs-investigation (NFS no-lock, mount timeout quoted) |
| sqs-worker | no port, task role with sqs:ReceiveMessage, source calls SQS | blocked for service -> worker pools; needs-investigation for SQS dependency |
| long-running-batch | scheduled task, runs to completion, no port | blocked for service -> jobs |
| denied-permission | task-definition call denied | blocked: incomplete inventory |

### Tests
`test_assess.py`, three assertions:
1. For every fixture, produced verdicts == `expected.json` exactly.
2. No fixture other than `stateless-http` rolls up to `supported`.
3. Every finding has a non-empty `url` and `quote`.

Doc drift has no test file; the cron's nonzero exit is the check.

## 9. Validation plan and go criterion

Baseline arm: same agent, no skill, same ECS service and source, a link to the Cloud Run docs.

**Portability check (untimed):** run the `stateless-http` and `sqs-worker` fixtures through the
guided workflow in each of the three agents once, to confirm the workflow, confirmations, and
explanations behave the same. Script output is already proven by `test_assess.py`; this checks
the agent-facing part only.

**Timed matrix:**

| Dimension | Values |
|---|---|
| Agents | Claude Code, Gemini CLI, Codex |
| Inputs | one real service per pilot team |
| Arms | skill installed, no skill |

Measures per run:
- engineer minutes to a verified staging URL or a correct "do not deploy" stop
- blockers found vs expected, misses named
- manual corrections to generated files
- false passes (deployed or declared supported anything the pilot's own review marks otherwise)
- agent grounding: every verdict the agent stated in prose cites a `rules.json` page, not a web
  result (this measures the agent's behavior; script output is covered by the test)

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
