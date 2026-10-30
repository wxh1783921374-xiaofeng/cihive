from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Protocol

import httpx
import jwt
from jwt import PyJWKClient

from cihive.config import Settings
from cihive.models import WorkerAssignment, utc_now


class AuthenticationError(ValueError):
    pass


def verify_webhook_signature(body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def create_capability_token() -> str:
    return secrets.token_urlsafe(32)


def hash_capability(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_capability(token: str | None, expected_hash: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(hash_capability(token), expected_hash)


def create_github_app_jwt(app_id: int, private_key: str) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


class IdentityVerifier(Protocol):
    async def verify(self, oidc_token: str | None, assignment: WorkerAssignment) -> None: ...


class GitHubOIDCVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwk_client = PyJWKClient(settings.github_oidc_jwks_url)

    async def verify(self, oidc_token: str | None, assignment: WorkerAssignment) -> None:
        if not self.settings.require_oidc:
            return
        if not oidc_token:
            raise AuthenticationError("GitHub OIDC token is required")
        try:
            signing_key = await asyncio.to_thread(
                self._jwk_client.get_signing_key_from_jwt,
                oidc_token,
            )
            claims = jwt.decode(
                oidc_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.github_oidc_audience,
                issuer=self.settings.github_oidc_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "repository", "workflow_ref"]},
            )
        except Exception as exc:
            raise AuthenticationError(f"invalid GitHub OIDC token: {exc}") from exc

        if claims.get("repository") != assignment.repo:
            raise AuthenticationError("OIDC repository does not match assignment")
        expected_workflow = (
            f"{assignment.repo}/{assignment.workflow_file}@refs/heads/{assignment.default_branch}"
        )
        if claims.get("workflow_ref") != expected_workflow:
            raise AuthenticationError("OIDC workflow_ref does not match the trusted workflow")
        if claims.get("ref") not in {
            f"refs/heads/{assignment.default_branch}",
            assignment.default_branch,
        }:
            raise AuthenticationError("OIDC ref does not match the default branch")


class GitHubOIDCClient:
    async def get_token(self, audience: str) -> str | None:
        request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not request_url or not request_token:
            return None
        separator = "&" if "?" in request_url else "?"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{request_url}{separator}audience={audience}",
                headers={"Authorization": f"Bearer {request_token}"},
            )
            response.raise_for_status()
            payload = response.json()
        return payload.get("value")


def assignment_expiry(settings: Settings) -> datetime:
    return utc_now() + timedelta(seconds=settings.assignment_ttl_seconds)
