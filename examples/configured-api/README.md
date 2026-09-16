# A health check is not an application test

This small, dependency-free HTTP app makes a migration mistake visible:

- `/health` returns 200 whenever the process is running.
- `/api/check` requires `APP_ENV`, a real `DEMO_TOKEN`, and a matching `X-App-Token` header.
- Missing or redacted configuration returns 503. An incorrect application token returns 401.
- Responses and application logs never include the token.

Run the functional tests from the repository root:

```bash
python3 -m unittest tests.test_example -v
```

To rehearse container deployment, build the actual source for Cloud Run's architecture:

```bash
docker build --platform linux/amd64 -t configured-api examples/configured-api
```

Use a synthetic test token for local work. For a cloud rehearsal, have the skill provision the token in
Secret Manager, pin the returned version, deploy the image privately, and test both endpoints. The
private Cloud Run endpoint also needs an identity token; its `Authorization` header is separate from
the application's `X-App-Token` check. Pass credentials through process input or environment, not chat
or logged command arguments.

Successful tests demonstrate configuration and application authorization for this example only.
They do not certify a real application's database, queue, load behavior, or migration. This uses Python's
standard-library HTTP server for a small test fixture; it is not a production server recommendation.
