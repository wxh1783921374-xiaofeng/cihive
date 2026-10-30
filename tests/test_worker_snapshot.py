from __future__ import annotations

from pathlib import Path

from cihive.worker import collect_repository_files


def test_snapshot_excludes_secrets_binaries_and_heavy_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "private.pem").write_text("secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "index.js").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02")
    files = collect_repository_files(tmp_path, 100_000)
    assert "src/app.py" in files
    assert ".env" not in files
    assert "private.pem" not in files
    assert "node_modules/index.js" not in files
    assert "binary.bin" not in files
