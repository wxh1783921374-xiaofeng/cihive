from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from cihive.api import create_app
from cihive.github import GitHubClient
from cihive.models import WorkerAssignment
from cihive.security import AuthenticationError, GitHubOIDCVerifier
from tests.conftest import FakeGitHub, FakeModel, webhook_payload
from tests.test_webhook import _post


class _FakeJWKClient:
    def __init__(self, public_key) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, _token: str):
        return SimpleNamespace(key=self.public_key)


async def test_github_oidc_claims_are_bound_to_assignment(settings) -> None:
    settings.require_oidc = True
    verifier = GitHubOIDCVerifier(settings)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier._jwk_client = _FakeJWKClient(private_key.public_key())
    assignment = WorkerAssignment(
        id="assignment",
        run_id="run",
        kind="repair",
        worker_index=0,
        repo="octo/demo",
        head_sha="a" * 40,
        default_branch="main",
        workflow_file=".github/workflows/cihive.yml",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": settings.github_oidc_issuer,
        "aud": settings.github_oidc_audience,
        "iat": now - 1,
        "exp": now + 300,
        "repository": "octo/demo",
        "workflow_ref": "octo/demo/.github/workflows/cihive.yml@refs/heads/main",
        "ref": "refs/heads/main",
    }
    token = jwt.encode(claims, private_key, algorithm="RS256")
    await verifier.verify(token, assignment)

    claims["repository"] = "attacker/repo"
    wrong_repo = jwt.encode(claims, private_key, algorithm="RS256")
    with pytest.raises(AuthenticationError):
        await verifier.verify(wrong_repo, assignment)


def test_model_proxy_enforces_call_budget(settings, store) -> None:
    github = FakeGitHub()
    app = create_app(settings, store, github, FakeModel())
    with TestClient(app) as client:
        response = _post(client, webhook_payload())
        assert response.status_code == 202
        assignment, token = github.dispatched["workers"][0]
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"files": {"mathx.py": "def add(a,b):\n    return a - b\n"}, "max_turns": 3}
        assert (
            client.post(
                f"/v1/assignments/{assignment.id}/agent",
                headers=headers,
                json=payload,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/assignments/{assignment.id}/agent",
                headers=headers,
                json=payload,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/v1/assignments/{assignment.id}/agent",
                headers=headers,
                json=payload,
            ).status_code
            == 429
        )


def test_expired_assignment_is_rejected(settings, store) -> None:
    github = FakeGitHub()
    app = create_app(settings, store, github, FakeModel())
    with TestClient(app) as client:
        _post(client, webhook_payload())
        assignment, token = github.dispatched["workers"][0]
        assignment.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        store.save_assignment(assignment)
        response = client.get(
            f"/v1/assignments/{assignment.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


async def test_github_client_retries_rate_limit(settings) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    client = GitHubClient(
        settings,
        static_token="token",
        transport=httpx.MockTransport(handler),
    )
    result = await client.request("GET", "/test", installation_id=None)
    assert result == {"ok": True}
    assert calls == 2
