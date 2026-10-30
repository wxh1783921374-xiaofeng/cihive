from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from cihive.config import Settings
from cihive.models import (
    AgentRequest,
    AgentResponse,
    AssignmentKind,
    AssignmentResultRequest,
    AssignmentView,
    CandidatePatch,
    CandidateSubmission,
    safe_relative_path,
)
from cihive.patch import parse_patch_paths
from cihive.security import GitHubOIDCClient
from cihive.verification import verify_candidates


class WorkerError(RuntimeError):
    pass


_SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}


def collect_repository_files(root: Path, max_total_bytes: int) -> dict[str, str]:
    files: dict[str, str] = {}
    total = 0
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIRECTORIES and not name.startswith(".")
        )
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                relative = safe_relative_path(path.relative_to(root).as_posix())
            except (ValueError, OSError):
                continue
            lowered = relative.lower()
            if (
                lowered.endswith(tuple(_SECRET_SUFFIXES))
                or Path(relative).name.startswith(".env")
                or "/secrets/" in f"/{lowered}"
            ):
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "\x00" in content:
                continue
            encoded_size = len(content.encode("utf-8"))
            if total + encoded_size > max_total_bytes:
                break
            files[relative] = content
            total += encoded_size
    return files


class WorkerClient:
    def __init__(
        self,
        api_url: str,
        assignment_id: str,
        capability_token: str,
        oidc_token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.assignment_id = assignment_id
        self.capability_token = capability_token
        self.oidc_token = oidc_token
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.capability_token}"}
        if self.oidc_token:
            headers["X-GitHub-OIDC-Token"] = self.oidc_token
        return headers

    async def get_assignment(self) -> AssignmentView:
        async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
            response = await client.get(
                f"{self.api_url}/v1/assignments/{self.assignment_id}",
                headers=self._headers(),
            )
        if response.status_code >= 400:
            raise WorkerError(f"assignment request failed: {response.text}")
        return AssignmentView.model_validate(response.json())

    async def request_repair(
        self,
        files: dict[str, str],
        max_turns: int,
        feedback: str | None = None,
    ) -> AgentResponse:
        payload = AgentRequest(files=files, max_turns=max_turns, feedback=feedback)
        async with httpx.AsyncClient(timeout=600, transport=self.transport) as client:
            response = await client.post(
                f"{self.api_url}/v1/assignments/{self.assignment_id}/agent",
                headers=self._headers(),
                json=payload.model_dump(mode="json"),
            )
        if response.status_code >= 400:
            raise WorkerError(f"model proxy request failed: {response.text}")
        return AgentResponse.model_validate(response.json())

    async def submit(self, result: AssignmentResultRequest) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120, transport=self.transport) as client:
            response = await client.post(
                f"{self.api_url}/v1/assignments/{self.assignment_id}/result",
                headers=self._headers(),
                json=result.model_dump(mode="json", exclude_none=True),
            )
        if response.status_code >= 400:
            raise WorkerError(f"result submission failed: {response.text}")
        return response.json()


async def run_worker(
    settings: Settings,
    root: Path,
    assignment_id: str,
    capability_token: str,
) -> CandidatePatch:
    oidc_token = await GitHubOIDCClient().get_token(settings.github_oidc_audience)
    client = WorkerClient(
        settings.control_plane_url,
        assignment_id,
        capability_token,
        oidc_token,
    )
    try:
        view = await client.get_assignment()
        if view.assignment.kind != AssignmentKind.REPAIR:
            raise WorkerError("repair command received a non-repair assignment")
        files = collect_repository_files(root, settings.max_context_bytes)
        response = await client.request_repair(files, view.repository_config.max_model_turns)
        apply = await _check_patch(root, response.proposal.unified_diff)
        if apply.returncode != 0 and response.calls_remaining > 0:
            response = await client.request_repair(
                files,
                view.repository_config.max_model_turns,
                feedback=f"Previous patch failed git apply: {apply.stderr[-2000:]}",
            )
            apply = await _check_patch(root, response.proposal.unified_diff)
        if apply.returncode != 0:
            raise WorkerError(f"model produced a patch that does not apply: {apply.stderr}")
        paths = parse_patch_paths(response.proposal.unified_diff)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "apply", "-"],
            cwd=root,
            input=response.proposal.unified_diff,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        file_contents: dict[str, str] = {}
        for path in paths:
            candidate_path = root / path
            if not candidate_path.is_file():
                raise WorkerError(f"candidate removed or changed file type: {path}")
            file_contents[path] = candidate_path.read_text(encoding="utf-8")
        submission = AssignmentResultRequest(
            candidate=CandidateSubmission(
                summary=response.proposal.summary,
                root_cause=response.proposal.root_cause,
                unified_diff=response.proposal.unified_diff,
                file_contents=file_contents,
                confidence=response.proposal.confidence,
                model_calls=1,
            )
        )
        result = await client.submit(submission)
        return CandidatePatch.model_validate(result["candidate"])
    except Exception as exc:
        with suppress(Exception):
            await client.submit(AssignmentResultRequest(error=str(exc)[:2000]))
        raise


async def _check_patch(root: Path, patch: str) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        ["git", "apply", "--check", "-"],
        cwd=root,
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


async def run_verifier(
    settings: Settings,
    root: Path,
    assignment_id: str,
    capability_token: str,
) -> dict[str, Any]:
    oidc_token = await GitHubOIDCClient().get_token(settings.github_oidc_audience)
    client = WorkerClient(
        settings.control_plane_url,
        assignment_id,
        capability_token,
        oidc_token,
    )
    try:
        view = await client.get_assignment()
        if view.assignment.kind != AssignmentKind.VERIFY:
            raise WorkerError("verify command received a non-verifier assignment")
        verification = verify_candidates(
            root,
            view.repository_config,
            view.candidates,
        )
        return await client.submit(AssignmentResultRequest(verification=verification))
    except Exception as exc:
        with suppress(Exception):
            await client.submit(AssignmentResultRequest(error=str(exc)[:2000]))
        raise
