"""Background entry point used by the Windows scheduled startup task.

It runs in one of two modes:

* Without ``--ssh-target``, this machine runs the whole application: the backend, the
  scheduler, and both dashboards.
* With ``--ssh-target``, the server runs all of that and this process only keeps an SSH
  tunnel open, so that ``127.0.0.1:8890`` and ``:8891`` on this machine reach the server.
  This process stays in memory all day, so this mode imports nothing from the web stack
  or the pipeline. ``tests/test_windows_startup.py`` checks that.
"""

from __future__ import annotations

import argparse
import codecs
import subprocess
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import IO

from canvas_task_sync.web_constants import (
    DEFAULT_SERVER_BACKEND_PORT,
    DEFAULT_SIMPLE_WEB_PORT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
)

# The previous log is kept once as ``<name>.1`` so it cannot grow without limit.
MAX_LOG_BYTES = 1_000_000


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
            "user@host of the server that runs Canvas Task Sync. When this is given, this "
            "process only keeps an SSH tunnel open to the dashboards on that server."
        ),
    )
    parser.add_argument(
        "--remote-backend-port",
        type=int,
        default=DEFAULT_SERVER_BACKEND_PORT,
        help="Port the backend and full dashboard bind on the server.",
    )
    parser.add_argument(
        "--remote-simple-port",
        type=int,
        default=DEFAULT_SIMPLE_WEB_PORT,
        help="Port the simple dashboard binds on the server.",
    )
    return parser


def tunnel_command(target: str, forwards: list[tuple[int, int]]) -> list[str]:
    command = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        # A laptop sleeps and changes networks. These settings make a dead connection end
        # within about 45 seconds so the supervisor can open a new one.
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "BatchMode=yes",
    ]
    for local_port, remote_port in forwards:
        # The explicit bind address keeps each port loopback-only even if ssh_config sets
        # GatewayPorts.
        command += ["-L", f"{DEFAULT_WEB_HOST}:{local_port}:{DEFAULT_WEB_HOST}:{remote_port}"]
    command.append(target)
    return command


class _TunnelSupervisor:
    """Keeps ``ssh -N -L`` running, and restarts it with backoff whenever it exits."""

    MIN_RETRY_SECONDS = 5
    MAX_RETRY_SECONDS = 60
    # A tunnel that stayed up this long was working, so the next failure starts the backoff
    # over instead of waiting the longest delay.
    HEALTHY_SECONDS = 60

    def __init__(self, command: list[str], *, ssh_log: IO[str] | None = None) -> None:
        self.command = command
        self._ssh_log = ssh_log
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None

    def run(self) -> None:
        delay = self.MIN_RETRY_SECONDS
        while not self._stopping.is_set():
            started = time.monotonic()
            print(f"[{_now()}] Opening SSH tunnel.")
            try:
                self._process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    # ssh's own errors (refused key, unreachable host) are what explain a
                    # failing tunnel, so they go to the log.
                    stderr=self._ssh_log if self._ssh_log is not None else subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                print(f"SSH tunnel could not start: {error}")
            else:
                status = self._process.wait()
                if self._stopping.is_set():
                    return
                print(f"[{_now()}] SSH tunnel exited with status {status}.")
            if time.monotonic() - started >= self.HEALTHY_SECONDS:
                delay = self.MIN_RETRY_SECONDS
            print(f"Reconnecting in {delay} seconds.")
            self._stopping.wait(delay)
            delay = min(delay * 2, self.MAX_RETRY_SECONDS)

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()


def _end_children_with_this_process() -> None:
    """Put this process in a job object that ends its children when it exits.

    Stopping the scheduled task or signing out kills this process, but Windows does not end
    its children with it. An orphaned ssh.exe would keep the dashboard ports bound, and
    every tunnel after it would then fail to start. Children join the job when they are
    created, so this must run before ssh starts. Best effort: without the job, the
    installer still removes stale tunnels.
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_kill_on_job_close = 0x2000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    # The handle is deliberately never closed: the job, and its kill-on-close promise,
    # must last exactly as long as this process does.
    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    if not (
        job
        and kernel32.SetInformationJobObject(
            job,
            job_object_extended_limit_information,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    ):
        print(
            "Could not tie the SSH tunnel's lifetime to this process "
            f"(Windows error {ctypes.get_last_error()})."
        )


def _now() -> str:
    return datetime.now().astimezone().isoformat()


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


def _rotate_large_log(log_path: Path) -> None:
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))


def _run_local(args: argparse.Namespace) -> int:
    # Imported here so the tunnel-only mode never loads the application.
    from canvas_task_sync import cli

    return cli.main(
        [
            "--config",
            str(args.config),
            "web",
            "--port",
            str(args.port),
            "--simple-port",
            str(args.simple_port),
            "--no-open",
        ]
    )


def _run_tunnel(args: argparse.Namespace, log_file: IO[str]) -> int:
    print(
        f"Dashboards are hosted on {args.ssh_target}. Forwarding "
        f"{DEFAULT_WEB_HOST}:{args.port} to its port {args.remote_backend_port} and "
        f"{DEFAULT_WEB_HOST}:{args.simple_port} to its port {args.remote_simple_port}."
    )
    _end_children_with_this_process()
    supervisor = _TunnelSupervisor(
        tunnel_command(
            args.ssh_target,
            [
                (args.port, args.remote_backend_port),
                (args.simple_port, args.remote_simple_port),
            ],
        ),
        ssh_log=log_file,
    )
    try:
        supervisor.run()
    finally:
        supervisor.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    archived_log_path = _archive_legacy_log(args.log_path)
    _rotate_large_log(args.log_path)

    with (
        args.log_path.open("a", encoding="utf-8", errors="replace", buffering=1) as log_file,
        redirect_stdout(log_file),
        redirect_stderr(log_file),
    ):
        if archived_log_path is not None:
            print(f"Archived legacy startup log: {archived_log_path}")
        print(f"[{_now()}] Starting Canvas Task Sync Windows background server.")
        try:
            if args.ssh_target:
                return _run_tunnel(args, log_file)
            return _run_local(args)
        except BaseException:
            traceback.print_exc()
            return 1
        finally:
            print(f"[{_now()}] Canvas Task Sync Windows background server stopped.")


if __name__ == "__main__":
    raise SystemExit(main())
