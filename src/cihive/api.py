from __future__ import annotations

import asyncio
import hmac
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from cihive.config import Settings
from cihive.db import RunStore
from cihive.github import GitHubClient
from cihive.model import ModelAdapter, ModelError, create_model_adapter
from cihive.models import (
    AgentRequest,
    AgentResponse,
    AssignmentKind,
    AssignmentResultRequest,
    AssignmentStatus,
    AssignmentView,
    RunState,
    RunStatus,
    WebhookRunResponse,
    WorkerAssignment,
    safe_relative_path,
    utc_now,
)
from cihive.orchestrator import Orchestrator, create_failure_run
from cihive.security import (
    AuthenticationError,
    GitHubOIDCVerifier,
    IdentityVerifier,
    verify_capability,
    verify_webhook_signature,
)


def _ignore_reason(payload: dict[str, Any], settings: Settings) -> str | None:
    action = payload.get("action")
    if action != "completed":
        return "only completed workflow runs are accepted"
    repository = payload.get("repository") or {}
    workflow_run = payload.get("workflow_run") or {}
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}

    if bool(repository.get("private", False)):
        return "private repositories are not supported in v0.1"
    if not installation.get("id"):
        return "GitHub App installation is missing"
    if workflow_run.get("conclusion") not in {"failure", "timed_out"}:
        return "workflow run did not fail"
    default_branch = repository.get("default_branch")
    head_branch = workflow_run.get("head_branch")
    if not default_branch or head_branch != default_branch:
        return "only failures on the default branch are accepted"
    if str(head_branch).startswith("cihive/"):
        return "CIHive branches are ignored"
    if workflow_run.get("path") == settings.workflow_file:
        return "CIHive workflow failures are ignored"
    sender_login = str(sender.get("login") or "")
    if sender_login in {settings.github_app_slug, f"{settings.github_app_slug}[bot]"}:
        return "runs triggered by this GitHub App are ignored"
    return None


def create_app(
    settings: Settings | None = None,
    store: RunStore | None = None,
    github: GitHubClient | None = None,
    model: ModelAdapter | None = None,
    identity_verifier: IdentityVerifier | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_settings.ensure_directories()
    resolved_store = store or RunStore(
        resolved_settings.resolved_database_path,
        resolved_settings.data_dir,
    )
    resolved_github = github or GitHubClient(resolved_settings)
    resolved_model = model or create_model_adapter(resolved_settings)
    resolved_identity = identity_verifier or GitHubOIDCVerifier(resolved_settings)
    orchestrator = Orchestrator(
        resolved_settings,
        resolved_store,
        resolved_github,
        resolved_model,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def reap_stale_runs() -> None:
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(
                    resolved_store.expire_stale_runs,
                    resolved_settings.run_timeout_seconds,
                )

        reaper = asyncio.create_task(reap_stale_runs())
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper

    app = FastAPI(
        title="CIHive",
        version="0.1.0",
        description="Evidence-first distributed CI repair agents",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.store = resolved_store
    app.state.github = resolved_github
    app.state.model = resolved_model
    app.state.identity_verifier = resolved_identity
    app.state.orchestrator = orchestrator

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/webhooks/github", response_model=WebhookRunResponse, status_code=202)
    async def github_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> WebhookRunResponse:
        if not resolved_settings.github_webhook_secret:
            raise HTTPException(status_code=503, detail="webhook secret is not configured")
        body = await request.body()
        if len(body) > resolved_settings.max_webhook_bytes:
            raise HTTPException(status_code=413, detail="webhook payload is too large")
        if not verify_webhook_signature(
            body,
            x_hub_signature_256,
            resolved_settings.github_webhook_secret,
        ):
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        if not x_github_delivery:
            raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery")
        if not resolved_store.receive_delivery(x_github_delivery):
            return WebhookRunResponse(status="duplicate", reason="delivery was already processed")
        if x_github_event != "workflow_run":
            return WebhookRunResponse(status="ignored", reason="unsupported event type")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
        reason = _ignore_reason(payload, resolved_settings)
        if reason:
            return WebhookRunResponse(status="ignored", reason=reason)
        try:
            run = create_failure_run(payload, str(uuid.uuid4()))
            if not run.repo or not run.head_sha or not run.workflow_run_id:
                raise ValueError("workflow run payload is incomplete")
            state = RunState(run=run)
            resolved_store.create_run(state)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        resolved_store.append_trace(
            run.id,
            "webhook_received",
            {
                "delivery_id": x_github_delivery,
                "repo": run.repo,
                "workflow_run_id": run.workflow_run_id,
            },
        )
        background_tasks.add_task(orchestrator.process_run, run.id)
        return WebhookRunResponse(status="accepted", run_id=run.id)

    async def authenticated_assignment(
        request: Request,
        assignment_id: str,
    ) -> WorkerAssignment:
        capability = _bearer_token(request.headers.get("authorization"))
        try:
            assignment, capability_hash = resolved_store.get_assignment(assignment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="assignment not found") from exc
        if not verify_capability(capability, capability_hash):
            raise HTTPException(status_code=401, detail="invalid assignment capability")
        if assignment.expires_at <= utc_now():
            raise HTTPException(status_code=403, detail="assignment expired")
        try:
            await resolved_identity.verify(
                request.headers.get("x-github-oidc-token"),
                assignment,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return assignment

    def require_active_run(state: RunState) -> None:
        if state.status in {
            RunStatus.FAILED,
            RunStatus.DUPLICATE,
            RunStatus.NO_CANDIDATE,
            RunStatus.SUCCEEDED,
        }:
            raise HTTPException(status_code=409, detail="run is no longer active")

    @app.get("/v1/assignments/{assignment_id}", response_model=AssignmentView)
    async def get_assignment(request: Request, assignment_id: str) -> AssignmentView:
        assignment = await authenticated_assignment(request, assignment_id)
        state = resolved_store.get_run(assignment.run_id)
        require_active_run(state)
        if not state.failure or not state.plan or not state.repository_config:
            raise HTTPException(status_code=409, detail="run is not ready for workers")
        if assignment.status == AssignmentStatus.PENDING:
            assignment.status = AssignmentStatus.IN_PROGRESS
            resolved_store.save_assignment(assignment)
        candidates = []
        if assignment.kind == AssignmentKind.VERIFY:
            candidates = [
                item.candidate
                for item in resolved_store.list_assignments(
                    assignment.run_id,
                    AssignmentKind.REPAIR,
                )
                if item.candidate
            ]
        return AssignmentView(
            assignment=assignment,
            failure=state.failure,
            plan=state.plan,
            repository_config=state.repository_config,
            candidates=candidates,
        )

    @app.post(
        "/v1/assignments/{assignment_id}/agent",
        response_model=AgentResponse,
    )
    async def agent_proxy(
        request: Request,
        assignment_id: str,
        body: AgentRequest,
    ) -> AgentResponse:
        assignment = await authenticated_assignment(request, assignment_id)
        if assignment.kind != AssignmentKind.REPAIR:
            raise HTTPException(status_code=403, detail="agent proxy is for repair workers")
        if assignment.status == AssignmentStatus.COMPLETED:
            raise HTTPException(status_code=409, detail="assignment is already complete")
        if assignment.model_calls >= assignment.max_model_calls:
            raise HTTPException(status_code=429, detail="model call budget exhausted")
        encoded_files = sum(len(path) + len(content) for path, content in body.files.items())
        if encoded_files > resolved_settings.max_context_bytes:
            raise HTTPException(status_code=413, detail="repository snapshot is too large")
        for path, content in body.files.items():
            try:
                normalized = safe_relative_path(path)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if (
                normalized != path
                or len(content.encode("utf-8")) > resolved_settings.max_file_bytes
            ):
                raise HTTPException(status_code=422, detail=f"unsafe or oversized file: {path}")

        state = resolved_store.get_run(assignment.run_id)
        require_active_run(state)
        if not state.failure or not state.plan or not state.repository_config:
            raise HTTPException(status_code=409, detail="run is not ready for model calls")
        assignment.model_calls += 1
        resolved_store.save_assignment(assignment)
        try:
            proposal = await resolved_model.generate_repair(
                state.failure,
                state.plan,
                body.files,
                min(body.max_turns, state.repository_config.max_model_turns),
                feedback=body.feedback,
            )
        except ModelError as exc:
            resolved_store.append_trace(
                assignment.run_id,
                "model_error",
                {"assignment_id": assignment.id, "error": str(exc)[:1000]},
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        resolved_store.append_trace(
            assignment.run_id,
            "model_proposal",
            {
                "assignment_id": assignment.id,
                "summary": proposal.summary,
                "confidence": proposal.confidence,
            },
        )
        return AgentResponse(
            proposal=proposal,
            calls_remaining=max(0, assignment.max_model_calls - assignment.model_calls),
        )

    @app.post("/v1/assignments/{assignment_id}/result")
    async def assignment_result(
        request: Request,
        assignment_id: str,
        body: AssignmentResultRequest,
    ) -> dict[str, Any]:
        assignment = await authenticated_assignment(request, assignment_id)
        state = resolved_store.get_run(assignment.run_id)

        if assignment.kind == AssignmentKind.REPAIR and assignment.candidate:
            return {
                "status": "completed",
                "candidate": assignment.candidate.model_dump(mode="json"),
            }
        if assignment.kind == AssignmentKind.VERIFY and assignment.verification:
            return {
                "status": state.status.value,
                "run": state.model_dump(mode="json"),
            }

        require_active_run(state)

        if body.error:
            assignment.error = body.error[:4000]
            assignment.status = AssignmentStatus.FAILED
            resolved_store.save_assignment(assignment)
            resolved_store.append_trace(
                assignment.run_id,
                "assignment_failed",
                {"assignment_id": assignment.id, "error": assignment.error},
            )
            if assignment.kind == AssignmentKind.VERIFY:
                state.status = RunStatus.FAILED
                state.error = f"verification worker failed: {assignment.error}"
                resolved_store.save_run(state)
            return {"status": "failed", "error": assignment.error}

        if assignment.kind == AssignmentKind.REPAIR:
            if not body.candidate:
                raise HTTPException(status_code=422, detail="candidate payload is required")
            if not state.repository_config:
                raise HTTPException(status_code=409, detail="repository config is unavailable")
            try:
                candidate = orchestrator.submit_candidate(
                    assignment,
                    body.candidate,
                    state.repository_config,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"status": "completed", "candidate": candidate.model_dump(mode="json")}

        if not body.verification:
            raise HTTPException(status_code=422, detail="verification payload is required")
        finalized = await orchestrator.finalize(assignment, body.verification)
        return {
            "status": finalized.status.value,
            "run": finalized.model_dump(mode="json"),
        }

    @app.get("/v1/runs/{run_id}", response_model=RunState)
    async def get_run(request: Request, run_id: str) -> RunState:
        if resolved_settings.admin_token:
            supplied = _bearer_token(request.headers.get("authorization"))
            if not supplied or not hmac.compare_digest(supplied, resolved_settings.admin_token):
                raise HTTPException(status_code=401, detail="admin token required")
        try:
            return resolved_store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    return app


def _bearer_token(value: str | None) -> str | None:
    if not value or not value.lower().startswith("bearer "):
        return None
    return value[7:].strip()
