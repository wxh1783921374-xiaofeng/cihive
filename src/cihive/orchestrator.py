from __future__ import annotations

import uuid
from typing import Any

from cihive.config import Settings
from cihive.db import RunStore
from cihive.failure import build_failure_context, logs_zip_to_text, parse_jobs
from cihive.github import GitHubClient, GitHubError
from cihive.model import ModelAdapter, ModelError
from cihive.models import (
    AssignmentKind,
    AssignmentStatus,
    CandidatePatch,
    CandidateSubmission,
    CommandResult,
    Decision,
    FailureRun,
    RepositoryConfig,
    RunState,
    RunStatus,
    VerificationBundle,
    WorkerAssignment,
)
from cihive.patch import patch_sha256, validate_patch
from cihive.security import assignment_expiry, create_capability_token


class OrchestrationError(RuntimeError):
    pass


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: RunStore,
        github: GitHubClient,
        model: ModelAdapter,
    ) -> None:
        self.settings = settings
        self.store = store
        self.github = github
        self.model = model

    async def process_run(self, run_id: str) -> RunState:
        state = self.store.get_run(run_id)
        state.status = RunStatus.PROCESSING
        self.store.save_run(state)
        self.store.append_trace(
            run_id,
            "processing_started",
            {"workflow_run_id": state.run.workflow_run_id},
        )
        try:
            jobs_payload = await self.github.list_jobs(
                state.run.repo,
                state.run.workflow_run_id,
                state.run.installation_id,
            )
            failed_jobs = parse_jobs(jobs_payload)
            logs_bytes = await self.github.download_logs(
                state.run.repo,
                state.run.workflow_run_id,
                state.run.installation_id,
            )
            if len(logs_bytes) > self.settings.max_log_bytes:
                raise ValueError("workflow log archive exceeds the configured size limit")
            logs = logs_zip_to_text(logs_bytes, self.settings.max_log_bytes)
            failure = build_failure_context(
                state.run,
                logs,
                failed_jobs,
                self.settings.max_log_chars,
            )
            state.failure = failure
            duplicate = self.store.find_matching_run(
                state.run.repo,
                state.run.head_sha,
                failure.signature.fingerprint,
                state.run.id,
            )
            if duplicate:
                state.status = RunStatus.DUPLICATE
                state.decision = Decision(
                    accepted=False,
                    reason=f"duplicate of run {duplicate.run.id}",
                )
                self.store.save_run(state)
                self.store.append_trace(
                    run_id,
                    "duplicate",
                    {"original_run_id": duplicate.run.id},
                )
                return state

            config = await self.github.fetch_repository_config(
                state.run.repo,
                state.run.head_sha,
                state.run.installation_id,
            )
            state.repository_config = config
            state.plan = await self.model.plan_failure(failure)
            self.store.write_artifact(
                run_id,
                "failure.json",
                failure.model_dump_json(indent=2),
            )
            self.store.write_artifact(
                run_id,
                "plan.json",
                state.plan.model_dump_json(indent=2),
            )
            self.store.append_trace(
                run_id,
                "plan_created",
                {"summary": state.plan.summary, "hypotheses": len(state.plan.hypotheses)},
            )

            repair_assignments = [
                self._create_assignment(state, AssignmentKind.REPAIR, index, config)
                for index in range(config.worker_count)
            ]
            verifier = self._create_assignment(
                state,
                AssignmentKind.VERIFY,
                config.worker_count,
                config,
            )
            await self.github.dispatch_run(state, repair_assignments, verifier)
            state.status = RunStatus.WAITING
            self.store.save_run(state)
            self.store.append_trace(
                run_id,
                "dispatched",
                {
                    "workers": len(repair_assignments),
                    "verifier": verifier[0].id,
                },
            )
            return state
        except (GitHubError, ModelError, ValueError, OSError) as exc:
            state.status = RunStatus.FAILED
            state.error = str(exc)[:2000]
            self.store.save_run(state)
            self.store.append_trace(run_id, "failed", {"error": state.error})
            return state

    def _create_assignment(
        self,
        state: RunState,
        kind: AssignmentKind,
        worker_index: int,
        config: RepositoryConfig,
    ) -> tuple[WorkerAssignment, str]:
        token = create_capability_token()
        assignment = WorkerAssignment(
            id=str(uuid.uuid4()),
            run_id=state.run.id,
            kind=kind,
            worker_index=worker_index,
            repo=state.run.repo,
            head_sha=state.run.head_sha,
            default_branch=state.run.default_branch,
            workflow_file=self.settings.workflow_file,
            expires_at=assignment_expiry(self.settings),
            max_model_calls=self.settings.max_model_calls_per_worker,
        )
        self.store.create_assignment(assignment, token)
        return assignment, token

    def submit_candidate(
        self,
        assignment: WorkerAssignment,
        submission: CandidateSubmission,
        config: RepositoryConfig,
    ) -> CandidatePatch:
        validation = validate_patch(
            submission.unified_diff,
            submission.file_contents,
            config,
        )
        if not validation.valid:
            raise ValueError("; ".join(validation.errors))
        candidate = CandidatePatch(
            id=str(uuid.uuid4()),
            run_id=assignment.run_id,
            assignment_id=assignment.id,
            worker_index=assignment.worker_index,
            summary=submission.summary[:2000],
            root_cause=submission.root_cause[:4000],
            unified_diff=submission.unified_diff,
            file_contents=submission.file_contents,
            changed_files=validation.changed_files,
            changed_lines=validation.changed_lines,
            confidence=submission.confidence,
            model_calls=submission.model_calls,
        )
        assignment.candidate = candidate
        assignment.status = AssignmentStatus.COMPLETED
        self.store.save_assignment(assignment)
        self.store.write_artifact(
            assignment.run_id,
            f"candidate-{candidate.worker_index}.diff",
            candidate.unified_diff,
        )
        self.store.append_trace(
            assignment.run_id,
            "candidate_submitted",
            {
                "assignment_id": assignment.id,
                "candidate_id": candidate.id,
                "changed_files": candidate.changed_files,
                "changed_lines": candidate.changed_lines,
            },
        )
        return candidate

    async def finalize(
        self,
        assignment: WorkerAssignment,
        verification: VerificationBundle,
    ) -> RunState:
        state = self.store.get_run(assignment.run_id)
        assignment.verification = verification
        assignment.status = AssignmentStatus.COMPLETED
        self.store.save_assignment(assignment)
        state.verification = verification
        self.store.write_artifact(
            state.run.id,
            "verification.json",
            verification.model_dump_json(indent=2),
        )

        config = state.repository_config
        expected_command_count = len(config.verification_commands) if config else 0
        setup_ok = all(
            check.exit_code == 0 and not check.timed_out for check in verification.setup_checks
        )
        baseline_failed = any(
            check.exit_code not in {0, None} or check.timed_out
            for check in verification.baseline_checks
        )
        evidence_reproduced = bool(
            verification.reproduced
            and setup_ok
            and baseline_failed
            and len(verification.baseline_checks) == expected_command_count
        )
        verification.reproduced = evidence_reproduced
        if not evidence_reproduced:
            state.status = RunStatus.NO_CANDIDATE
            state.decision = Decision(
                accepted=False,
                reason="verification evidence did not reproduce the original failure",
            )
            self.store.save_run(state)
            self.store.append_trace(
                state.run.id,
                "decision",
                state.decision.model_dump(mode="json"),
            )
            return state

        repair_assignments = self.store.list_assignments(
            state.run.id,
            AssignmentKind.REPAIR,
        )
        candidates_by_id = {
            assignment.candidate.id: assignment.candidate
            for assignment in repair_assignments
            if assignment.candidate
        }
        trusted_checks = {
            item.candidate_id: _checks_are_successful(item.checks, expected_command_count)
            for item in verification.candidates
            if item.candidate_id in candidates_by_id
            and item.patch_sha256 == patch_sha256(candidates_by_id[item.candidate_id].unified_diff)
        }
        passing_ids = {
            item.candidate_id
            for item in verification.candidates
            if item.passed and trusted_checks.get(item.candidate_id, False)
        }
        candidates = [
            assignment.candidate
            for assignment in repair_assignments
            if assignment.candidate and assignment.candidate.id in passing_ids
        ]
        if not candidates:
            state.status = RunStatus.NO_CANDIDATE
            state.decision = Decision(
                accepted=False,
                reason="no candidate reproduced the failure and passed all verification commands",
            )
            self.store.save_run(state)
            self.store.append_trace(
                state.run.id,
                "decision",
                state.decision.model_dump(mode="json"),
            )
            return state

        selected = sorted(
            candidates,
            key=lambda item: (
                item.changed_lines,
                len(item.changed_files),
                -item.confidence,
                item.created_at,
            ),
        )[0]
        score = max(
            0.0,
            1.0
            - selected.changed_lines
            / max(1, state.repository_config.max_patch_lines if state.repository_config else 500),
        )
        draft_pr_url: str | None = None
        if state.repository_config and state.repository_config.allow_draft_pr:
            try:
                draft_pr_url = await self.github.publish_draft_pr(state, selected)
            except GitHubError as exc:
                state.status = RunStatus.FAILED
                state.error = f"candidate passed but draft PR publishing failed: {exc}"
                self.store.save_run(state)
                self.store.append_trace(
                    state.run.id,
                    "publish_failed",
                    {"error": state.error},
                )
                return state
        state.status = RunStatus.SUCCEEDED
        state.decision = Decision(
            accepted=True,
            selected_candidate_id=selected.id,
            reason="failure reproduced and all verification commands passed",
            score=score,
            draft_pr_url=draft_pr_url,
        )
        self.store.save_run(state)
        self.store.write_artifact(
            state.run.id,
            "decision.json",
            state.decision.model_dump_json(indent=2),
        )
        self.store.append_trace(
            state.run.id,
            "decision",
            {
                **state.decision.model_dump(mode="json"),
                "patch_sha256": patch_sha256(selected.unified_diff),
            },
        )
        return state


def create_failure_run(payload: dict[str, Any], run_id: str) -> FailureRun:
    workflow_run = payload.get("workflow_run") or {}
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    sender = payload.get("sender") or {}
    return FailureRun(
        id=run_id,
        repo=str(repository.get("full_name", "")),
        installation_id=installation.get("id"),
        workflow_run_id=int(workflow_run.get("id", 0)),
        workflow_name=str(workflow_run.get("name") or "workflow"),
        workflow_path=str(workflow_run.get("path") or ""),
        workflow_event=str(workflow_run.get("event") or ""),
        head_sha=str(workflow_run.get("head_sha") or ""),
        head_branch=str(workflow_run.get("head_branch") or ""),
        default_branch=str(repository.get("default_branch") or ""),
        html_url=workflow_run.get("html_url"),
        conclusion=str(workflow_run.get("conclusion") or ""),
        sender=sender.get("login"),
        repository_private=bool(repository.get("private", False)),
    )


def _checks_are_successful(
    checks: list[CommandResult],
    expected_command_count: int,
) -> bool:
    return (
        expected_command_count > 0
        and len(checks) == expected_command_count
        and all(check.exit_code == 0 and not check.timed_out for check in checks)
    )
