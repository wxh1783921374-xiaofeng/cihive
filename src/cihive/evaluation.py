from __future__ import annotations

import difflib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from cihive.models import CandidatePatch, RepositoryConfig
from cihive.verification import verify_candidates


class EvaluationCase(BaseModel):
    name: str
    description: str
    files: dict[str, str]
    command: list[str] = Field(min_length=1)
    candidate: dict[str, str] | None = None
    expected: Literal["accepted", "no_candidate"]
    timeout_seconds: float = 30


class EvaluationCaseResult(BaseModel):
    name: str
    expected: str
    actual: str
    passed: bool
    reason: str


class EvaluationReport(BaseModel):
    cases: int
    expected_outcomes_met: int
    repair_success_rate: float
    no_candidate_cases: int
    invalid_prs: int
    model_tokens: int
    duration_seconds: float
    results: list[EvaluationCaseResult]


def _build_diff(original: dict[str, str], updated: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(updated):
        before = original.get(path, "").splitlines()
        after = updated[path].splitlines()
        diff = difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
        body = "\n".join(diff)
        if body:
            chunks.append(f"diff --git a/{path} b/{path}\n{body}")
    return "\n".join(chunks) + ("\n" if chunks else "")


def _initialize_git(directory: Path) -> None:
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.name", "CIHive Eval"],
        ["git", "config", "user.email", "eval@cihive.invalid"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "fixture"],
    ]
    for command in commands:
        process = subprocess.run(
            command,
            cwd=directory,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"evaluation git setup failed: {process.stderr}")


def run_evaluation_suite(path: Path) -> EvaluationReport:
    started = time.monotonic()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [EvaluationCase.model_validate(item) for item in raw["cases"]]
    results: list[EvaluationCaseResult] = []
    accepted_count = 0
    no_candidate_count = 0
    temp_parent = Path.cwd() / ".tmp" / "eval"
    temp_parent.mkdir(parents=True, exist_ok=True)

    for case in cases:
        with tempfile.TemporaryDirectory(
            prefix=f"cihive-eval-{case.name}-",
            dir=temp_parent,
        ) as temp:
            root = Path(temp)
            for file_path, content in case.files.items():
                destination = root / file_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            _initialize_git(root)
            config = RepositoryConfig(
                verification_commands=[case.command],
                worker_count=1,
                command_timeout_seconds=case.timeout_seconds,
            )
            candidates: list[CandidatePatch] = []
            if case.candidate:
                patch = _build_diff(case.files, case.candidate)
                candidates.append(
                    CandidatePatch(
                        id=f"candidate-{case.name}",
                        run_id=f"run-{case.name}",
                        assignment_id=f"assignment-{case.name}",
                        worker_index=0,
                        summary="fixture candidate",
                        root_cause=case.description,
                        unified_diff=patch,
                        file_contents=case.candidate,
                        changed_files=sorted(case.candidate),
                        changed_lines=patch.count("\n+") + patch.count("\n-"),
                        confidence=0.8,
                    )
                )
            verification = verify_candidates(
                root,
                config,
                candidates,
                temp_parent=temp_parent,
            )
            accepted = verification.reproduced and any(
                result.passed for result in verification.candidates
            )
            actual = "accepted" if accepted else "no_candidate"
            if accepted:
                accepted_count += 1
                reason = "failure reproduced and candidate passed"
            elif not verification.reproduced:
                reason = "configured command did not reproduce the failure"
            elif not verification.candidates:
                reason = "no candidate was submitted"
            else:
                reason = "; ".join(result.reason for result in verification.candidates)
            results.append(
                EvaluationCaseResult(
                    name=case.name,
                    expected=case.expected,
                    actual=actual,
                    passed=actual == case.expected,
                    reason=reason,
                )
            )
            no_candidate_count += int(actual == "no_candidate")

    duration = round(time.monotonic() - started, 3)
    return EvaluationReport(
        cases=len(cases),
        expected_outcomes_met=sum(result.passed for result in results),
        repair_success_rate=round(accepted_count / max(1, len(cases)), 3),
        no_candidate_cases=no_candidate_count,
        invalid_prs=0,
        model_tokens=0,
        duration_seconds=duration,
        results=results,
    )
