# CIHive

CIHive is an evidence-first CI repair system for public GitHub repositories. When a
default-branch workflow fails, a self-hosted GitHub App turns the failure into a
bounded distributed task. Three ephemeral workers run in GitHub Actions, propose
independent patches, and a fresh verification job decides whether any candidate
actually fixed the failure.

CIHive opens a **draft pull request** only when the failure is reproduced and every
configured verification command passes. It never merges automatically and never
lets a repair worker hold repository write credentials or the model API key.

## Why this project

General-purpose agent frameworks are abundant. CIHive targets a narrower problem:

- GitHub Actions is the compute plane; there is no separate worker fleet.
- Test results, not model confidence, decide whether a patch is good.
- Multiple independent attempts reduce single-trajectory failure modes.
- Every decision has a replayable evidence trail.
- Untrusted logs and source files never become trusted instructions.

## Current scope

Version 0.1 targets:

- GitHub.com public repositories
- failures on the repository's default branch
- a self-hosted control plane backed by SQLite
- one GitHub App installation model
- OpenAI as the default model provider
- draft PR publishing with human review

Private repositories, GitHub Enterprise, a web dashboard, and automatic merging are
out of scope.

## Local development

Python 3.12, uv, and Node.js are required.

```bash
uv sync --extra dev
uv run pytest
uv run cihive eval --suite evals/cases.yml
```

On Windows PowerShell:

```powershell
uv sync --extra dev
uv run pytest
uv run cihive eval --suite evals\cases.yml
```

Start the control plane:

```bash
cp .env.example .env
cihive serve
```

The API is then available at `http://127.0.0.1:8000`, with health information at
`/healthz`.

## Install in a repository

After the GitHub App and control plane are reachable:

```bash
cihive init
```

This creates `.cihive.yml` and `.github/workflows/cihive.yml`. Commit both files to
the default branch, then enable the workflow in repository settings.

Set `setup_commands` for dependency installation inside every isolated verification
worktree, and `verification_commands` to the smallest command set that reproduces
and proves the failure.

See [docs/install.md](docs/install.md) for the GitHub App setup, required
permissions, secrets, and deployment steps.

## CLI

```text
cihive init [--directory PATH] [--force]
cihive serve [--host HOST] [--port PORT]
cihive run --repo OWNER/REPO --run-id RUN_ID
cihive inspect RUN_ID
cihive replay TRACE_PATH
cihive eval [--suite PATH]
```

`cihive run` is intended for local or scheduled operation with a fine-grained
`CIHIVE_GITHUB_TOKEN`. It is not used by the webhook path.

## Architecture

1. A `workflow_run.completed` webhook is signature-checked and persisted.
2. The control plane downloads and redacts failed job logs, creates a stable
   failure signature, and asks the configured model for bounded root-cause
   hypotheses.
3. The control plane creates three repair assignments and one verifier assignment.
   Each gets a short-lived capability token and must additionally prove its
   GitHub Actions identity with OIDC.
4. The repository workflow fans out three workers. Workers receive a read-only
   checkout, send a bounded source snapshot to the model proxy, and submit a patch.
5. A new verification job reproduces the original failure and applies each patch
   in an isolated worktree. CIHive rejects workflow, configuration, test, secret,
   binary, oversized, and unrelated-file edits.
6. The best passing candidate is published as a draft PR with the failure
   signature, command results, patch hash, and decision reason.

Detailed design and threat boundaries are in [docs/architecture.md](docs/architecture.md)
and [docs/security.md](docs/security.md).

## Model provider

The OpenAI adapter uses the official Python Agents SDK (`Agent`, `Runner`, and
function tools). The default model is configured by `CIHIVE_MODEL`; see the
[official Agents SDK quickstart](https://developers.openai.com/api/docs/guides/agents/quickstart)
for current SDK setup. Planning, model invocation, and GitHub publishing are
separate interfaces so another provider can be added without changing workflow
state or verification.

No OpenAI API key is required for unit tests or fixture evaluation.

## Safety

CIHive treats logs, repository contents, comments, and model output as untrusted.
It uses scoped capability tokens, GitHub OIDC verification, read-only worker jobs,
per-run model budgets, strict patch policy, fresh verification worktrees, and
draft-only publishing. Read [docs/security.md](docs/security.md) before enabling it
on a repository with secrets.

## License

Apache-2.0. See [LICENSE](LICENSE).
