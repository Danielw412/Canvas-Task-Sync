from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from canvas_task_sync import cli, windows_startup

ROOT = Path(__file__).parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install-windows-startup.ps1"
REMOVE_SCRIPT = ROOT / "scripts" / "remove-windows-startup.ps1"
START_SCRIPT = ROOT / "scripts" / "start-windows-web.ps1"


def test_windows_startup_entrypoint_forces_no_browser_and_logs(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_cli_main(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(cli, "main", fake_cli_main)
    log_path = tmp_path / ".canvas-task-sync" / "web-startup.log"

    assert windows_startup.main(
        [
            "--config",
            str(tmp_path / "courses.yaml"),
            "--log-path",
            str(log_path),
        ]
    ) == 0

    assert calls == [
        [
            "--config",
            str(tmp_path / "courses.yaml"),
            "web",
            "--port",
            "8890",
            "--simple-port",
            "8891",
            "--no-open",
        ]
    ]
    log = log_path.read_text(encoding="utf-8")
    assert "Starting Canvas Task Sync Windows background server." in log
    assert "Canvas Task Sync Windows background server stopped." in log


def test_windows_startup_archives_legacy_utf16_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    log_path = tmp_path / "web-startup.log"
    log_path.write_text("legacy startup log", encoding="utf-16")

    assert windows_startup.main(
        ["--config", str(tmp_path / "courses.yaml"), "--log-path", str(log_path)]
    ) == 0

    archive_path = tmp_path / "web-startup.log.legacy"
    assert archive_path.read_text(encoding="utf-16") == "legacy startup log"
    assert "Archived legacy startup log:" in log_path.read_text(encoding="utf-8")


def test_an_oversized_startup_log_is_rotated_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "main", lambda argv: 0)
    log_path = tmp_path / "web-startup.log"
    log_path.write_text("x" * (windows_startup.MAX_LOG_BYTES + 1), encoding="utf-8")

    windows_startup.main(["--config", str(tmp_path / "courses.yaml"), "--log-path", str(log_path)])

    assert (tmp_path / "web-startup.log.1").stat().st_size == windows_startup.MAX_LOG_BYTES + 1
    assert log_path.stat().st_size < 1000


def test_server_mode_only_forwards_both_dashboard_ports_over_ssh(tmp_path, monkeypatch):
    commands: list[list[str]] = []

    def fake_run(self) -> None:
        commands.append(self.command)

    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("server mode must not run the app"))
    monkeypatch.setattr(windows_startup, "_end_children_with_this_process", lambda: None)
    monkeypatch.setattr(windows_startup._TunnelSupervisor, "run", fake_run)
    log_path = tmp_path / "web-startup.log"

    assert windows_startup.main(
        [
            "--config",
            str(tmp_path / "courses.yaml"),
            "--log-path",
            str(log_path),
            "--ssh-target",
            "daniel@server",
        ]
    ) == 0

    [command] = commands
    assert command[0] == "ssh"
    assert command[-1] == "daniel@server"
    forwards = [command[index + 1] for index, arg in enumerate(command) if arg == "-L"]
    # The browser keeps using 127.0.0.1:8890 and :8891; both now live on the server.
    assert forwards == ["127.0.0.1:8890:127.0.0.1:8790", "127.0.0.1:8891:127.0.0.1:8891"]
    assert "BatchMode=yes" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "Dashboards are hosted on daniel@server" in log_path.read_text(encoding="utf-8")


def test_tunnel_is_reopened_with_backoff_after_it_drops(monkeypatch):
    class ExitedProcess:
        def wait(self) -> int:
            return 255

        def poll(self) -> int:
            return 255

    launches: list[list[str]] = []
    delays: list[float] = []

    def fake_popen(command, **kwargs):
        launches.append(command)
        assert kwargs["stdin"] is subprocess.DEVNULL
        return ExitedProcess()

    supervisor = windows_startup._TunnelSupervisor(["ssh", "-N", "server"])

    def fake_wait(delay: float) -> bool:
        delays.append(delay)
        if len(delays) == 6:
            supervisor._stopping.set()
        return supervisor._stopping.is_set()

    monkeypatch.setattr(windows_startup.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(supervisor._stopping, "wait", fake_wait)

    supervisor.run()

    assert len(launches) == 6
    assert delays == [5, 10, 20, 40, 60, 60]


def test_server_mode_does_not_load_the_application():
    # This process sits in memory all day, so it must not import the web stack. A
    # subprocess is the only honest check: this test session has already imported it.
    heavy = ["canvas_task_sync.cli", "fastapi", "pydantic", "uvicorn", "starlette"]
    probe = (
        "import sys;"
        "import canvas_task_sync.windows_startup;"
        f"print(','.join(m for m in {heavy!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert result.stdout.strip() == ""


def test_windows_installer_uses_a_hidden_windowless_task_and_is_idempotent():
    installer = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "$pythonwPath" in installer
    assert "-Execute $pythonwPath" in installer
    assert "canvas_task_sync.windows_startup" in installer
    assert "-Hidden" in installer
    assert "-MultipleInstances IgnoreNew" in installer
    assert "Stop-ScheduledTask" in installer
    assert "canvas_task_sync.windows_startup" in installer
    assert "Get-NetTCPConnection" in installer
    assert "Stop-Process -Id $staleServer.ProcessId" in installer
    assert "$listenerIsReplacement" in installer
    # A leftover tunnel would hold the dashboard ports, so the installer clears it too.
    assert "$staleTunnels" in installer
    assert "--remote-simple-port" in installer
    assert "Register-ScheduledTask" in installer
    assert "-Force" in installer
    assert "powershell.exe" not in installer
    assert "webbrowser" not in installer


def test_startup_keeps_desktop_shortcut_and_remove_script_removes_task_and_shortcut():
    installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
    remover = REMOVE_SCRIPT.read_text(encoding="utf-8")
    launcher = START_SCRIPT.read_text(encoding="utf-8")

    assert "Canvas Task Sync.url" in installer
    assert "Canvas Task Sync Simple.url" in installer
    assert "8891" in installer
    assert "Canvas Task Sync.url" in remover
    assert "Canvas Task Sync Simple.url" in remover
    assert "Unregister-ScheduledTask" in remover
    assert "Remove-Item" in remover
    assert "--log-path" in launcher
    assert "windows_startup" in launcher
    assert "$pythonwPath" in launcher
    assert "Start-Process" in launcher
    assert "-WindowStyle Hidden" in launcher
    assert "foreground diagnostic server" not in launcher
