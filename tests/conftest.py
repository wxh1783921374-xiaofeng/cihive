from __future__ import annotations

import base64
import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from cihive.config import Settings
from cihive.db import RunStore
from cihive.models import (
    CandidatePatch,
    CandidateSubmission,
    FailedJob,
    FailureContext,
    FailureRun,
    FailureSignature,
    Hypothesis,
    RepairPlan,
    RepairProposal,
    RepositoryConfig,
)


def pytest_configure(config: pytest.Config) -> None:
    (config.rootpath / ".tmp").mkdir(exist_ok=True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data" / "cihive.db",
        github_webhook_secret="test-secret",
        require_oidc=False,
        model_provider="none",
        control_plane_url="http://testserver",
    )


@pytest.fixture
def store(settings: Settings) -> RunStore:
    return RunStore(settings.resolved_database_path, settings.data_dir)


def make_failure_run(run_id: str = "run-1") -> FailureRun:
    return FailureRun(
        id=run_id,
        repo="octo/demo",
        installation_id=123,
        workflow_run_id=456,
        workflow_name="Tests",
        workflow_path=".github/workflows/tests.yml",
        workflow_event="push",
        head_sha="a" * 40,
        head_branch="main",
        default_branch="main",
        html_url="https://github.com/octo/demo/actions/runs/456",
        conclusion="failure",
        sender="maintainer",
    )


def make_failure_context(run: FailureRun) -> FailureContext:
    return FailureContext(
        run_id=run.id,
        summary="Tests / pytest",
        logs_excerpt="AssertionError: expected 5, got -1",
        failed_jobs=[FailedJob(name="Tests", conclusion="failure", failed_steps=["pytest"])],
        signature=FailureSignature(
            fingerprint="f" * 64,
            workflow_path=run.workflow_path,
            failed_jobs=["Tests"],
            failed_steps=["pytest"],
            normalized_error="assertionerror expected <n> got -<n>",
        ),
    )


def make_plan() -> RepairPlan:
    return RepairPlan(
        summary="The add function subtracts instead of adds.",
        hypotheses=[
            Hypothesis(
                title="Wrong arithmetic operator",
                rationale="The failing assertion is consistent with subtraction.",
                suspected_files=["mathx.py"],
                strategy="Change the operator and run the tests.",
            )
        ],
    )


def make_config() -> RepositoryConfig:
    return RepositoryConfig(
        verification_commands=[["python", "-m", "pytest", "-q"]],
        worker_count=3,
    )


def make_patch(old: str = "return a - b\n", new: str = "return a + b\n") -> str:
    return (
        "diff --git a/mathx.py b/mathx.py\n"
        "--- a/mathx.py\n"
        "+++ b/mathx.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        f"-    {old}"
        f"+    {new}"
    )


def make_candidate(
    run_id: str,
    assignment_id: str,
    worker_index: int = 0,
    content: str = "def add(a, b):\n    return a + b\n",
) -> CandidatePatch:
    return CandidatePatch(
        id=f"candidate-{worker_index}",
        run_id=run_id,
        assignment_id=assignment_id,
        worker_index=worker_index,
        summary="Fix arithmetic",
        root_cause="Wrong operator",
        unified_diff=make_patch(),
        file_contents={"mathx.py": content},
        changed_files=["mathx.py"],
        changed_lines=2,
        confidence=0.9,
    )


def make_submission(candidate: CandidatePatch) -> CandidateSubmission:
    return CandidateSubmission(
        summary=candidate.summary,
        root_cause=candidate.root_cause,
        unified_diff=candidate.unified_diff,
        file_contents=candidate.file_contents,
        confidence=candidate.confidence,
        model_calls=candidate.model_calls,
    )


class FakeGitHub:
    def __init__(self, config: RepositoryConfig | None = None) -> None:
        self.config = config or make_config()
        self.dispatched = None
        self.published = None

    async def list_jobs(self, repo, run_id, installation_id):
        return {
            "jobs": [
                {
                    "name": "Tests",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "pytest", "conclusion": "failure"},
                    ],
                }
            ]
        }

    async def download_logs(self, repo, run_id, installation_id):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "Tests/1_pytest.txt",
                "AssertionError: expected 5, got -1\n"
                "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n",
            )
        return payload.getvalue()

    async def fetch_repository_config(self, repo, ref, installation_id):
        return self.config

    async def dispatch_run(self, state, repair_assignments, verify_assignment):
        self.dispatched = {
            "state": state,
            "workers": repair_assignments,
            "verifier": verify_assignment,
        }

    async def publish_draft_pr(self, state, candidate):
        self.published = (state, candidate)
        return "https://github.com/octo/demo/pull/7"


class FakeModel:
    async def plan_failure(self, failure):
        return make_plan()

    async def generate_repair(self, failure, plan, files, max_turns, feedback=None):
        return RepairProposal(
            summary="Fix arithmetic",
            root_cause="Wrong operator",
            unified_diff=make_patch(),
            confidence=0.9,
        )


def webhook_payload() -> dict:
    return {
        "action": "completed",
        "installation": {"id": 123},
        "repository": {
            "full_name": "octo/demo",
            "private": False,
            "default_branch": "main",
        },
        "sender": {"login": "maintainer"},
        "workflow_run": {
            "id": 456,
            "name": "Tests",
            "path": ".github/workflows/tests.yml",
            "event": "push",
            "head_sha": "a" * 40,
            "head_branch": "main",
            "conclusion": "failure",
            "html_url": "https://github.com/octo/demo/actions/runs/456",
        },
    }


def init_git_repo(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    for command in [
        ["git", "init", "-q"],
        ["git", "config", "user.name", "Test"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "fixture"],
    ]:
        subprocess.run(command, cwd=root, check=True, capture_output=True)


def encode_yaml_content(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")
