from __future__ import annotations

import io
import zipfile

from cihive.failure import (
    build_failure_context,
    logs_zip_to_text,
    normalize_error,
)
from tests.conftest import make_failure_run


def test_zip_logs_and_secret_redaction() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("job.txt", "AssertionError\nTOKEN=abcdefghijklmnop")
    text = logs_zip_to_text(payload.getvalue())
    context = build_failure_context(make_failure_run(), text, [], 10_000)
    assert "AssertionError" in context.logs_excerpt
    assert "abcdefghijklmnop" not in context.logs_excerpt


def test_failure_signature_is_stable_across_volatile_values() -> None:
    first = normalize_error("AssertionError at /tmp/run-123 after 42ms with deadbeef")
    second = normalize_error("AssertionError at /tmp/run-999 after 8ms with cafebabe")
    assert first == second


def test_zip_log_limit_is_enforced() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("job.txt", "x" * 1000)
    text = logs_zip_to_text(payload.getvalue(), max_bytes=100)
    assert "LOG SIZE LIMIT REACHED" in text
    assert "x" * 1000 not in text
