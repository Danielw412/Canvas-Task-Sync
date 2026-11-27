"""The server install script stops on a repository path the systemd unit cannot hold."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install-server-service.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="the install script is for a Linux server and needs bash",
)


def _run(tmp_path: Path, directory: str) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / directory / "deploy"
    deploy.mkdir(parents=True)
    script = deploy / SCRIPT.name
    shutil.copy(SCRIPT, script)
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=False, timeout=30
    )


@pytest.mark.parametrize("directory", ["R&D", "has space", "a|b", "100%"])
def test_the_install_script_refuses_a_path_systemd_cannot_hold(tmp_path, directory):
    result = _run(tmp_path, directory)

    assert result.returncode == 1
    assert "systemd unit cannot hold" in result.stderr


def test_the_install_script_accepts_a_plain_path(tmp_path):
    result = _run(tmp_path, "plain")

    assert result.returncode == 1  # it stops later, at the missing virtual environment
    assert ".venv/bin/python is missing" in result.stderr
    assert "cannot hold" not in result.stderr
