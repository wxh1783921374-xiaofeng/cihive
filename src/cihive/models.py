from __future__ import annotations

import shlex
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


MANDATORY_FORBIDDEN_PATHS = [
    ".github/workflows/**",
    ".cihive.yml",
    "**/secrets/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    ".gitmodules",
    "tests/**",
    "test/**",
    "**/test/**",
    "**/tests/**",
    "test_*.py",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/__tests__/**",
    "**/fixtures/**",
]


class RunStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    NO_CANDIDATE = "no_candidate"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class AssignmentKind(StrEnum):
    REPAIR = "repair"
    VERIFY = "verify"


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class FailedJob(BaseModel):
    name: str
    conclusion: str | None = None
    failed_steps: list[str] = Field(default_factory=list)


class FailureSignature(BaseModel):
    fingerprint: str
    workflow_path: str
    failed_jobs: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    normalized_error: str


class FailureRun(BaseModel):
    id: str
    repo: str
    installation_id: int | None = None
    workflow_run_id: int
    workflow_name: str
    workflow_path: str
    workflow_event: str
    head_sha: str
    head_branch: str
    default_branch: str
    html_url: str | None = None
    conclusion: str
    sender: str | None = None
    repository_private: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class FailureContext(BaseModel):
    run_id: str
    summary: str
    logs_excerpt: str
    failed_jobs: list[FailedJob] = Field(default_factory=list)
    signature: FailureSignature


class Hypothesis(BaseModel):
    title: str
    rationale: str
    suspected_files: list[str] = Field(default_factory=list)
    strategy: str


class RepairPlan(BaseModel):
    summary: str
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)


class RepositoryConfig(BaseModel):
    version: Literal[1] = 1
    setup_commands: list[list[str]] = Field(default_factory=list, max_length=3)
    verification_commands: list[list[str]] = Field(min_length=1, max_length=5)
    worker_count: int = Field(default=3, ge=1, le=5)
    run_timeout_seconds: int = Field(default=1200, ge=30)
    command_timeout_seconds: float = Field(default=600, ge=0.05)
    max_patch_lines: int = Field(default=500, ge=1)
    max_patch_files: int = Field(default=20, ge=1)
    max_model_turns: int = Field(default=12, ge=1, le=40)
    allow_draft_pr: bool = True
    forbidden_paths: list[str] = Field(default_factory=lambda: list(MANDATORY_FORBIDDEN_PATHS))

    @field_validator("setup_commands", "verification_commands", mode="before")
    @classmethod
    def normalize_commands(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        for command in value:
            if isinstance(command, str):
                normalized.append(shlex.split(command))
            else:
                normalized.append(command)
        return normalized

    @model_validator(mode="after")
    def restore_mandatory_paths(self) -> RepositoryConfig:
        self.forbidden_paths = sorted(set(self.forbidden_paths).union(MANDATORY_FORBIDDEN_PATHS))
        return self


class WorkerAssignment(BaseModel):
    id: str
    run_id: str
    kind: AssignmentKind
    worker_index: int = Field(ge=0)
    repo: str
    head_sha: str
    default_branch: str
    workflow_file: str
    expires_at: datetime
    status: AssignmentStatus = AssignmentStatus.PENDING
    model_calls: int = 0
    max_model_calls: int = 2
    candidate: CandidatePatch | None = None
    verification: VerificationBundle | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentRequest(BaseModel):
    files: dict[str, str]
    max_turns: int = Field(default=12, ge=1, le=40)
    feedback: str | None = Field(default=None, max_length=4000)


class RepairProposal(BaseModel):
    summary: str
    root_cause: str
    unified_diff: str = Field(min_length=1, max_length=1_000_000)
    confidence: float = Field(ge=0, le=1)


class PatchValidation(BaseModel):
    valid: bool
    changed_files: list[str] = Field(default_factory=list)
    changed_lines: int = 0
    errors: list[str] = Field(default_factory=list)


class CandidatePatch(BaseModel):
    id: str
    run_id: str
    assignment_id: str
    worker_index: int
    summary: str
    root_cause: str
    unified_diff: str
    file_contents: dict[str, str]
    changed_files: list[str]
    changed_lines: int
    confidence: float = Field(ge=0, le=1)
    model_calls: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class CommandResult(BaseModel):
    command: list[str]
    exit_code: int | None
    timed_out: bool = False
    duration_seconds: float = 0
    output: str = ""


class CandidateVerification(BaseModel):
    candidate_id: str
    passed: bool
    reason: str
    checks: list[CommandResult] = Field(default_factory=list)
    patch_sha256: str | None = None


class VerificationBundle(BaseModel):
    verified_at: datetime = Field(default_factory=utc_now)
    reproduced: bool
    setup_checks: list[CommandResult] = Field(default_factory=list)
    baseline_checks: list[CommandResult] = Field(default_factory=list)
    candidates: list[CandidateVerification] = Field(default_factory=list)


class Decision(BaseModel):
    accepted: bool
    selected_candidate_id: str | None = None
    reason: str
    score: float | None = None
    draft_pr_url: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunState(BaseModel):
    run: FailureRun
    status: RunStatus = RunStatus.QUEUED
    failure: FailureContext | None = None
    plan: RepairPlan | None = None
    verification: VerificationBundle | None = None
    decision: Decision | None = None
    repository_config: RepositoryConfig | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AssignmentView(BaseModel):
    assignment: WorkerAssignment
    failure: FailureContext
    plan: RepairPlan
    repository_config: RepositoryConfig
    candidates: list[CandidatePatch] = Field(default_factory=list)


class AgentResponse(BaseModel):
    proposal: RepairProposal
    calls_remaining: int


class CandidateSubmission(BaseModel):
    summary: str
    root_cause: str
    unified_diff: str = Field(min_length=1, max_length=1_000_000)
    file_contents: dict[str, str]
    confidence: float = Field(ge=0, le=1)
    model_calls: int = Field(default=1, ge=0)


class AssignmentResultRequest(BaseModel):
    candidate: CandidateSubmission | None = None
    verification: VerificationBundle | None = None
    error: str | None = None


class WebhookRunResponse(BaseModel):
    status: Literal["accepted", "ignored", "duplicate"]
    run_id: str | None = None
    reason: str | None = None


def safe_relative_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if len(normalized) > 500 or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"unsafe path: {path!r}")
    if normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha()
    ):
        raise ValueError(f"unsafe path: {path!r}")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.as_posix() == ".":
        raise ValueError(f"unsafe path: {path!r}")
    return pure.as_posix()


WorkerAssignment.model_rebuild()
