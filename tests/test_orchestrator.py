from __future__ import annotations

from datetime import timedelta

from cihive.models import (
    AssignmentKind,
    CandidateVerification,
    CommandResult,
    RunState,
    RunStatus,
    VerificationBundle,
    utc_now,
)
from cihive.orchestrator import Orchestrator
from cihive.patch import patch_sha256
from tests.conftest import (
    FakeGitHub,
    FakeModel,
    make_candidate,
    make_failure_run,
    make_submission,
)


async def test_full_repair_flow_opens_only_after_verification(
    settings,
    store,
) -> None:
    github = FakeGitHub()
    orchestrator = Orchestrator(settings, store, github, FakeModel())
    state = RunState(run=make_failure_run())
    store.create_run(state)

    processed = await orchestrator.process_run(state.run.id)
    assert processed.status == RunStatus.WAITING
    assert github.dispatched is not None
    workers = store.list_assignments(state.run.id, AssignmentKind.REPAIR)
    verifier = store.list_assignments(state.run.id, AssignmentKind.VERIFY)[0]
    assert len(workers) == 3

    for index, worker in enumerate(workers):
        orchestrator.submit_candidate(
            worker,
            make_submission(
                make_candidate(
                    state.run.id,
                    worker.id,
                    worker_index=index,
                )
            ),
            processed.repository_config,
        )

    bundle = VerificationBundle(
        reproduced=True,
        baseline_checks=[
            CommandResult(
                command=["pytest"],
                exit_code=1,
                output="failed",
            )
        ],
        candidates=[
            CandidateVerification(
                candidate_id=worker.candidate.id,
                passed=True,
                reason="passed",
                checks=[
                    CommandResult(
                        command=["pytest"],
                        exit_code=0,
                        output="passed",
                    )
                ],
                patch_sha256=patch_sha256(worker.candidate.unified_diff),
            )
            for worker in store.list_assignments(state.run.id, AssignmentKind.REPAIR)
            if worker.candidate
        ],
    )
    finalized = await orchestrator.finalize(verifier, bundle)
    assert finalized.status == RunStatus.SUCCEEDED
    assert finalized.decision is not None
    assert finalized.decision.accepted
    assert finalized.decision.draft_pr_url == "https://github.com/octo/demo/pull/7"
    assert github.published is not None


async def test_duplicate_failure_signature_does_not_dispatch_twice(settings, store) -> None:
    github = FakeGitHub()
    orchestrator = Orchestrator(settings, store, github, FakeModel())
    first = RunState(run=make_failure_run("run-1"))
    store.create_run(first)
    await orchestrator.process_run(first.run.id)

    second = RunState(run=make_failure_run("run-2"))
    store.create_run(second)
    processed = await orchestrator.process_run(second.run.id)
    assert processed.status == RunStatus.DUPLICATE


async def test_verifier_booleans_without_command_evidence_are_rejected(settings, store) -> None:
    github = FakeGitHub()
    orchestrator = Orchestrator(settings, store, github, FakeModel())
    state = RunState(run=make_failure_run("run-untrusted"))
    store.create_run(state)
    processed = await orchestrator.process_run(state.run.id)
    worker = store.list_assignments(state.run.id, AssignmentKind.REPAIR)[0]
    verifier = store.list_assignments(state.run.id, AssignmentKind.VERIFY)[0]
    orchestrator.submit_candidate(
        worker,
        make_submission(make_candidate(state.run.id, worker.id)),
        processed.repository_config,
    )
    worker = store.list_assignments(state.run.id, AssignmentKind.REPAIR)[0]
    bundle = VerificationBundle(
        reproduced=True,
        baseline_checks=[
            CommandResult(command=["pytest"], exit_code=1),
        ],
        candidates=[
            CandidateVerification(
                candidate_id=worker.candidate.id,
                passed=True,
                reason="claimed success without checks",
                patch_sha256=patch_sha256(worker.candidate.unified_diff),
            )
        ],
    )
    finalized = await orchestrator.finalize(verifier, bundle)
    assert finalized.status == RunStatus.NO_CANDIDATE
    assert github.published is None


def test_stale_waiting_run_is_failed(settings, store) -> None:
    state = RunState(
        run=make_failure_run("run-stale"),
        status=RunStatus.WAITING,
        updated_at=utc_now() - timedelta(hours=2),
    )
    store.create_run(state)
    assert store.expire_stale_runs(1200) == ["run-stale"]
    expired = store.get_run("run-stale")
    assert expired.status == RunStatus.FAILED
    assert "timeout" in (expired.error or "")
