from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cihive.models import (
    CandidatePatch,
    CandidateVerification,
    CommandResult,
    RepositoryConfig,
    VerificationBundle,
)
from cihive.patch import patch_sha256, validate_patch
from cihive.redaction import redact_text, truncate_text


def _normalized_command(command: list[str]) -> list[str]:
    if not command:
        raise ValueError("verification command cannot be empty")
    normalized = list(command)
    if normalized[0] in {"python", "python3"}:
        normalized[0] = sys.executable
    return normalized


def run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: float,
) -> CommandResult:
    normalized = _normalized_command(command)
    started = time.monotonic()
    try:
        process = subprocess.run(
            normalized,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, "CI": "true", "CIHIVE_VERIFY": "1"},
        )
        output = redact_text(f"{process.stdout}\n{process.stderr}".strip())
        return CommandResult(
            command=normalized,
            exit_code=process.returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            output=truncate_text(output, 30_000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        output = redact_text(f"{stdout or ''}\n{stderr or ''}".strip())
        return CommandResult(
            command=normalized,
            exit_code=None,
            timed_out=True,
            duration_seconds=round(time.monotonic() - started, 3),
            output=truncate_text(output, 30_000),
        )
    except OSError as exc:
        return CommandResult(
            command=normalized,
            exit_code=127,
            duration_seconds=round(time.monotonic() - started, 3),
            output=redact_text(str(exc)),
        )


def run_commands(repo_root: Path, config: RepositoryConfig) -> list[CommandResult]:
    return [
        run_command(command, repo_root, config.command_timeout_seconds)
        for command in config.verification_commands
    ]


def run_setup_commands(repo_root: Path, config: RepositoryConfig) -> list[CommandResult]:
    return [
        run_command(command, repo_root, config.command_timeout_seconds)
        for command in config.setup_commands
    ]


@contextmanager
def fresh_worktree(
    repo_root: Path,
    temp_parent: Path | None = None,
) -> Iterator[Path]:
    if temp_parent is not None:
        temp_parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(
            prefix="cihive-verify-",
            dir=temp_parent if temp_parent is not None else None,
        )
    )
    worktree = temp_root / "worktree"
    try:
        process = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"could not create verification worktree: {process.stderr}")
        yield worktree
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        shutil.rmtree(temp_root, ignore_errors=True)


def apply_candidate_patch(worktree: Path, patch: str) -> None:
    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=worktree,
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        raise RuntimeError(f"patch does not apply: {check.stderr}")
    applied = subprocess.run(
        ["git", "apply", "-"],
        cwd=worktree,
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(f"could not apply patch: {applied.stderr}")


def verify_candidates(
    repo_root: Path,
    config: RepositoryConfig,
    candidates: list[CandidatePatch],
    temp_parent: Path | None = None,
) -> VerificationBundle:
    with fresh_worktree(repo_root, temp_parent) as worktree:
        setup_checks = run_setup_commands(worktree, config)
        baseline_checks = (
            run_commands(worktree, config)
            if all(check.exit_code == 0 and not check.timed_out for check in setup_checks)
            else []
        )
    setup_failed = any(
        check.exit_code not in {0, None} or check.timed_out for check in setup_checks
    )
    reproduced = (
        any(check.exit_code not in {0, None} or check.timed_out for check in baseline_checks)
        and not setup_failed
    )

    results: list[CandidateVerification] = []
    for candidate in candidates:
        validation = validate_patch(
            candidate.unified_diff,
            candidate.file_contents,
            config,
        )
        if not validation.valid:
            results.append(
                CandidateVerification(
                    candidate_id=candidate.id,
                    passed=False,
                    reason="; ".join(validation.errors),
                    patch_sha256=patch_sha256(candidate.unified_diff),
                )
            )
            continue
        if setup_failed:
            results.append(
                CandidateVerification(
                    candidate_id=candidate.id,
                    passed=False,
                    reason="setup commands failed during baseline verification",
                    patch_sha256=patch_sha256(candidate.unified_diff),
                )
            )
            continue
        if not reproduced:
            results.append(
                CandidateVerification(
                    candidate_id=candidate.id,
                    passed=False,
                    reason="the configured commands did not reproduce the failure",
                    patch_sha256=patch_sha256(candidate.unified_diff),
                )
            )
            continue
        try:
            with fresh_worktree(repo_root, temp_parent) as worktree:
                apply_candidate_patch(worktree, candidate.unified_diff)
                candidate_setup = run_setup_commands(worktree, config)
                if any(
                    check.exit_code not in {0, None} or check.timed_out for check in candidate_setup
                ):
                    raise RuntimeError("setup commands failed for candidate")
                checks = run_commands(worktree, config)
            passed = all(check.exit_code == 0 and not check.timed_out for check in checks)
            reason = "all verification commands passed" if passed else "one or more commands failed"
            results.append(
                CandidateVerification(
                    candidate_id=candidate.id,
                    passed=passed,
                    reason=reason,
                    checks=checks,
                    patch_sha256=patch_sha256(candidate.unified_diff),
                )
            )
        except RuntimeError as exc:
            results.append(
                CandidateVerification(
                    candidate_id=candidate.id,
                    passed=False,
                    reason=str(exc),
                    patch_sha256=patch_sha256(candidate.unified_diff),
                )
            )

    return VerificationBundle(
        reproduced=reproduced,
        setup_checks=setup_checks,
        baseline_checks=baseline_checks,
        candidates=results,
    )
