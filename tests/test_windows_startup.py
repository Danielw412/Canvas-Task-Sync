from __future__ import annotations

from pathlib import Path

from canvas_task_sync import windows_startup

ROOT = Path(__file__).parents[1]
INSTALL_SCRIPT = ROOT / "scripts" / "install-windows-startup.ps1"
REMOVE_SCRIPT = ROOT / "scripts" / "remove-windows-startup.ps1"
START_SCRIPT = ROOT / "scripts" / "start-windows-web.ps1"


def test_windows_startup_entrypoint_forces_no_browser_and_logs(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_cli_main(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(windows_startup.cli, "main", fake_cli_main)
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
            "8790",
            "--simple-port",
            "8791",
            "--no-open",
        ]
    ]
    log = log_path.read_text(encoding="utf-8")
    assert "Starting Canvas Task Sync Windows background server." in log
    assert "Canvas Task Sync Windows background server stopped." in log


def test_windows_startup_archives_legacy_utf16_log(tmp_path, monkeypatch):
    monkeypatch.setattr(windows_startup.cli, "main", lambda argv: 0)
    log_path = tmp_path / "web-startup.log"
    log_path.write_text("legacy startup log", encoding="utf-16")

    assert windows_startup.main(
        ["--config", str(tmp_path / "courses.yaml"), "--log-path", str(log_path)]
    ) == 0

    archive_path = tmp_path / "web-startup.log.legacy"
    assert archive_path.read_text(encoding="utf-16") == "legacy startup log"
    assert "Archived legacy startup log:" in log_path.read_text(encoding="utf-8")


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
    assert "8791" in installer
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
