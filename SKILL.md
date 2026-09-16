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
Tell the user what was collected, how many secret-looking values (environment, labels, log
options, command flags) were redacted, and every denied call by name. A denied call is a finding,
not a gap to paper over.

## Phase 3: Assess

```
python3 scripts/assess.py --inventory inventory.json --src <source-dir> --out assessment.json
```
Omit `--src` if there is no source. Show the printed summary. Then, for each finding, restate it
in plain words using the row's `explain`, and show the `url` and `quote`.

Anything the rules do not recognize — a missing target-group protocol, a non-HTTP protocol, a UDP
port, a gRPC/HTTP2 app protocol, an ARM64 or Windows platform, a non-awslogs log driver, or any
task-definition field with no matching rule — is reported as needs-investigation, never skipped.
If a command-line argument was redacted as secret-looking, its command mapping is withheld and
flagged for review; tell the user they must re-supply that argument on Cloud Run themselves.

- Rollup **blocked**: explain what would unblock each blocked finding (a job, a worker pool, a
  supported CPU/memory pair, the missing AWS permission). Stop. No generation, no deploy.
- Rollup **needs-investigation**: list what the user must verify. Say that those items will be
  omitted from the manifest with `# OMITTED:` markers. Ask whether to continue.
- Rollup **supported**: ask whether to continue.

## Phase 4: Generate

```
python3 scripts/generate.py --assessment assessment.json --inventory inventory.json --project <project> --region <region> --out-dir out
```
`generate.py` refuses to run — and the run stops — if the image is an ECR Public image or if the
ECS service name cannot be turned into a valid Cloud Run name; show that refusal message
verbatim. Otherwise show `out/service.yaml` and `out/deploy.sh` in full. Walk through each
`deploy.sh` step's comment block. Ask whether to proceed.

## Phase 5: Staging deploy

Run `out/deploy.sh` **one step at a time, yourself**, not by executing the file. Every step's
command is self-contained — it carries its own `--project` and `--region` — so run each one as
printed in its own shell, with no environment setup beforehand. Before each step: say what it
does, what it costs, and wait for yes. On any failure: stop, show the error verbatim, show the
step's docs link. Never retry silently.

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
