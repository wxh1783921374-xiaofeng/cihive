from __future__ import annotations

import hashlib
import io
import re
import zipfile

from cihive.models import FailedJob, FailureContext, FailureRun, FailureSignature
from cihive.redaction import redact_text, truncate_text

_VOLATILE_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b", re.IGNORECASE), "<duration>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"(?i)(?:[A-Z]:\\|/)(?:[^\s:]+/)+"), "<path>/"),
]

_ERROR_HINTS = (
    "error",
    "failed",
    "failure",
    "exception",
    "traceback",
    "assert",
    "not found",
    "cannot",
    "timeout",
    "fatal",
)


def logs_zip_to_text(payload: bytes, max_bytes: int = 10_000_000) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            parts: list[str] = []
            total = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if total + info.file_size > max_bytes:
                    parts.append("\n[LOG SIZE LIMIT REACHED]\n")
                    break
                try:
                    content = archive.read(info).decode("utf-8", errors="replace")
                except (KeyError, RuntimeError):
                    continue
                parts.append(f"\n===== {info.filename} =====\n{content}")
                total += len(content.encode("utf-8"))
            return "".join(parts)
    except zipfile.BadZipFile:
        return payload.decode("utf-8", errors="replace")


def extract_log_excerpt(logs: str, max_chars: int) -> str:
    redacted = redact_text(logs)
    lines = [line.rstrip() for line in redacted.splitlines()]
    non_empty = [line for line in lines if line.strip()]
    hinted = [line for line in non_empty if any(hint in line.lower() for hint in _ERROR_HINTS)]
    selected = hinted[-120:] if hinted else non_empty[-120:]
    excerpt = "\n".join(selected)
    return truncate_text(excerpt, max_chars)


def normalize_error(excerpt: str) -> str:
    normalized = redact_text(excerpt).lower()
    for pattern, replacement in _VOLATILE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return truncate_text(normalized, 4000)


def build_failure_context(
    run: FailureRun,
    logs: str,
    jobs: list[FailedJob],
    max_log_chars: int,
) -> FailureContext:
    excerpt = extract_log_excerpt(logs, max_log_chars)
    normalized = normalize_error(excerpt)
    failed_job_names = [job.name for job in jobs]
    failed_steps = [step for job in jobs for step in job.failed_steps]
    material = "|".join(
        [
            run.workflow_path,
            *failed_job_names,
            *failed_steps,
            normalized,
        ]
    )
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    summary = " / ".join(failed_job_names[:3]) or run.workflow_name
    if failed_steps:
        summary = f"{summary} :: {' / '.join(failed_steps[:3])}"
    signature = FailureSignature(
        fingerprint=fingerprint,
        workflow_path=run.workflow_path,
        failed_jobs=failed_job_names,
        failed_steps=failed_steps,
        normalized_error=normalized,
    )
    return FailureContext(
        run_id=run.id,
        summary=summary,
        logs_excerpt=excerpt,
        failed_jobs=jobs,
        signature=signature,
    )


def parse_jobs(payload: dict[str, object]) -> list[FailedJob]:
    result: list[FailedJob] = []
    jobs = payload.get("jobs", [])
    if not isinstance(jobs, list):
        return result
    for raw_job in jobs:
        if not isinstance(raw_job, dict):
            continue
        conclusion = raw_job.get("conclusion")
        if conclusion not in {"failure", "timed_out", "cancelled"}:
            continue
        steps = raw_job.get("steps", [])
        failed_steps: list[str] = []
        if isinstance(steps, list):
            for step in steps:
                if (
                    isinstance(step, dict)
                    and step.get("conclusion") in {"failure", "timed_out"}
                    and isinstance(step.get("name"), str)
                ):
                    failed_steps.append(step["name"])
        name = raw_job.get("name")
        if isinstance(name, str):
            result.append(
                FailedJob(
                    name=name,
                    conclusion=str(conclusion),
                    failed_steps=failed_steps,
                )
            )
    return result
