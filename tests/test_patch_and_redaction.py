from __future__ import annotations

import pytest

from cihive.models import RepositoryConfig, safe_relative_path
from cihive.patch import parse_patch_paths, validate_patch
from cihive.redaction import redact_text
from tests.conftest import make_patch


def test_redacts_common_secret_shapes() -> None:
    value = (
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456\n"
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n"
        "password: very-secret-value"
    )
    redacted = redact_text(value)
    assert "ghp_" not in redacted
    assert "sk-abc" not in redacted
    assert "very-secret-value" not in redacted
    assert "[REDACTED]" in redacted


def test_safe_patch_passes() -> None:
    patch = make_patch()
    config = RepositoryConfig(verification_commands=[["true"]])
    result = validate_patch(
        patch,
        {"mathx.py": "def add(a, b):\n    return a + b\n"},
        config,
    )
    assert result.valid
    assert result.changed_files == ["mathx.py"]
    assert result.changed_lines == 2


@pytest.mark.parametrize(
    ("path", "expected_pattern"),
    [
        ("tests/test_math.py", "tests/**"),
        (".github/workflows/release.yml", ".github/workflows/**"),
        ("app.env", None),
        (".env", "**/.env"),
        ("secrets/prod.txt", "**/secrets/**"),
    ],
)
def test_forbidden_paths(path: str, expected_pattern: str | None) -> None:
    patch = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
    result = validate_patch(
        patch,
        {path: "new\n"},
        RepositoryConfig(verification_commands=[["true"]]),
    )
    assert result.valid is (expected_pattern is None)
    if expected_pattern:
        assert result.errors


def test_patch_must_match_file_payload() -> None:
    result = validate_patch(
        make_patch(),
        {"other.py": "content"},
        RepositoryConfig(verification_commands=[["true"]]),
    )
    assert not result.valid
    assert any("missing final content" in error for error in result.errors)


def test_unsafe_paths_are_rejected() -> None:
    with pytest.raises(ValueError):
        safe_relative_path("../secret")
    with pytest.raises(ValueError):
        safe_relative_path("/absolute")
    with pytest.raises(ValueError):
        safe_relative_path("source.py\nmalicious")
    assert parse_patch_paths(make_patch()) == ["mathx.py"]
