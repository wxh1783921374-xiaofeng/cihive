# Install CIHive

This guide targets GitHub.com public repositories and a self-hosted Linux control
plane. The expected first install is under ten minutes after you have a domain and
an OpenAI API key.

## 1. Deploy the control plane

```bash
cp .env.example .env
mkdir -p secrets data
# Save the GitHub App private key as secrets/github-app.pem after step 2.
docker compose up -d --build
```

Set these values in `.env`:

```text
CIHIVE_GITHUB_APP_ID=<app id>
CIHIVE_GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
CIHIVE_GITHUB_WEBHOOK_SECRET=<long random value>
CIHIVE_GITHUB_APP_SLUG=<app slug>
CIHIVE_CONTROL_PLANE_URL=https://cihive.example.com
OPENAI_API_KEY=<server-side key>
CIHIVE_REQUIRE_OIDC=true
```

Put a TLS reverse proxy in front of port 8000. GitHub must be able to reach:

```text
https://cihive.example.com/webhooks/github
```

Verify health:

```bash
curl https://cihive.example.com/healthz
```

## 2. Create the GitHub App

Use `github-app-manifest.json` as the reference and replace its example URL and
webhook URL. Configure:

- Webhook event: `workflow_run`
- Actions: read
- Contents: write
- Issues: write
- Metadata: read
- Pull requests: write

Save the generated private key as `secrets/github-app.pem`, put the App ID and
Webhook secret in `.env`, and restart the container.

Install the App only on repositories you intend to evaluate.

## 3. Install the repository workflow

From a repository checkout:

```bash
cihive init
```

Edit `.cihive.yml` so `verification_commands` reproduce and validate the failure.
`setup_commands` run in every fresh verification worktree before the test commands.
Commands are executed without a shell:

```yaml
version: 1
setup_commands:
  - ["python", "-m", "pip", "install", "-e", "."]
verification_commands:
  - ["python", "-m", "pytest", "-q"]
worker_count: 3
run_timeout_seconds: 1200
command_timeout_seconds: 600
max_patch_lines: 500
max_patch_files: 20
max_model_turns: 12
allow_draft_pr: true
forbidden_paths: []
```

CIHive always unions `forbidden_paths` with its mandatory protected paths.
Commit `.cihive.yml` and `.github/workflows/cihive.yml` to the default branch.

In repository variables, set:

```text
CIHIVE_CONTROL_PLANE_URL=https://cihive.example.com
CIHIVE_OIDC_AUDIENCE=cihive
CIHIVE_PACKAGE_SPEC=cihive==0.1.0
```

`CIHIVE_PACKAGE_SPEC` may point at a Git URL before the first PyPI release.

## 4. Run a safe test

Create a deliberately failing default-branch job whose fix is in source code, not
the workflow or test file. Push it, wait for the failure, and inspect:

```bash
cihive inspect <run-id>
```

The expected result is either a draft PR containing evidence, or a clean
`no_candidate` decision. CIHive must never create an automatic merge.

## Local development without OIDC

For an isolated local API test only:

```text
CIHIVE_REQUIRE_OIDC=false
```

Do not disable OIDC on a public endpoint. Fixture evaluation and unit tests do not
call GitHub or OpenAI:

```bash
cihive eval --suite evals/cases.yml
pytest
```
