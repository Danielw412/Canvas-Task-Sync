"""Where the Claude and Codex agents live on this machine, and whether they are signed in.

Standard library only: the web process imports this for its status lines, and it must not
load either SDK to do so. Both SDKs ship their own CLI binary; every check here runs that
same binary, so a status line describes exactly what a run would use.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

# Everything that looks like a credential is kept from the agents: Canvas, Google, and
# Gemini secrets, and the pay-per-token API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY,
# CODEX_API_KEY), so each agent falls back to its subscription sign-in and draws from
# plan usage.
_SECRET_ENV = re.compile(r"CANVAS|GOOGLE|GEMINI|TOKEN|SECRET|PASSWORD|COOKIE|API_KEY", re.I)
# Claude Code's long-lived subscription token is a sign-in, not an API key.
_SUBSCRIPTION_SIGN_IN_ENV = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

SIGN_IN_HINTS = {
    "claude": "Sign in to Claude Code on this machine: run `claude`, then /login.",
    "codex": "Sign in to Codex on this machine: run `codex login`.",
}
SDK_PACKAGES = {"claude": "claude_agent_sdk", "codex": "openai_codex"}
INSTALL_HINT = "Reinstall the backend with `pip install -e .` to add the {label} SDK."


DEFAULT_AGENT_CONCURRENCY = 3


def agent_concurrency() -> int:
    """Agent turns one process runs at once; ``CANVAS_TASK_SYNC_AGENT_CONCURRENCY`` overrides.

    Each turn is a Claude Code or Codex process of a few hundred megabytes, so this, not the
    run queue's width, is what bounds the memory parallel runs take.
    """
    try:
        value = int(os.getenv("CANVAS_TASK_SYNC_AGENT_CONCURRENCY") or DEFAULT_AGENT_CONCURRENCY)
    except ValueError:
        value = DEFAULT_AGENT_CONCURRENCY
    return max(1, min(8, value))


def _is_secret(key: str) -> bool:
    return bool(_SECRET_ENV.search(key)) and key not in _SUBSCRIPTION_SIGN_IN_ENV


def agent_environment_overrides() -> dict[str, str]:
    """Blank every secret in the environment, for SDKs that merge their env onto ours."""
    return {key: "" for key in os.environ if _is_secret(key)}


def agent_environment() -> dict[str, str]:
    """This process's environment without its secrets, for a subprocess we start."""
    return {key: value for key, value in os.environ.items() if not _is_secret(key)}


def _package_dir(module: str) -> Path | None:
    try:
        spec = find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def sdk_installed(provider: str) -> bool:
    package = SDK_PACKAGES.get(provider)
    return package is not None and _package_dir(package) is not None


def agent_cli_path(provider: str) -> Path | None:
    """The CLI binary the provider's SDK runs, without importing the SDK."""
    windows = os.name == "nt"
    if provider == "claude":
        package = _package_dir("claude_agent_sdk")
        candidate = package and package / "_bundled" / ("claude.exe" if windows else "claude")
    elif provider == "codex":
        package = _package_dir("codex_cli_bin")
        candidate = package and package / "bin" / ("codex.exe" if windows else "codex")
    else:
        return None
    return candidate if candidate and candidate.is_file() else None


def _credentials_present(provider: str) -> bool:
    if provider == "claude":
        if os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
            return True
        home = Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return (home / ".credentials.json").is_file()
    if provider == "codex":
        home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
        return (home / "auth.json").is_file()
    return False


@dataclass(frozen=True)
class AgentSignIn:
    # None when the check itself could not run, which is not the same as signed out.
    ready: bool | None
    detail: str


def quick_agent_status(provider: str) -> AgentSignIn:
    """A file-system check cheap enough for every dashboard request."""
    label = provider.title()
    if not sdk_installed(provider):
        return AgentSignIn(False, INSTALL_HINT.format(label=label))
    if not _credentials_present(provider):
        return AgentSignIn(False, SIGN_IN_HINTS[provider])
    return AgentSignIn(True, f"{label} sign-in found on this machine")


def check_agent_sign_in(provider: str, *, timeout: float = 30.0) -> AgentSignIn:
    """Ask the provider's own CLI whether it is signed in. Starts no turn and uses no usage."""
    label = provider.title()
    binary = agent_cli_path(provider)
    if binary is None:
        return AgentSignIn(False, INSTALL_HINT.format(label=label))
    command = [str(binary), "auth", "status"] if provider == "claude" else [
        str(binary),
        "login",
        "status",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=agent_environment(),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return AgentSignIn(None, f"{label} could not be asked about its sign-in: {error}")
    if provider == "claude":
        return _claude_sign_in(completed)
    return _codex_sign_in(completed)


def _claude_sign_in(completed: subprocess.CompletedProcess[str]) -> AgentSignIn:
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return AgentSignIn(None, _last_line(completed) or "Claude Code gave no sign-in status.")
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return AgentSignIn(False, f"Not signed in. {SIGN_IN_HINTS['claude']}")
    method = str(status.get("authMethod") or "")
    plan = str(status.get("subscriptionType") or "").strip()
    if method in {"api_key", "apiKey"} or "key" in method.casefold():
        return AgentSignIn(
            False,
            "Claude Code is signed in with an API key, which bills the API instead of plan "
            f"usage. {SIGN_IN_HINTS['claude']}",
        )
    return AgentSignIn(True, f"Signed in · Claude {plan.title()}" if plan else "Signed in")


def _codex_sign_in(completed: subprocess.CompletedProcess[str]) -> AgentSignIn:
    # `codex login status` prints any warnings first and its verdict last.
    verdict = _last_line(completed)
    if completed.returncode != 0:
        return AgentSignIn(False, f"Not signed in. {SIGN_IN_HINTS['codex']}")
    if "api key" in verdict.casefold():
        return AgentSignIn(
            False,
            "Codex is signed in with an API key, which bills the API instead of ChatGPT plan "
            f"usage. {SIGN_IN_HINTS['codex']}",
        )
    return AgentSignIn(True, verdict or "Signed in")


def _last_line(completed: subprocess.CompletedProcess[str]) -> str:
    lines = [
        line.strip()
        for line in f"{completed.stdout}\n{completed.stderr}".splitlines()
        if line.strip()
    ]
    return lines[-1] if lines else ""
