from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials


def __getattr__(name: str) -> Any:
    """Resolve the refresh transport on first use.

    ``google.auth.transport.requests`` pulls in ``requests``, about 5 MB that a web
    process which never refreshes a token has no reason to carry. Exposing it as a module
    attribute keeps ``auth.Request`` importable and patchable exactly as before.
    """
    if name == "Request":
        from google.auth.transport.requests import Request

        globals()["Request"] = Request
        return Request
    if name == "Credentials":
        from google.oauth2.credentials import Credentials

        globals()["Credentials"] = Credentials
        return Credentials
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
SLIDES_READONLY_SCOPE = "https://www.googleapis.com/auth/presentations.readonly"
SCOPES = [TASKS_SCOPE, SLIDES_READONLY_SCOPE]

# A sync-all operation prepares courses concurrently.  All of those workers share the
# same OAuth token file, so credential refresh and persistence must be one critical
# section.  The unique temporary file in _write_token also prevents a second local
# process (for example, a CLI invocation beside the web app) from colliding with it.
_CREDENTIALS_LOCK = threading.RLock()


class AuthenticationError(RuntimeError):
    pass


def _write_token(path: Path, credentials: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _request() -> Any:
    from canvas_task_sync import auth

    return auth.Request()


def _credentials_class() -> Any:
    # Through the module so a test that patches ``auth.Credentials`` still wins.
    from canvas_task_sync import auth

    return auth.Credentials


def _token_scopes(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return set(data.get("scopes") or [])


def persist_authorized_credentials(root_dir: Path, credentials: Credentials) -> None:
    """Store freshly authorized credentials under the same lock a refresh would take."""
    with _CREDENTIALS_LOCK:
        _write_token(root_dir / "token.json", credentials)


def load_google_credentials(
    root_dir: Path,
    *,
    interactive: bool = False,
) -> Credentials:
    with _CREDENTIALS_LOCK:
        return _load_google_credentials_locked(root_dir, interactive=interactive)


def _load_google_credentials_locked(
    root_dir: Path,
    *,
    interactive: bool,
) -> Credentials:
    token_path = root_dir / "token.json"
    client_path = root_dir / "credentials.json"
    credentials: Credentials | None = None

    token_has_scopes = token_path.exists() and set(SCOPES).issubset(_token_scopes(token_path))
    if token_has_scopes:
        credentials = _credentials_class().from_authorized_user_file(token_path, SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(_request())
        except Exception as error:  # Google auth surfaces several transport-specific errors.
            if not interactive:
                raise AuthenticationError(
                    "Google OAuth refresh failed. Run 'canvas-task-sync auth'."
                ) from error
        else:
            _write_token(token_path, credentials)
            return credentials

    if not interactive:
        if token_path.exists() and not token_has_scopes:
            raise AuthenticationError(
                "token.json does not include Google Slides read access. "
                "Run 'canvas-task-sync auth' once to grant the new scope."
            )
        raise AuthenticationError("Google OAuth is not configured. Run 'canvas-task-sync auth'.")

    if not client_path.exists():
        raise AuthenticationError(f"OAuth client file not found: {client_path}")

    # Imported here rather than at module scope: google_auth_oauthlib costs about
    # 11 MB of resident memory and only this branch, which a long-running backend
    # reaches rarely or never, needs it.
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    credentials = flow.run_local_server(
        port=0,
        authorization_prompt_message="Open this URL to authorize Canvas Task Sync:\n{url}",
        success_message="Authorization complete. You may close this window.",
        open_browser=True,
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    _write_token(token_path, credentials)
    return credentials
