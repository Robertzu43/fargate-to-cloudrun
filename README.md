# fargate-to-cloudrun

**An agent skill that checks whether one Amazon ECS/Fargate service can run on Google Cloud Run,
proves every claim with a quoted sentence from the Cloud Run documentation, and then walks you
through a private staging deploy one confirmed step at a time.**

It runs inside Claude Code, Gemini CLI, or Codex. You do not need to know Google Cloud. The skill
explains each concept the first time it appears and asks before it creates anything.

---

## What it does, in one picture

```
 your AWS account (read-only)          your source code
        │                                     │
        ▼                                     ▼
 1. inventory.py  ──► inventory.json  (secrets redacted, denied calls recorded)
                                │
                                ▼
 2. assess.py     ──► assessment.json  every finding: supported | needs-investigation | blocked
                                │       + the docs URL and quoted sentence that justifies it
                                ▼
 3. generate.py   ──► out/service.yaml + out/deploy.sh   (only when nothing is blocked)
                                │
                                ▼
 4. you + the agent run deploy.sh one step at a time, saying yes to each
                                │
                                ▼
        a private Cloud Run service in your staging project, smoke-tested
```

### What it will never do

- Write anything to AWS. Only `describe`, `list`, and `get` calls, and it never reads a secret value.
- Create, change, or delete anything on Google Cloud without asking you first, step by step.
- Make your service public. The smoke test uses your own identity token.
- Answer a question about Cloud Run from memory or from a web search. If the referenced
  documentation does not cover it, it says so.
- Migrate a database, switch production traffic, or tear down AWS. Those are detected and explained,
  never executed.

---

## Requirements

On the machine where your agent runs:

| Tool | Why | Check |
|---|---|---|
| Python 3.9+ | the three scripts (standard library only, nothing to pip install) | `python3 --version` |
| aws CLI, logged in | read your ECS service | `aws sts get-caller-identity` |
| gcloud, logged in | deploy to Cloud Run | `gcloud auth list` |
| A Google Cloud project with billing enabled | where the staging service lands | `gcloud billing projects describe <project>` |
| Docker | only if your image lives in ECR (it gets copied to Artifact Registry) | `docker --version` |

The AWS identity needs read access to ECS, ELBv2, IAM (roles and policies), and EventBridge rules.
The Google account needs Owner on the staging project. Use a fresh project for staging.

---

## Install

**Option A: one command, any supported agent**

```bash
npx skills add Robertzu43/fargate-to-cloudrun
```

Add `-g` to install for all your projects instead of the current one, and `-a claude-code`,
`-a gemini-cli`, or `-a codex` to target one agent. This copies the whole repo (skill, scripts,
rules, fixtures) into your agent's skills folder.

**Option B: manual**

```bash
# Claude Code (project-level; use ~/.claude/skills/ for all projects)
git clone https://github.com/Robertzu43/fargate-to-cloudrun .claude/skills/fargate-to-cloudrun

# Gemini CLI and Codex (shared folder; use ~/.agents/skills/ for all projects)
git clone https://github.com/Robertzu43/fargate-to-cloudrun .agents/skills/fargate-to-cloudrun
```

Then start your agent in the project that holds your service's source code.

---

## Use it, step by step

Open your agent in the directory that contains the source code of the service you want to move,
then say something like:

> Assess my ECS service `web` in cluster `prod` for Cloud Run and take me through a staging deploy.

The skill takes over from there. This is what happens, and what you will be asked.

### Step 1: Preflight

The agent checks aws, gcloud, your Google Cloud project and billing, and Docker if needed. If
anything is missing it explains what the tool is, links the install page, and stops until it is
ready. It then asks for three things:

1. the ECS **cluster** name
2. the ECS **service** name
3. the path to the service's **source code** (say "none" if you do not have it; the assessment
   then flags every AWS SDK dependency as needing your review)

### Step 2: Inventory (read-only)

```
python3 $SKILL_DIR/scripts/inventory.py --cluster prod --service web --out inventory.json
```

The agent runs this and tells you what was collected, how many secret-looking values were
redacted (environment variables, Docker labels, log-driver options, command-line flags), and
every AWS call that was denied. A denied call is a finding, not something to paper over.

### Step 3: Assessment

```
python3 $SKILL_DIR/scripts/assess.py --inventory inventory.json --src . --out assessment.json
```

You get a summary grouped into three verdicts. Every line shows the evidence from your task
definition, the Cloud Run docs URL, and the exact sentence quoted from that page.

| Verdict | Meaning | What happens next |
|---|---|---|
| **supported** | maps to a documented Cloud Run feature with no caveat | continue |
| **needs-investigation** | maps, but the docs state a limitation you must check, or evidence was incomplete | the agent lists what to verify, tells you what will be left out of the manifest, and asks whether to continue |
| **blocked** | Cloud Run services do not do this, or an AWS call was denied | the agent explains what would unblock it and **stops** |

Anything in your task definition the rules do not recognize is reported as needs-investigation,
never skipped. The overall result is the worst verdict of any finding. Missing evidence can never
produce "supported".

Common results and what they mean:

- **`workload.background` blocked**: no port exposed. On Cloud Run this is a worker pool, not a
  service. v1 only deploys services.
- **`workload.scheduled` blocked**: a scheduled task. On Cloud Run this is a job plus Cloud
  Scheduler.
- **`container.architecture` blocked**: ARM64 (Graviton) or Windows. Cloud Run runs Linux x86_64 only.
- **`deps.aws-service` needs-investigation**: your code or task role talks to SQS, S3, DynamoDB
  and so on. Cloud Run runs as a Google service account with no AWS credentials; each dependency
  needs its own plan.
- **`storage.efs` needs-investigation**: an EFS volume. Cloud Run can mount NFS, but there is no
  Filestore yet, so the volume is left out of the staging manifest.

### Step 4: Generate

```
python3 $SKILL_DIR/scripts/generate.py --assessment assessment.json --inventory inventory.json \
  --project my-staging-project --region us-central1 --out-dir out
```

Two files appear, and the agent shows you both in full:

- `out/service.yaml`: the Cloud Run manifest. Every value carries a comment naming the ECS field
  it came from and the rule that allowed it. Anything not supported appears as an `# OMITTED:` line.
- `out/deploy.sh`: numbered steps. Each step has a plain-language explanation and a docs link,
  names its own `--project` and `--region`, and is safe to re-run.

The generator refuses, and the run stops, when the rollup is blocked, the project id or region is
invalid, the image is on ECR Public, or the service name cannot become a valid Cloud Run name.

### Step 5: Staging deploy, one step at a time

The agent runs each step of `out/deploy.sh` itself and asks you before every one. The full path
for an image in ECR is:

1. enable the Cloud Run, Artifact Registry and Secret Manager APIs
2. create the Artifact Registry repository
3. log Docker in to ECR (read-only: a pull token)
4. let Docker push to Artifact Registry
5. copy the image (pull, tag, push; never rebuilt)
6. create the runtime service account
7. create each Secret Manager secret, empty
8. grant the service account access to each secret
9. **PAUSE**: you add each secret value yourself with the printed `gcloud secrets versions add`
   command. The agent never sees the values.
10. deploy from `out/service.yaml`
11. print the service URL
12. smoke test with your identity token (the service stays private)
13. show the last 20 log lines

Steps 2 to 5 are skipped when the image is already in a registry Cloud Run can pull from. Any
failure stops the run and shows the error verbatim with the docs link for that step.

### Step 6: Closing report

You get the URL, the smoke result, every item still marked needs-investigation or omitted, the
three things not done by design (production traffic, database, AWS teardown), and the exact
command to delete the staging service so you stop paying for it. Making the service public is a
separate, explained confirmation if you want it.

---

## Try it without an AWS account

Six synthetic services live under `fixtures/`. From the skill directory:

```bash
python3 scripts/assess.py --inventory fixtures/sqs-worker/inventory.json --src fixtures/sqs-worker/src --out /tmp/a.json
```

You will see a `blocked` rollup with the worker-pool finding and its citation. `stateless-http`
is the one fixture that rolls up `supported`; its generated deploy script is frozen under
`fixtures/stateless-http/golden/`.

---

## How the credibility works

- `references/rules.json` holds 18 rules. Each has the ECS field, the Cloud Run field, the
  verdict, a plain-language explanation, the docs URL, the exact quoted sentence, and the snapshot
  date. The assessor copies the URL and quote into every finding it derives from a rule.
- A weekly GitHub Actions job (`scripts/docdrift.py`) re-fetches every cited page and fails if a
  quoted sentence is gone. A rule marked stale downgrades its findings until a maintainer reviews it.
- The test suite (44 tests, `python3 -m unittest tests.test_assess -v`) asserts that every fixture
  yields its expected verdicts, that no incompatible fixture ever rolls up `supported`, that every
  Cloud Run finding carries a citation, and that the demo deploy script is byte-identical to the
  golden file.

## Scope of v1

In: one ECS/Fargate service at a time, HTTP workloads, staging deploy, private by default.
Out, by design: Lambda, queues and event buses, databases, production cutover, AWS teardown,
multi-service apps, Terraform output.

## License

Apache 2.0. Quoted documentation is copyright Google, CC-BY 4.0; see `NOTICE`. "Fargate" and
"Cloud Run" are used descriptively. Not affiliated with or endorsed by Amazon or Google.
