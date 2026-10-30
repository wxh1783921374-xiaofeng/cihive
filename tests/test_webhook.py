from __future__ import annotations

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from cihive.api import create_app
from tests.conftest import FakeGitHub, FakeModel, webhook_payload


def _sign(body: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict, delivery: str = "delivery-1"):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": "workflow_run",
            "Content-Type": "application/json",
        },
    )


def test_signature_and_idempotency(settings, store) -> None:
    app = create_app(settings, store, FakeGitHub(), FakeModel())
    with TestClient(app) as client:
        payload = webhook_payload()
        body = json.dumps(payload).encode()
        rejected = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": "sha256=bad",
                "X-GitHub-Delivery": "bad",
                "X-GitHub-Event": "workflow_run",
            },
        )
        assert rejected.status_code == 401

        accepted = _post(client, payload)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "accepted"
        duplicate = _post(client, payload, "delivery-1")
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "duplicate"


def test_non_default_branch_is_ignored(settings, store) -> None:
    app = create_app(settings, store, FakeGitHub(), FakeModel())
    payload = webhook_payload()
    payload["workflow_run"]["head_branch"] = "feature"
    with TestClient(app) as client:
        response = _post(client, payload)
    assert response.json()["status"] == "ignored"


def test_assignment_requires_capability(settings, store) -> None:
    github = FakeGitHub()
    app = create_app(settings, store, github, FakeModel())
    with TestClient(app) as client:
        response = _post(client, webhook_payload())
        run_id = response.json()["run_id"]
        assignment, token = github.dispatched["workers"][0]
        missing = client.get(f"/v1/assignments/{assignment.id}")
        assert missing.status_code == 401
        ok = client.get(
            f"/v1/assignments/{assignment.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200
        assert ok.json()["assignment"]["run_id"] == run_id


def test_oversized_webhook_is_rejected(settings, store) -> None:
    limited = settings.model_copy(update={"max_webhook_bytes": 200})
    app = create_app(limited, store, FakeGitHub(), FakeModel())
    with TestClient(app) as client:
        response = _post(client, webhook_payload())
    assert response.status_code == 413
