# Security model

CIHive handles attacker-controlled logs and source code, so the security boundary
is explicit: model output is a proposal, and repository publication is a separate
privileged operation.

## Required controls

- Keep `CIHIVE_REQUIRE_OIDC=true` outside isolated tests.
- Use a unique Webhook secret and a separate GitHub App private key.
- Run the control plane with the supplied non-root container.
- Give the App only Actions read, Contents write, Issues write, Metadata read, and
  Pull requests write.
- Keep OpenAI credentials only in the control plane.
- Back up `CIHIVE_DATA_DIR`; it contains redacted evidence and run state.
- Set `CIHIVE_ADMIN_TOKEN` if `GET /v1/runs/{id}` is exposed beyond localhost.

## Prompt-injection defenses

- Logs and repository files are labeled as untrusted data in model instructions.
- Repair workers have three read-only tools: list files, read a file, and search a
  bounded snapshot.
- Workers cannot ask the model to fetch URLs or execute shell commands.
- Model output has a strict Pydantic `RepairProposal` shape.
- The control plane, not the model, decides file policy, verification, scoring, and
  publication.
- Test and workflow edits are rejected even if a candidate claims they are needed.

## Patch policy

The following are blocked by default:

- workflows, CIHive configuration, secrets, environment files, keys, and certificates
- test files, test directories, and fixtures
- binary patches, file deletions, and submodule changes
- more than 500 changed lines, 20 files, 1 MB of patch, or 512 KB per file
- final file contents that do not exactly correspond to the submitted diff

A candidate is never accepted solely because the model reports high confidence.
The original failure must be reproduced and every configured command must pass in
a fresh worktree.

## Data handling

Workflow logs are redacted for common token, password, private-key, and API-key
patterns before model input or local artifact persistence. Redaction is
best-effort; GitHub masking is not a substitute for avoiding secrets in public CI
logs. Public repository source is sent to the configured model provider.

## Residual risks

- A malicious public repository can attempt to exhaust model or Actions budgets.
- Configured verification commands are trusted code from the default branch.
- Model providers may process public source under their own terms.
- GitHub-hosted runners are isolated for a job but have normal network access.
- The MVP is not a multi-tenant hardened SaaS and should not be exposed as one
  without rate limiting, tenant isolation, and an external database.

Report vulnerabilities privately to the repository maintainers rather than opening
a public issue containing exploit details.
