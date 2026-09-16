# fargate-to-cloudrun: design

Date: 2026-09-16
Status: ready for user review (revision 4)
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
    test_assess.py          # verdict equality, no-false-pass, citation on Cloud Run findings, golden generate
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
2. Nothing that creates, changes, or deletes anything on Google Cloud runs without an explicit
   confirmation immediately before it. Read-only preflight calls (`gcloud auth list`,
   `gcloud config get project`, `gcloud billing projects describe`) need no confirmation.

Guided-mode rule: the first time a Google Cloud concept appears, the agent explains it in one or
two plain sentences and links the docs URL. Three sources, in this order: the `explain` and `url`
fields of the matching rule row; the comment on the matching deploy step; and, for preflight
concepts that precede both (what gcloud is, what a project with billing is), a short fixed
paragraph with its docs URL in SKILL.md. Never from memory, never from web search.

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
- Present findings grouped: supported, needs-investigation, blocked. Every finding shows its
  evidence (inventory path or file:line). Findings from a rule row also show rule id, docs URL,
  and quoted sentence. Findings with a `reason` show the reason instead.
- Rollup `blocked`: explain what would unblock it and stop. No generation, no deploy.
- Rollup `needs-investigation`: list the items the user must verify, then ask whether to continue.
  Every finding whose verdict is not `supported` is omitted from the manifest with an explicit
  `# OMITTED:` marker and repeated in the closing report, regardless of whether its rule row has a
  `cloudrun_field`. (An EFS row maps to an NFS volume in the docs, but v1 still omits it: there is
  no Filestore to point at.)
- Rollup `supported`: ask whether to continue.

### Phase 4: Generate
- Run `generate.py`. Show `service.yaml` and `deploy.sh` in full.
- Confirm before proceeding.

### Phase 5: Staging deploy
- `deploy.sh` is a plain shell script, one command per numbered comment block. The agent runs
  each command itself, in order, rather than executing the file. Before each command: what it
  does, what it costs, wait for yes. The file is also runnable end to end by a human who has read it.
- Any failure: stop, show the error verbatim, show the docs link from that step's comment. No silent retry.
- The last two steps are the smoke test: `curl -sf --retry` against the ECS HTTP health-check
  path, or `/` when the ECS health check is not an HTTP path or is absent, then
  `gcloud run services logs read --limit 20`. Pass or fail is stated with the output.
- Closing report: URL, smoke result, remaining needs-investigation findings, every `# OMITTED:`
  item, the three things not done (production traffic, database, AWS teardown), and the exact
  gcloud command to delete the staging service.

## 6. Assessment rules and verdict model

### Grounding rule
Every row in `rules.json` carries URL, quoted sentence, and snapshot date. `assess.py` copies those
fields into every finding it derives from a row. The agent answers Cloud Run behavior questions
only from `rules.json`. If the user asks about something no row covers, the agent says
"not covered by the referenced documentation" and does not search the web.

### Unmatched ECS fields
`assess.py` walks every field of the task definition and service. A field that matches no rule
row and is not on the ignore list produces a needs-investigation finding with reason `not-covered`
and the field path as evidence. `rules.json` has a top-level `ignore` array of field paths that
are known to have no Cloud Run consequence (for example `registeredAt`, `tags`, `family`).
Silently skipping a field is never allowed; this is the second half of "missing evidence can
never produce `supported`".

### Reason enumeration
`reason` is one of four slugs. Findings with the first three come from no rule row and carry no
`url`/`quote`; `stale` comes from a row and keeps both.

| reason | verdict | source |
|---|---|---|
| `denied` | blocked | AWS inventory call refused |
| `source-unavailable` | needs-investigation | no source tree to scan |
| `not-covered` | needs-investigation | ECS field matches no row and is not ignored |
| `stale` | needs-investigation | a `supported` row whose citation failed drift |

### Verdicts
Per finding:

- `supported`: the ECS configuration maps to a documented Cloud Run feature with no documented caveat.
- `needs-investigation`: it maps, but the docs state a limitation the user must check against
  their app, or the source tree was unavailable so SDK usage could not be scanned.
- `blocked`: the docs state Cloud Run services do not do this, or an AWS inventory call was denied
  so the finding cannot be evaluated.

Split rule: denied AWS call -> `blocked`. Missing source tree -> `needs-investigation`.
Service rollup = worst finding. Missing evidence can never produce `supported`.

A `supported` row with `stale: true` downgrades its finding to needs-investigation with reason
`stale`. Stale never changes a `blocked` or `needs-investigation` row; those stay as they are
until the maintainer reviews them. See the reason enumeration above for the full set.

### Rule categories (v1)
Each row sourced from the Cloud Run docs corpus:

1. Container shape: single vs multiple containers; PORT; image registry.
2. Resources: CPU and memory vs documented limits and allowed combinations.
3. Health checks: ECS container health check -> startup and liveness probes.
4. Networking: ALB listener rules / target group paths -> ingress. Private subnets -> a
   needs-investigation finding citing the Direct VPC egress page; v1 has no Google Cloud VPC to
   target, so `generate.py` emits it as `# OMITTED`.
5. Storage: EFS mounts -> NFS volumes; quote no-lock and mount-timeout caveats.
6. Secrets and config: Secrets Manager / SSM references -> Secret Manager.
7. AWS dependencies: for each AWS service named in (task-role policy actions ∪ SDK clients found
   in source), one needs-investigation finding whose evidence lists both sources. The execution
   role is excluded: its actions (ECR pull, CloudWatch Logs, `secretsmanager:GetSecretValue`,
   `ssm:GetParameters`, `kms:Decrypt`) are ECS plumbing that Cloud Run replaces.
8. Workload type: HTTP service / scheduled task / queue consumer -> service / job / worker pool.
   Non-HTTP shapes are `blocked` for a service and point at the correct resource type.
9. Scaling and timeout: `desiredCount` -> `min-instances` is an ordinary mapping row that produces
   a `supported` finding; its `explain` carries the scale-to-zero and CPU-allocation note, which
   `generate.py` folds into the comment on that line. Request timeout has no ECS source field in
   the v1 inventory; `generate.py` emits the documented default (the docs: "The timeout is set by
   default to 5 minutes (300 seconds) and can be extended up to 60 minutes") with that citation
   as the comment. ALB idle timeout is not collected in v1.

### Doc drift
`docdrift.py` is a maintainer tool. A weekly GitHub Actions cron runs it; it re-fetches each
cited page and exits nonzero, printing the affected row ids, if any quoted sentence is gone. It
writes nothing in CI; the maintainer sets `stale: true` on those rows in a commit and reviews them.
Run locally with `--write` it flips the flags in place. It is never run during the guided workflow
and the skill never invokes it.

## 7. Generation and staging deploy

### generate.py contract
`generate.py` targets the stateless HTTP shape only. It refuses to run on a `blocked` rollup.
It emits manifest values only for findings whose verdict is `supported`; every other finding
becomes a `# OMITTED: <item> — see finding <rule id>` comment, regardless of its `cloudrun_field`.
It performs no network calls and is fixture-replayable.

### Generated artifacts
- `service.yaml`: Cloud Run service manifest in the documented YAML format. Image, PORT, CPU,
  memory, env vars, secret references, probes, min/max instances, concurrency, timeout. Every
  value carries a comment naming its source ECS field and the rule id it came from.
- `deploy.sh`: numbered steps, one confirmation per step. A step may hold more than one shell
  command when they are inseparable (the three docker commands, a check-then-create). Each step's
  comment block holds a one-line purpose, a one-sentence plain-language explanation of the Google
  Cloud concept involved, and the docs URL. Steps, in order:
  1. enable APIs (run, artifactregistry, secretmanager)
  2. create Artifact Registry repo (check-then-create; re-runs must not dead-end)
  3. `aws ecr get-login-password | docker login` to ECR (read-only AWS; obtains a pull token)
  4. `gcloud auth configure-docker <region>-docker.pkg.dev` (edits local Docker config only)
  5. `docker pull` from ECR, `docker tag`, `docker push` to Artifact Registry
  6. create the runtime service account (check-then-create)
  7. create each Secret Manager secret as an empty shell (check-then-create)
  8. grant the runtime service account `roles/secretmanager.secretAccessor` on each secret
     (the docs: "To allow Cloud Run to access the secret, the service identity must have the
     following role: Secret Manager Secret Accessor")
  9. **pause**: print the `gcloud secrets versions add <name> --data-file=-` command for each
     secret and wait until the user confirms every value is in place. The manifest pins each
     reference to version `1`, not `latest` (the docs: environment-variable secrets "are resolved
     at instance startup time", and Google recommends pinning a version). The agent never
     sees or handles the values.
  10. `gcloud run services replace service.yaml`
  11. fetch URL
  12. curl smoke, authenticated: `curl -sf --retry 5 -H "Authorization: Bearer $(gcloud auth
      print-identity-token)" <url><path>`. The staging service stays private; no `allUsers`
      invoker grant. The docs require the Cloud Run Invoker role for callers, and the deploying
      user already holds it as project owner. If the user wants a public URL, that is a separate,
      explained confirmation after the smoke test, not part of deploy.sh.
  13. `gcloud run services logs read --limit 20`

  Every step that creates a Google Cloud resource is idempotent on re-run (describe, then create
  only if absent), so a second staging attempt never stops on "already exists".

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
  golden/                 # stateless-http only: expected service.yaml + deploy.sh
```
Fixtures need no flag: `assess.py --inventory fixtures/<name>/inventory.json --src fixtures/<name>/src`.

### Fixtures
| Fixture | Contents | Expected rollup |
|---|---|---|
| stateless-http | one container, PORT, HTTP health check, env vars, one Secrets Manager ref, task role with no policy actions, execution role with ECR/logs/GetSecretValue. Image is Google's public sample `us-docker.pkg.dev/cloudrun/container/hello`, so steps 3 and 5 are skipped for this fixture and the golden `deploy.sh` reflects that. | supported (demo; only fixture that deploys) |
| sidecar-datadog | two containers | needs-investigation (multi-container docs quoted) |
| efs-mount | EFS volume | needs-investigation (NFS no-lock, mount timeout quoted) |
| sqs-worker | no port, task role with sqs:ReceiveMessage, source calls SQS | blocked for service -> worker pools; needs-investigation for SQS dependency |
| long-running-batch | scheduled task, runs to completion, no port | blocked for service -> jobs |
| denied-permission | task-definition call denied | blocked: incomplete inventory |

### Tests
`test_assess.py`, four assertions:
1. For every fixture, produced verdicts == `expected.json` exactly.
2. No fixture other than `stateless-http` rolls up to `supported`.
3. Every finding without a `reason` in `{denied, source-unavailable, not-covered}` has a
   non-empty `url` and `quote`.
4. `generate.py` on `stateless-http` produces `service.yaml` and `deploy.sh` byte-identical to
   `fixtures/stateless-http/golden/`. This pins the step order, including the IAM grant and the
   secret-version pause before the replace.

Doc drift has no test file; the cron's nonzero exit is the check.

## 9. Validation plan and go criterion

Baseline arm: same agent, no skill, same ECS service and source, a link to the Cloud Run docs.

**Portability check (untimed):** run the `stateless-http` and `sqs-worker` fixtures through the
guided workflow in each of the three agents once, starting at Phase 3 with the fixture's
`inventory.json` (SKILL.md carries a one-line replay instruction for this). The run proceeds
through Phase 4 and stops by declining the first deploy.sh confirmation; no Google Cloud resource
is created during the portability check. Confirms the workflow, confirmations, and explanations
behave the same. Script output is already proven by
`test_assess.py`; this checks the agent-facing part only.

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
