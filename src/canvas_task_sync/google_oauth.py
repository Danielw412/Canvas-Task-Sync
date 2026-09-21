"""Browser-delegated Google OAuth for a backend with no graphical browser.

``InstalledAppFlow.run_local_server`` assumes one machine both opens the consent page
and binds the loopback redirect port.  That stops being true once the backend runs on a
headless server reached over an SSH tunnel: the person sits at the laptop, so the
consent page and the redirect both belong there.

This module splits the flow at that seam.  The backend mints the authorization URL and
keeps the OAuth state (including the PKCE verifier) in memory; the person completes
consent in their own browser; Google redirects the browser to the dashboard origin,
whose proxy hands the code back to the backend.  Only the backend ever sees the code,
the client secret, or the resulting refresh token.
"""

from __future__ import annotations

import hmac
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from canvas_task_sync.auth import SCOPES, persist_authorized_credentials
from canvas_task_sync.web_constants import LOOPBACK_HOSTNAMES

CALLBACK_PATH = "/api/v1/settings/google/callback"
AUTHORIZATION_TTL_SECONDS = 600
# One entry per consent tab the person has open.  The cap keeps an abandoned consent
# screen from growing this dictionary without bound.
MAX_PENDING_AUTHORIZATIONS = 4

FlowFactory = Callable[[Path, str], Any]

# ``fetch_token`` runs oauthlib's scope comparison, which aborts on any difference
# between the requested and the issued scope list.  Google legitimately returns extra
# scopes when ``include_granted_scopes`` is set, so the check is relaxed for the
# duration of the call and replaced by the explicit subset test in
# ``_credentials_from_flow``.  os.environ is process-global, so the swap is serialized.
_SCOPE_RELAXATION_LOCK = threading.Lock()


class GoogleAuthorizationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthorizationStart:
    state: str
    authorization_url: str
    redirect_uri: str
    expires_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "authorization_url": self.authorization_url,
            "redirect_uri": self.redirect_uri,
            "expires_at": self.expires_at.isoformat(),
        }


@dataclass
class _Pending:
    flow: Any
    redirect_uri: str
    created_at: datetime
    status: str = "pending"
    message: str | None = None
    error_code: str | None = None


def build_redirect_uri(public_origin: str) -> str:
    return f"{public_origin.rstrip('/')}{CALLBACK_PATH}"


def _default_flow_factory(client_path: Path, redirect_uri: str) -> Any:
    # Deferred so the ~11 MB google_auth_oauthlib import only lands when someone
    # actually authorizes, not in every idle backend.
    from google_auth_oauthlib.flow import Flow

    # ``Flow`` defaults to autogenerate_code_verifier=True, so the exchange stays
    # PKCE-protected exactly as run_local_server's was.
    return Flow.from_client_secrets_file(
        str(client_path),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def _validate_redirect_uri(redirect_uri: str) -> None:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTNAMES:
        raise GoogleAuthorizationError(
            "google_redirect_invalid",
            "The Google redirect must return to a loopback dashboard address.",
        )
    if parsed.path != CALLBACK_PATH:
        raise GoogleAuthorizationError(
            "google_redirect_invalid",
            "The Google redirect must target the dashboard callback path.",
        )


def _fetch_token(flow: Any, code: str) -> None:
    with _SCOPE_RELAXATION_LOCK:
        previous = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        try:
            flow.fetch_token(code=code)
        finally:
            if previous is None:
                os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
            else:
                os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous


def _credentials_from_flow(flow: Any) -> Any:
    credentials = flow.credentials
    granted = set(getattr(credentials, "scopes", None) or [])
    if not set(SCOPES).issubset(granted):
        raise GoogleAuthorizationError(
            "google_scopes_declined",
            "Google Tasks and Slides access were not both granted. Authorize again and "
            "leave both permissions checked.",
        )
    if not getattr(credentials, "refresh_token", None):
        # Overwriting a working token.json with one that cannot refresh would silently
        # break every later sync, so this stops before the write.
        raise GoogleAuthorizationError(
            "google_refresh_token_missing",
            "Google did not return a refresh token. Remove Canvas Task Sync from the "
            "third-party access list on your Google account and authorize again.",
        )
    return credentials


class GoogleAuthorizationManager:
    """Tracks consent rounds in memory, one entry per authorization the person starts."""

    def __init__(
        self,
        root_dir: Path,
        *,
        ttl_seconds: int = AUTHORIZATION_TTL_SECONDS,
        flow_factory: FlowFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root_dir = root_dir
        self.ttl_seconds = ttl_seconds
        self._flow_factory = flow_factory or _default_flow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    @property
    def client_path(self) -> Path:
        return self.root_dir / "credentials.json"

    def begin(self, *, redirect_uri: str) -> AuthorizationStart:
        _validate_redirect_uri(redirect_uri)
        if not self.client_path.exists():
            raise GoogleAuthorizationError(
                "google_client_missing",
                "Upload the Google OAuth desktop client file before authorizing.",
            )
        flow = self._flow_factory(self.client_path, redirect_uri)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        created_at = self._now()
        with self._lock:
            self._prune_locked()
            if len(self._pending) >= MAX_PENDING_AUTHORIZATIONS:
                oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
                del self._pending[oldest]
            self._pending[state] = _Pending(
                flow=flow,
                redirect_uri=redirect_uri,
                created_at=created_at,
            )
        return AuthorizationStart(
            state=state,
            authorization_url=authorization_url,
            redirect_uri=redirect_uri,
            expires_at=created_at + timedelta(seconds=self.ttl_seconds),
        )

    def complete(self, *, state: str, code: str) -> None:
        """Exchange ``code`` for credentials and store them. Raises on any mismatch."""
        if not code:
            raise GoogleAuthorizationError(
                "google_code_missing",
                "Google did not return an authorization code.",
            )
        entry, matched = self._take(state)
        try:
            _fetch_token(entry.flow, code)
            credentials = _credentials_from_flow(entry.flow)
        except GoogleAuthorizationError as error:
            self._record(matched, entry, status="failed", code=error.code, message=str(error))
            raise
        except Exception as error:
            message = "The Google authorization code could not be exchanged. Try again."
            self._record(
                matched,
                entry,
                status="failed",
                code="google_token_exchange_failed",
                message=message,
            )
            raise GoogleAuthorizationError("google_token_exchange_failed", message) from error
        persist_authorized_credentials(self.root_dir, credentials)
        self._record(
            matched,
            entry,
            status="completed",
            code=None,
            message="Google authorization is complete.",
        )

    def fail(self, *, state: str, code: str, message: str) -> None:
        """Record a consent screen the person cancelled or that Google rejected."""
        try:
            entry, matched = self._take(state)
        except GoogleAuthorizationError:
            return
        self._record(matched, entry, status="failed", code=code, message=message)

    def status(self, state: str) -> dict[str, Any]:
        with self._lock:
            self._prune_locked()
            entry = self._match_locked(state)
            if entry is None:
                return {
                    "status": "unknown",
                    "code": "google_authorization_unknown",
                    "message": (
                        "This authorization attempt expired or was never started. "
                        "Start authorization again."
                    ),
                }
            return {"status": entry.status, "code": entry.error_code, "message": entry.message}

    def forget(self, state: str) -> None:
        with self._lock:
            matched = self._match_key_locked(state)
            if matched is not None:
                del self._pending[matched]

    def _take(self, state: str) -> tuple[_Pending, str]:
        with self._lock:
            self._prune_locked()
            matched = self._match_key_locked(state)
            if matched is None:
                raise GoogleAuthorizationError(
                    "google_state_invalid",
                    "This authorization link expired or does not match a pending request. "
                    "Start authorization again from the dashboard.",
                )
            entry = self._pending[matched]
            if entry.status != "pending":
                raise GoogleAuthorizationError(
                    "google_state_used",
                    "This authorization link was already used. Start authorization again.",
                )
            return entry, matched

    def _record(
        self,
        state: str,
        entry: _Pending,
        *,
        status: str,
        code: str | None,
        message: str | None,
    ) -> None:
        entry.status = status
        entry.error_code = code
        entry.message = message
        with self._lock:
            self._pending[state] = entry

    def _match_key_locked(self, state: str) -> str | None:
        # The state is high-entropy, but a constant-time comparison keeps the lookup from
        # leaking a prefix to a caller that can time the loopback endpoint.
        if not state:
            return None
        for key in self._pending:
            if hmac.compare_digest(key, state):
                return key
        return None

    def _match_locked(self, state: str) -> _Pending | None:
        matched = self._match_key_locked(state)
        return self._pending[matched] if matched is not None else None

    def _prune_locked(self) -> None:
        cutoff = self._now() - timedelta(seconds=self.ttl_seconds)
        for key in [key for key, item in self._pending.items() if item.created_at < cutoff]:
            del self._pending[key]
