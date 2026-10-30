from __future__ import annotations

import asyncio
import base64
import html
from datetime import datetime
from typing import Any

import httpx
import yaml

from cihive.config import Settings
from cihive.models import (
    CandidatePatch,
    RepositoryConfig,
    RunState,
    WorkerAssignment,
    utc_now,
)
from cihive.patch import patch_sha256
from cihive.security import create_github_app_jwt


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        static_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.static_token = static_token
        self.transport = transport
        self._installation_tokens: dict[int, tuple[str, datetime]] = {}

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._installation_tokens.get(installation_id)
        if cached and cached[1] > utc_now():
            return cached[0]
        if not self.settings.github_app_id:
            raise GitHubError("CIHIVE_GITHUB_APP_ID is not configured")
        private_key = self.settings.load_private_key()
        if not private_key:
            raise GitHubError("GitHub App private key is not configured")
        app_jwt = create_github_app_jwt(self.settings.github_app_id, private_key)
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.post(
                f"{self.settings.github_api_url}/app/installations/{installation_id}/access_tokens",
                headers=self._headers(app_jwt),
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"could not create installation token: {response.text[:500]}",
                response.status_code,
            )
        payload = response.json()
        expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        token = payload["token"]
        self._installation_tokens[installation_id] = (token, expires_at)
        return token

    async def _token(self, installation_id: int | None) -> str:
        if self.static_token:
            return self.static_token
        if installation_id is None:
            raise GitHubError("GitHub installation ID is required")
        return await self._installation_token(installation_id)

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CIHive/0.1",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        installation_id: int | None,
        json: dict[str, Any] | None = None,
        expected_status: tuple[int, ...] = (200, 201),
    ) -> dict[str, Any]:
        token = await self._token(installation_id)
        response: httpx.Response | None = None
        for attempt in range(3):
            async with httpx.AsyncClient(timeout=60, transport=self.transport) as client:
                response = await client.request(
                    method,
                    f"{self.settings.github_api_url}{path}",
                    headers=self._headers(token),
                    json=json,
                )
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            await asyncio.sleep(min(delay, 5))
        assert response is not None
        if response.status_code not in expected_status:
            raise GitHubError(
                f"GitHub API {method} {path} failed: {response.text[:1000]}",
                response.status_code,
            )
        if not response.content:
            return {}
        return response.json()

    async def fetch_workflow_run(
        self,
        repo: str,
        run_id: int,
        installation_id: int | None,
    ) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}",
            installation_id=installation_id,
        )

    async def list_jobs(
        self,
        repo: str,
        run_id: int,
        installation_id: int | None,
    ) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
            installation_id=installation_id,
        )

    async def download_logs(
        self,
        repo: str,
        run_id: int,
        installation_id: int | None,
    ) -> bytes:
        token = await self._token(installation_id)
        async with httpx.AsyncClient(
            timeout=90,
            follow_redirects=True,
            transport=self.transport,
        ) as client:
            response = await client.get(
                f"{self.settings.github_api_url}/repos/{repo}/actions/runs/{run_id}/logs",
                headers=self._headers(token),
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"could not download workflow logs: {response.text[:500]}",
                response.status_code,
            )
        return response.content

    async def fetch_repository_config(
        self,
        repo: str,
        ref: str,
        installation_id: int | None,
    ) -> RepositoryConfig:
        try:
            payload = await self.request(
                "GET",
                f"/repos/{repo}/contents/.cihive.yml?ref={ref}",
                installation_id=installation_id,
            )
        except GitHubError as exc:
            if exc.status_code == 404:
                raise GitHubError(
                    ".cihive.yml is missing; run `cihive init` on the default branch",
                    404,
                ) from exc
            raise
        encoded = payload.get("content", "")
        try:
            content = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubError(".cihive.yml is not valid UTF-8") from exc
        try:
            parsed = yaml.safe_load(content)
            return RepositoryConfig.model_validate(parsed)
        except (yaml.YAMLError, ValueError) as exc:
            raise GitHubError(f"invalid .cihive.yml: {exc}") from exc

    async def dispatch_run(
        self,
        run: RunState,
        repair_assignments: list[tuple[WorkerAssignment, str]],
        verify_assignment: tuple[WorkerAssignment, str],
    ) -> None:
        payload = {
            "run_id": run.run.id,
            "repo": run.run.repo,
            "head_sha": run.run.head_sha,
            "workers": [
                {"id": assignment.id, "token": token} for assignment, token in repair_assignments
            ],
            "verifier": {
                "id": verify_assignment[0].id,
                "token": verify_assignment[1],
            },
        }
        await self.request(
            "POST",
            f"/repos/{run.run.repo}/dispatches",
            installation_id=run.run.installation_id,
            json={"event_type": "cihive-run", "client_payload": payload},
            expected_status=(204,),
        )

    async def publish_draft_pr(
        self,
        run: RunState,
        candidate: CandidatePatch,
    ) -> str:
        installation_id = run.run.installation_id
        head = await self.request(
            "GET",
            f"/repos/{run.run.repo}/git/commits/{run.run.head_sha}",
            installation_id=installation_id,
        )
        existing_tree = await self.request(
            "GET",
            f"/repos/{run.run.repo}/git/trees/{head['tree']['sha']}?recursive=1",
            installation_id=installation_id,
        )
        existing_modes = {
            str(entry["path"]): str(entry["mode"])
            for entry in existing_tree.get("tree", [])
            if entry.get("type") == "blob"
        }
        tree_entries: list[dict[str, str]] = []
        for path, content in candidate.file_contents.items():
            mode = existing_modes.get(path, "100644")
            if mode == "120000":
                raise GitHubError(f"refusing to publish symlink change for {path}")
            blob = await self.request(
                "POST",
                f"/repos/{run.run.repo}/git/blobs",
                installation_id=installation_id,
                json={"content": content, "encoding": "utf-8"},
            )
            tree_entries.append(
                {
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree = await self.request(
            "POST",
            f"/repos/{run.run.repo}/git/trees",
            installation_id=installation_id,
            json={"base_tree": head["tree"]["sha"], "tree": tree_entries},
        )
        commit = await self.request(
            "POST",
            f"/repos/{run.run.repo}/git/commits",
            installation_id=installation_id,
            json={
                "message": f"CIHive: repair {run.run.workflow_name}",
                "tree": tree["sha"],
                "parents": [run.run.head_sha],
            },
        )
        branch = f"cihive/run-{run.run.id[:8]}-{candidate.id[:8]}"
        await self.request(
            "POST",
            f"/repos/{run.run.repo}/git/refs",
            installation_id=installation_id,
            json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
        )
        verification = run.verification
        safe_root_cause = html.escape(candidate.root_cause).replace("@", "&#64;")
        evidence_lines = [
            "## CIHive evidence",
            "",
            f"- Failure signature: `{run.failure.signature.fingerprint}`",
            f"- Root cause: {safe_root_cause}",
            f"- Patch SHA-256: `{patch_sha256(candidate.unified_diff)}`",
            f"- Changed files: {len(candidate.changed_files)}",
            f"- Changed lines: {candidate.changed_lines}",
            f"- Failure reproduced: {verification.reproduced if verification else False}",
            "",
            "This is a draft proposed by CIHive. CIHive never merges automatically.",
        ]
        if verification:
            evidence_lines.extend(["", "### Verification", ""])
            for check in verification.baseline_checks:
                evidence_lines.append(
                    f"- Baseline `{' '.join(check.command)}`: exit `{check.exit_code}`"
                )
            for candidate_result in verification.candidates:
                if candidate_result.candidate_id != candidate.id:
                    continue
                evidence_lines.append(f"- Candidate: `{candidate_result.reason}`")
                for check in candidate_result.checks:
                    evidence_lines.append(
                        f"- Verify `{' '.join(check.command)}`: exit `{check.exit_code}`"
                    )
        pull = await self.request(
            "POST",
            f"/repos/{run.run.repo}/pulls",
            installation_id=installation_id,
            json={
                "title": f"CIHive: repair failed {run.run.workflow_name}",
                "head": branch,
                "base": run.run.default_branch,
                "body": "\n".join(evidence_lines),
                "draft": True,
            },
        )
        return str(pull["html_url"])
