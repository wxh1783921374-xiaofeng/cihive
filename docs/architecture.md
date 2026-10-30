# Architecture

CIHive is a control plane plus an installed GitHub Actions workflow. The control
plane owns trusted state, model credentials, and repository write access. Actions
runs are treated as disposable, untrusted compute.

## Failure flow

1. GitHub sends `workflow_run.completed`.
2. The API verifies `X-Hub-Signature-256`, persists the delivery ID, and applies
   repository/branch filters.
3. A background job downloads failed jobs and logs, redacts secret-shaped values,
   and computes a stable failure signature.
4. The model adapter proposes bounded root-cause hypotheses.
5. The control plane creates three repair assignments and one verifier
   assignment, each with a random capability token and a 30-minute expiry.
6. The GitHub App sends a `repository_dispatch` event. The installed workflow
   fans out three read-only jobs.
7. A repair worker sends a bounded source snapshot to the model proxy, receives a
   unified diff, applies it in its temporary checkout, and submits the diff plus
   final contents.
8. The verifier runs configured commands on the unmodified checkout. If they do
   not reproduce the failure, no candidate can pass. Otherwise it creates a fresh
   worktree per candidate, applies the patch, and runs every command again.
9. The control plane applies patch policy and selects the passing candidate with
   the fewest changed lines. It creates a blob/tree/commit/ref through the GitHub
   API and opens a draft PR.

## Trust boundaries

| Component | Trust | Capabilities |
| --- | --- | --- |
| Webhook delivery | Untrusted until HMAC verified | Can create one run |
| Repository/tool logs | Untrusted data | Bounded model context and read-only tools |
| Repair worker | Semi-trusted | Read checkout, call one scoped model proxy, submit result |
| Verifier worker | Semi-trusted | Read candidates, create temporary worktrees, submit evidence |
| Control plane | Trusted | GitHub App key, OpenAI key, draft PR publication |
| GitHub Actions runner | Ephemeral | No repository write token |

Capability tokens prevent accidental cross-run access. GitHub OIDC binds each
request to the expected repository, default branch, and installed workflow; a
token copied from a public dispatch payload is insufficient on its own.

## State

SQLite tables store deliveries, runs, assignments, status, and serialized
Pydantic payloads. Artifacts and JSONL traces live below `CIHIVE_DATA_DIR`.
SQLite WAL allows reads while the background worker updates a run.

The run state machine is:

```text
queued -> processing -> waiting -> succeeded
                              \-> no_candidate
      \-> duplicate
      \-> failed
```

Webhook retries are idempotent by delivery ID. A second failure with the same
repository, commit, and normalized failure signature does not dispatch new
workers.

## Deliberate v0.1 limits

- GitHub.com public repositories only.
- Default-branch failures only; PR-triggered failures are not trusted inputs.
- One control-plane process and SQLite.
- Three workers by default; no dynamic fleet scheduler.
- Draft PRs only; no merge endpoint exists in the implementation.
- No web dashboard.
