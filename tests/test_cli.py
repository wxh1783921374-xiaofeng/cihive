from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from cihive.cli import app


def test_init_writes_config_and_workflow(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "init",
            "--directory",
            str(tmp_path),
            "--command",
            "python -m pytest -q",
            "--setup-command",
            "python -m pip install -e .",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".cihive.yml").exists()
    assert (tmp_path / ".github" / "workflows" / "cihive.yml").exists()
    config = yaml.safe_load((tmp_path / ".cihive.yml").read_text(encoding="utf-8"))
    assert config["setup_commands"] == [["python", "-m", "pip", "install", "-e", "."]]
    assert config["verification_commands"] == [["python", "-m", "pytest", "-q"]]
    assert "repository_dispatch" in (tmp_path / ".github" / "workflows" / "cihive.yml").read_text()


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["init", "--directory", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0
    second = runner.invoke(app, args)
    assert second.exit_code != 0
