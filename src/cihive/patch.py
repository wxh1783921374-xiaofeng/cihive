from __future__ import annotations

import fnmatch
import hashlib
import re

from cihive.models import (
    MANDATORY_FORBIDDEN_PATHS,
    PatchValidation,
    RepositoryConfig,
    safe_relative_path,
)

_DIFF_PATH = re.compile(r"^(?:diff --git a/|\+\+\+ b/)(.+?)(?:\t|$)")


def parse_patch_paths(patch: str) -> list[str]:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if match:
                paths.add(safe_relative_path(match.group(2)))
        elif line.startswith("+++ b/"):
            paths.add(safe_relative_path(line[6:]))
    return sorted(paths)


def _matches_path(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/"):
        return _matches_path(path, pattern[3:])
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return False


def forbidden_reason(path: str, patterns: list[str]) -> str | None:
    for pattern in sorted(set(patterns).union(MANDATORY_FORBIDDEN_PATHS)):
        if _matches_path(path, pattern):
            return pattern
    return None


def changed_line_count(patch: str) -> int:
    count = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line.startswith(("+", "-")):
            count += 1
    return count


def patch_sha256(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


def validate_patch(
    patch: str,
    file_contents: dict[str, str],
    config: RepositoryConfig,
) -> PatchValidation:
    errors: list[str] = []
    if len(patch.encode("utf-8")) > 1_000_000:
        errors.append("patch exceeds 1 MB")
    if "GIT binary patch" in patch or "Binary files " in patch:
        errors.append("binary patches are forbidden")
    if re.search(r"(?m)^(?:rename|copy) (?:from|to)\b", patch):
        errors.append("renames and copies are forbidden")
    if re.search(r"(?m)^deleted file mode\b", patch):
        errors.append("file deletion is forbidden")
    if re.search(r"(?m)^(?:old|new) mode \d+\b", patch):
        errors.append("file mode changes are forbidden")
    if re.search(r"(?m)^new file mode (?:120000|160000)\b", patch):
        errors.append("symlinks and submodules are forbidden")

    try:
        paths = parse_patch_paths(patch)
    except ValueError as exc:
        return PatchValidation(valid=False, errors=[str(exc)])

    if not paths:
        errors.append("patch does not modify any files")
    if len(paths) > config.max_patch_files:
        errors.append(f"patch changes {len(paths)} files; limit is {config.max_patch_files}")
    lines = changed_line_count(patch)
    if lines == 0:
        errors.append("patch does not change any lines")
    if lines > config.max_patch_lines:
        errors.append(f"patch changes {lines} lines; limit is {config.max_patch_lines}")

    for path in paths:
        reason = forbidden_reason(path, config.forbidden_paths)
        if reason:
            errors.append(f"{path} is forbidden by {reason}")
        if path not in file_contents:
            errors.append(f"missing final content for {path}")
            continue
        if "\x00" in file_contents[path]:
            errors.append(f"{path} contains binary data")
        if len(file_contents[path].encode("utf-8")) > 512_000:
            errors.append(f"{path} exceeds the 512 KB file limit")

    for path in file_contents:
        try:
            normalized = safe_relative_path(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if normalized not in paths:
            errors.append(f"{normalized} has final content but is absent from the patch")

    total_bytes = sum(len(value.encode("utf-8")) for value in file_contents.values())
    if total_bytes > 2_000_000:
        errors.append("candidate file payload exceeds 2 MB")

    return PatchValidation(
        valid=not errors,
        changed_files=paths,
        changed_lines=lines,
        errors=sorted(set(errors)),
    )
