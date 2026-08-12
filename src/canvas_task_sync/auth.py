from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
SLIDES_READONLY_SCOPE = "https://www.googleapis.com/auth/presentations.readonly"
SCOPES = [TASKS_SCOPE, SLIDES_READONLY_SCOPE]


class AuthenticationError(RuntimeError):
    pass


def _write_token(path: Path, credentials: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    temporary.replace(path)


def _token_scopes(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return set(data.get("scopes") or [])


def load_google_credentials(
    root_dir: Path,
    *,
    interactive: bool = False,
) -> Credentials:
    token_path = root_dir / "token.json"
    client_path = root_dir / "credentials.json"
    credentials: Credentials | None = None

    token_has_scopes = token_path.exists() and set(SCOPES).issubset(_token_scopes(token_path))
    if token_has_scopes:
        credentials = Credentials.from_authorized_user_file(token_path, SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
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

