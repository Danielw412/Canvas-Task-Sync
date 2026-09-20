"""Background entry point used by the Windows scheduled startup task."""

from __future__ import annotations

import argparse
import codecs
import subprocess
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from canvas_task_sync import cli
from canvas_task_sync.web_constants import (
    DEFAULT_REMOTE_BACKEND_PORT,
    DEFAULT_SIMPLE_WEB_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-task-sync-windows-startup",
        description="Start Canvas Task Sync without opening a browser or console window.",
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--simple-port", type=int, default=DEFAULT_SIMPLE_WEB_PORT)
    parser.add_argument(
        "--ssh-target",
        help=(
            "user@host of the machine running the authoritative backend. When given, this "
            "process keeps an SSH tunnel open and serves dashboards only."
        ),
    )
    parser.add_argument("--tunnel-port", type=int, default=DEFAULT_REMOTE_BACKEND_PORT)
    parser.add_argument("--remote-backend-port", type=int, default=DEFAULT_WEB_PORT)
    return parser


class _TunnelSupervisor:
    """Keeps ``ssh -N -L`` alive for as long as the dashboards are running.

    A laptop sleeps, changes networks, and loses the server; restarting the tunnel is the
    difference between the dashboards recovering by themselves and needing a sign-out.
    """

    RETRY_SECONDS = 5

    def __init__(self, *, target: str, local_port: int, remote_port: int) -> None:
        self.command = [
            "ssh",
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "BatchMode=yes",
            "-L",
            f"{local_port}:{DEFAULT_WEB_HOST}:{remote_port}",
            target,
        ]
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(target=self._run, name="ssh-tunnel", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _run(self) -> None:
        while not self._stopping.is_set():
            print(f"[{datetime.now().astimezone().isoformat()}] Opening SSH tunnel.")
            try:
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                print(f"SSH tunnel could not start: {error}")
            else:
                self._process.wait()
                if self._stopping.is_set():
                    return
                print("SSH tunnel exited; reconnecting.")
            self._stopping.wait(self.RETRY_SECONDS)


def _wait_for_tunnel(port: int, *, timeout: float = 30.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1.0)
            if probe.connect_ex((DEFAULT_WEB_HOST, port)) == 0:
                return True
        time.sleep(0.5)
    return False


def _archive_legacy_log(log_path: Path) -> Path | None:
    if not log_path.exists() or log_path.stat().st_size < 2:
        return None

    with log_path.open("rb") as log_file:
        prefix = log_file.read(2)
    if prefix not in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return None

    archive_path = log_path.with_name(f"{log_path.name}.legacy")
    if archive_path.exists():
        archive_path = log_path.with_name(
            f"{log_path.name}.{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}.legacy"
        )
    log_path.replace(archive_path)
    return archive_path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    archived_log_path = _archive_legacy_log(args.log_path)

    with (
        args.log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as log_file,
        redirect_stdout(log_file),
        redirect_stderr(log_file),
    ):
        if archived_log_path is not None:
            print(f"Archived legacy startup log: {archived_log_path}")
        print(
            f"[{datetime.now().astimezone().isoformat()}] "
            "Starting Canvas Task Sync Windows background server."
        )
        tunnel: _TunnelSupervisor | None = None
        command = [
            "--config",
            str(args.config),
            "web",
            "--port",
            str(args.port),
            "--simple-port",
            str(args.simple_port),
            "--no-open",
        ]
        if args.ssh_target:
            # Dashboard-only mode: this machine never loads course configuration, Google
            # credentials, or the operational database.
            tunnel = _TunnelSupervisor(
                target=args.ssh_target,
                local_port=args.tunnel_port,
                remote_port=args.remote_backend_port,
            )
            tunnel.start()
            if not _wait_for_tunnel(args.tunnel_port):
                print("SSH tunnel is not listening yet; the dashboards will retry on demand.")
            command += [
                "--remote",
                f"http://{DEFAULT_WEB_HOST}:{args.tunnel_port}",
                "--remote-host-header",
                f"{DEFAULT_WEB_HOST}:{args.remote_backend_port}",
            ]
        try:
            return cli.main(command)
        except BaseException:
            traceback.print_exc()
            return 1
        finally:
            if tunnel is not None:
                tunnel.stop()
            print(
                f"[{datetime.now().astimezone().isoformat()}] "
                "Canvas Task Sync Windows background server stopped."
            )


if __name__ == "__main__":
    raise SystemExit(main())
