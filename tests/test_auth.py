import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import canvas_task_sync.auth as auth
from canvas_task_sync.auth import SCOPES, SLIDES_READONLY_SCOPE, TASKS_SCOPE


def test_oauth_uses_only_tasks_and_target_slides_read_scope():
    assert SCOPES == [TASKS_SCOPE, SLIDES_READONLY_SCOPE]
    assert all("drive" not in scope for scope in SCOPES)


def test_parallel_credential_loads_refresh_and_write_shared_token_once(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    token_path.write_text(
        json.dumps({"scopes": SCOPES, "refreshed": False}),
        encoding="utf-8",
    )
    refresh_count = 0
    refresh_count_lock = threading.Lock()
    start = threading.Barrier(6)

    class FakeCredentials:
        def __init__(self, *, valid: bool) -> None:
            self.valid = valid
            self.expired = not valid
            self.refresh_token = "refresh-token"

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            assert scopes == SCOPES
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(valid=bool(payload.get("refreshed")))

        def refresh(self, _request) -> None:
            nonlocal refresh_count
            with refresh_count_lock:
                refresh_count += 1
            time.sleep(0.05)
            self.valid = True
            self.expired = False

        def to_json(self) -> str:
            return json.dumps({"scopes": SCOPES, "refreshed": True})

    monkeypatch.setattr(auth, "Credentials", FakeCredentials)
    monkeypatch.setattr(auth, "Request", object)

    def load():
        start.wait()
        return auth.load_google_credentials(tmp_path, interactive=False)

    with ThreadPoolExecutor(max_workers=6) as executor:
        credentials = list(executor.map(lambda _index: load(), range(6)))

    assert all(item.valid for item in credentials)
    assert refresh_count == 1
    assert json.loads(token_path.read_text(encoding="utf-8"))["refreshed"] is True
    assert list(tmp_path.glob(".token.json.*.tmp")) == []
