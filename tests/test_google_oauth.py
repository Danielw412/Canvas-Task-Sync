"""Browser-delegated Google authorization, driven without contacting Google."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canvas_task_sync.auth import SCOPES
from canvas_task_sync.google_oauth import (
    CALLBACK_PATH,
    MAX_PENDING_AUTHORIZATIONS,
    GoogleAuthorizationError,
    GoogleAuthorizationManager,
    build_redirect_uri,
)
from canvas_task_sync.web_app import create_web_app

REDIRECT_URI = f"http://127.0.0.1:8890{CALLBACK_PATH}"


class FakeCredentials:
    def __init__(self, *, scopes=None, refresh_token="refresh-token") -> None:
        self.scopes = SCOPES if scopes is None else scopes
        self.refresh_token = refresh_token
        self.valid = True
        self.expired = False

    def to_json(self) -> str:
        return json.dumps({"scopes": list(self.scopes), "refresh_token": self.refresh_token})


class FakeFlow:
    """Stands in for google_auth_oauthlib's Flow with no network involved."""

    instances: list[FakeFlow] = []

    def __init__(self, client_path: Path, redirect_uri: str, **outcome) -> None:
        self.client_path = client_path
        self.redirect_uri = redirect_uri
        self.exchanged_code: str | None = None
        self._outcome = outcome
        self._state = f"state-{len(FakeFlow.instances)}"
        FakeFlow.instances.append(self)

    def authorization_url(self, **kwargs):
        self.authorization_kwargs = kwargs
        return f"https://accounts.google.com/o/oauth2/auth?state={self._state}", self._state

    def fetch_token(self, *, code: str) -> None:
        if self._outcome.get("raises"):
            raise RuntimeError("invalid_grant")
        self.exchanged_code = code

    @property
    def credentials(self):
        return FakeCredentials(
            scopes=self._outcome.get("scopes"),
            refresh_token=self._outcome.get("refresh_token", "refresh-token"),
        )


@pytest.fixture(autouse=True)
def _reset_flows():
    FakeFlow.instances.clear()
    yield
    FakeFlow.instances.clear()


def _manager(root: Path, *, now=None, **outcome) -> GoogleAuthorizationManager:
    (root / "credentials.json").write_text(
        json.dumps({"installed": {"client_id": "fixture"}}), encoding="utf-8"
    )
    return GoogleAuthorizationManager(
        root,
        flow_factory=lambda client_path, redirect_uri: FakeFlow(
            client_path, redirect_uri, **outcome
        ),
        now=now,
    )


def test_authorization_url_requests_offline_consent_for_the_existing_scopes(tmp_path):
    started = _manager(tmp_path).begin(redirect_uri=REDIRECT_URI)

    flow = FakeFlow.instances[0]
    assert flow.redirect_uri == REDIRECT_URI
    assert flow.authorization_kwargs == {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    assert started.authorization_url.startswith("https://accounts.google.com/")
    assert started.state


def test_completing_the_flow_writes_the_token_on_this_machine_only(tmp_path):
    manager = _manager(tmp_path)
    started = manager.begin(redirect_uri=REDIRECT_URI)

    manager.complete(state=started.state, code="auth-code")

    token = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert token["refresh_token"] == "refresh-token"
    assert set(token["scopes"]) == set(SCOPES)
    assert FakeFlow.instances[0].exchanged_code == "auth-code"
    assert manager.status(started.state)["status"] == "completed"
    # Nothing is left lying around for another machine to pick up.
    assert list(tmp_path.glob(".token.json.*.tmp")) == []


def test_a_mismatched_state_is_refused(tmp_path):
    manager = _manager(tmp_path)
    manager.begin(redirect_uri=REDIRECT_URI)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state="not-the-state", code="auth-code")

    assert error.value.code == "google_state_invalid"
    assert not (tmp_path / "token.json").exists()


def test_a_state_cannot_be_replayed(tmp_path):
    manager = _manager(tmp_path)
    started = manager.begin(redirect_uri=REDIRECT_URI)
    manager.complete(state=started.state, code="auth-code")

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state=started.state, code="auth-code")

    assert error.value.code == "google_state_used"


def test_an_expired_authorization_is_refused(tmp_path):
    clock = {"now": datetime(2026, 9, 19, 12, 0, tzinfo=UTC)}
    manager = _manager(tmp_path, now=lambda: clock["now"])
    started = manager.begin(redirect_uri=REDIRECT_URI)

    clock["now"] += timedelta(seconds=manager.ttl_seconds + 1)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state=started.state, code="auth-code")
    assert error.value.code == "google_state_invalid"
    assert manager.status(started.state)["status"] == "unknown"


def test_a_response_without_a_refresh_token_never_overwrites_a_working_token(tmp_path):
    (tmp_path / "token.json").write_text(
        json.dumps({"scopes": SCOPES, "refresh_token": "existing"}), encoding="utf-8"
    )
    manager = _manager(tmp_path, refresh_token=None)
    started = manager.begin(redirect_uri=REDIRECT_URI)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state=started.state, code="auth-code")

    assert error.value.code == "google_refresh_token_missing"
    kept = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert kept["refresh_token"] == "existing"
    assert manager.status(started.state)["status"] == "failed"


def test_declining_a_scope_is_refused_rather_than_stored(tmp_path):
    manager = _manager(tmp_path, scopes=[SCOPES[0]])
    started = manager.begin(redirect_uri=REDIRECT_URI)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state=started.state, code="auth-code")

    assert error.value.code == "google_scopes_declined"
    assert not (tmp_path / "token.json").exists()


def test_extra_previously_granted_scopes_are_accepted(tmp_path):
    manager = _manager(tmp_path, scopes=[*SCOPES, "https://www.googleapis.com/auth/userinfo.email"])
    started = manager.begin(redirect_uri=REDIRECT_URI)

    manager.complete(state=started.state, code="auth-code")

    assert (tmp_path / "token.json").exists()


def test_a_failed_exchange_is_reported_without_leaking_the_cause(tmp_path):
    manager = _manager(tmp_path, raises=True)
    started = manager.begin(redirect_uri=REDIRECT_URI)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.complete(state=started.state, code="auth-code")

    assert error.value.code == "google_token_exchange_failed"
    assert "invalid_grant" not in str(error.value)
    assert not (tmp_path / "token.json").exists()


def test_a_missing_oauth_client_is_reported_before_any_url_is_minted(tmp_path):
    manager = GoogleAuthorizationManager(tmp_path, flow_factory=FakeFlow)

    with pytest.raises(GoogleAuthorizationError) as error:
        manager.begin(redirect_uri=REDIRECT_URI)

    assert error.value.code == "google_client_missing"
    assert FakeFlow.instances == []


@pytest.mark.parametrize(
    "redirect_uri",
    [
        f"https://127.0.0.1:8890{CALLBACK_PATH}",
        f"http://example.com{CALLBACK_PATH}",
        "http://127.0.0.1:8890/somewhere-else",
    ],
)
def test_only_the_loopback_callback_may_be_used_as_a_redirect(tmp_path, redirect_uri):
    with pytest.raises(GoogleAuthorizationError) as error:
        _manager(tmp_path).begin(redirect_uri=redirect_uri)
    assert error.value.code == "google_redirect_invalid"


def test_abandoned_consent_tabs_do_not_accumulate(tmp_path):
    manager = _manager(tmp_path)
    states = [
        manager.begin(redirect_uri=REDIRECT_URI).state
        for _ in range(MAX_PENDING_AUTHORIZATIONS + 2)
    ]

    assert [manager.status(state)["status"] for state in states[:2]] == ["unknown", "unknown"]
    assert manager.status(states[-1])["status"] == "pending"


def test_cancelled_consent_is_recorded_for_the_dashboard(tmp_path):
    manager = _manager(tmp_path)
    started = manager.begin(redirect_uri=REDIRECT_URI)

    manager.fail(state=started.state, code="google_consent_declined", message="Cancelled.")

    assert manager.status(started.state) == {
        "status": "failed",
        "code": "google_consent_declined",
        "message": "Cancelled.",
    }


def test_redirect_uri_is_built_from_the_dashboard_origin():
    assert build_redirect_uri("http://127.0.0.1:8890") == REDIRECT_URI
    assert build_redirect_uri("http://127.0.0.1:8890/") == REDIRECT_URI


# --- the web routes that drive the flow from the dashboard --------------------------


def _project(root: Path) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses: {}
""",
        encoding="utf-8",
    )
    return config


def test_dashboard_can_authorize_google_end_to_end_without_a_server_browser(tmp_path):
    app = create_web_app(_project(tmp_path), port=8890, simple_port=8891)
    with TestClient(app) as client:
        app.state.runtime.google_auth = _manager(tmp_path)
        headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}

        started = client.post("/api/v1/settings/google/authorize", headers=headers)
        assert started.status_code == 200
        payload = started.json()
        assert payload["redirect_uri"] == REDIRECT_URI
        assert payload["authorization_url"].startswith("https://accounts.google.com/")

        pending = client.get(f"/api/v1/settings/google/authorize/{payload['state']}")
        assert pending.json()["status"] == "pending"
        assert pending.json()["connections"]["google_authorized"] is False

        # Google sends the browser here; it is a plain navigation with no CSRF token.
        callback = client.get(
            CALLBACK_PATH,
            params={"state": payload["state"], "code": "auth-code"},
        )
        assert callback.status_code == 200
        assert callback.headers["content-type"].startswith("text/html")
        assert "Authorization complete" in callback.text

        done = client.get(f"/api/v1/settings/google/authorize/{payload['state']}")
        assert done.json()["status"] == "completed"
        assert done.json()["connections"]["google_authorized"] is True
    assert (tmp_path / "token.json").exists()


def test_callback_with_a_forged_state_does_not_authorize(tmp_path):
    app = create_web_app(_project(tmp_path), port=8890, simple_port=8891)
    with TestClient(app) as client:
        app.state.runtime.google_auth = _manager(tmp_path)
        headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
        client.post("/api/v1/settings/google/authorize", headers=headers)

        callback = client.get(CALLBACK_PATH, params={"state": "forged", "code": "auth-code"})

    assert callback.status_code == 400
    assert "Authorization failed" in callback.text
    assert not (tmp_path / "token.json").exists()


def test_declined_consent_renders_a_cancellation_page(tmp_path):
    app = create_web_app(_project(tmp_path), port=8890, simple_port=8891)
    with TestClient(app) as client:
        app.state.runtime.google_auth = _manager(tmp_path)
        headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
        state = client.post("/api/v1/settings/google/authorize", headers=headers).json()["state"]

        callback = client.get(CALLBACK_PATH, params={"state": state, "error": "access_denied"})
        assert "Authorization cancelled" in callback.text

        status = client.get(f"/api/v1/settings/google/authorize/{state}").json()
    assert status["status"] == "failed"
    assert status["code"] == "google_consent_declined"


def test_authorize_requires_csrf_like_every_other_mutation(tmp_path):
    app = create_web_app(_project(tmp_path), port=8890, simple_port=8891)
    with TestClient(app) as client:
        response = client.post("/api/v1/settings/google/authorize")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_authorize_uses_the_configured_dashboard_origin_when_it_differs(tmp_path, monkeypatch):
    monkeypatch.setenv("CANVAS_TASK_SYNC_PUBLIC_ORIGIN", "http://127.0.0.1:9999")
    app = create_web_app(_project(tmp_path), port=8890, simple_port=8891)
    with TestClient(app) as client:
        app.state.runtime.google_auth = _manager(tmp_path)
        headers = {"X-CSRF-Token": client.get("/api/v1/bootstrap").json()["csrf_token"]}
        started = client.post("/api/v1/settings/google/authorize", headers=headers)
    assert started.json()["redirect_uri"] == f"http://127.0.0.1:9999{CALLBACK_PATH}"
