from __future__ import annotations

from pathlib import Path

from cihive.evaluation import run_evaluation_suite


def test_fixture_evaluation_suite() -> None:
    report = run_evaluation_suite(Path("evals/cases.yml"))
    assert report.cases >= 10
    assert report.expected_outcomes_met == report.cases
    assert report.invalid_prs == 0
