from __future__ import annotations

from pathlib import Path

from cihive.models import CandidatePatch, RepositoryConfig
from cihive.verification import verify_candidates
from tests.conftest import init_git_repo, make_patch


def test_verification_requires_reproduction_and_passing_patch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(
        root,
        {
            "mathx.py": "def add(a, b):\n    return a - b\n",
            "check.py": "from mathx import add\nassert add(2, 3) == 5\n",
        },
    )
    config = RepositoryConfig(verification_commands=[["python", "check.py"]])
    candidate = CandidatePatch(
        id="candidate-1",
        run_id="run-1",
        assignment_id="assignment-1",
        worker_index=0,
        summary="Fix",
        root_cause="Wrong operator",
        unified_diff=make_patch(),
        file_contents={"mathx.py": "def add(a, b):\n    return a + b\n"},
        changed_files=["mathx.py"],
        changed_lines=2,
        confidence=0.9,
    )
    result = verify_candidates(root, config, [candidate])
    assert result.reproduced
    assert result.candidates[0].passed
    assert result.candidates[0].checks[0].exit_code == 0


def test_verification_rejects_forbidden_test_change(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root, {"check.py": "raise SystemExit(1)\n"})
    patch = (
        "diff --git a/tests/test_check.py b/tests/test_check.py\n"
        "--- a/tests/test_check.py\n"
        "+++ b/tests/test_check.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n"
    )
    candidate = CandidatePatch(
        id="bad",
        run_id="run",
        assignment_id="assignment",
        worker_index=0,
        summary="bad",
        root_cause="bad",
        unified_diff=patch,
        file_contents={"tests/test_check.py": "assert True\n"},
        changed_files=["tests/test_check.py"],
        changed_lines=2,
        confidence=0.2,
    )
    result = verify_candidates(
        root,
        RepositoryConfig(verification_commands=[["python", "check.py"]]),
        [candidate],
    )
    assert not result.candidates[0].passed
    assert "forbidden" in result.candidates[0].reason


def test_setup_failure_blocks_candidates(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(
        root,
        {
            "mathx.py": "def add(a, b):\n    return a - b\n",
            "check.py": "from mathx import add\nassert add(2, 3) == 5\n",
        },
    )
    candidate = CandidatePatch(
        id="candidate",
        run_id="run",
        assignment_id="assignment",
        worker_index=0,
        summary="Fix",
        root_cause="Wrong operator",
        unified_diff=make_patch(),
        file_contents={"mathx.py": "def add(a, b):\n    return a + b\n"},
        changed_files=["mathx.py"],
        changed_lines=2,
        confidence=0.9,
    )
    result = verify_candidates(
        root,
        RepositoryConfig(
            setup_commands=[["python", "-c", "raise SystemExit(1)"]],
            verification_commands=[["python", "check.py"]],
        ),
        [candidate],
    )
    assert not result.reproduced
    assert result.setup_checks[0].exit_code == 1
    assert not result.candidates[0].passed
    assert "setup commands failed" in result.candidates[0].reason
