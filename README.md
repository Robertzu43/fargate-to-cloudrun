# fargate-to-cloudrun

An agent skill that assesses **one** Amazon ECS/Fargate service for Google Cloud Run, with every
verdict quoted from the Cloud Run documentation, and then guides a **private staging deploy**.

It runs in Claude Code, Gemini CLI, and Codex.

## What it does

1. **Inventory** your ECS service with read-only aws CLI calls. Secret-looking values
   (environment, labels, log options, command flags) are redacted; secret values are never read.
2. **Assess** each part of the task definition against 18 rules in `references/rules.json`. Every
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
43 tests. The suite asserts that every fixture yields its expected verdicts, that no incompatible
fixture ever rolls up to `supported`, that every Cloud Run finding carries a citation, and that
the generated deploy for the demo fixture is byte-identical to the golden files.

## Keeping citations honest

`scripts/docdrift.py` re-fetches every cited page weekly (GitHub Actions) and fails if a quoted
sentence is gone. Stale rules downgrade their findings until a maintainer reviews them.

## License

Apache 2.0. Quoted documentation is © Google, CC-BY 4.0; see `NOTICE`. Not affiliated with
Google or Amazon.
