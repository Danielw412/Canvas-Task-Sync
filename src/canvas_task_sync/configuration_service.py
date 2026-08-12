from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from ruamel.yaml import YAML

from canvas_task_sync.configuration import ProjectSettings, load_settings
from canvas_task_sync.web_models import CourseSave

MAX_CREDENTIAL_FILE_BYTES = 128 * 1024


def _atomic_replace(path: Path, payload: bytes, *, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(f"{path.suffix}.bak"))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ConfigurationService:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.root_dir = self.config_path.parent.parent
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.default_flow_style = False

    def load(self) -> ProjectSettings:
        return load_settings(self.config_path)

    def save_course(self, course: CourseSave, *, creating: bool) -> ProjectSettings:
        document = self._document()
        courses = document.setdefault("courses", {})
        exists = course.id in courses
        if creating and exists:
            raise ValueError(f"Course '{course.id}' already exists.")
        if not creating and not exists:
            raise ValueError(f"Course '{course.id}' does not exist.")
        payload = course.settings.model_dump(mode="json")
        if exists:
            _merge_mapping(courses[course.id], payload)
        else:
            courses[course.id] = payload
        self._write_and_validate(document)
        return self.load()

    def set_course_enabled(self, course_id: str, enabled: bool) -> ProjectSettings:
        document = self._document()
        courses = document.setdefault("courses", {})
        if course_id not in courses:
            raise ValueError(f"Course '{course_id}' does not exist.")
        courses[course_id]["enabled"] = enabled
        self._write_and_validate(document)
        return self.load()

    def sanitized_document(self) -> dict[str, Any]:
        document = self._document()
        return json.loads(json.dumps(document))

    def save_gemini_key(self, api_key: str) -> None:
        normalized = api_key.strip()
        if not normalized:
            raise ValueError("Gemini API key cannot be blank.")
        path = self.root_dir / ".env"
        values = {key: value for key, value in dotenv_values(path).items() if value is not None}
        values["GEMINI_API_KEY"] = normalized
        payload = "".join(f"{key}={_quote_env(value)}\n" for key, value in values.items())
        _atomic_replace(path, payload.encode("utf-8"), backup=True)

    def save_oauth_client(self, payload: bytes) -> None:
        if len(payload) > MAX_CREDENTIAL_FILE_BYTES:
            raise ValueError("OAuth client file exceeds the 128 KiB limit.")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("OAuth client file must be valid UTF-8 JSON.") from error
        installed = document.get("installed") if isinstance(document, dict) else None
        if not isinstance(installed, dict):
            raise ValueError("OAuth client JSON must contain an 'installed' desktop client.")
        required = {"client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"}
        missing = sorted(key for key in required if not installed.get(key))
        if missing:
            raise ValueError(f"OAuth client JSON is missing: {', '.join(missing)}.")
        redirects = installed.get("redirect_uris")
        if not isinstance(redirects, list) or not any(
            str(uri).startswith("http://localhost") for uri in redirects
        ):
            raise ValueError("OAuth desktop client must allow a localhost redirect URI.")
        formatted = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        _atomic_replace(self.root_dir / "credentials.json", formatted, backup=True)

    def disconnect_google(self) -> bool:
        path = self.root_dir / "token.json"
        if not path.exists():
            return False
        backup = path.with_suffix(".json.disconnected")
        if backup.exists():
            backup.unlink()
        path.replace(backup)
        return True

    def _document(self) -> Any:
        if not self.config_path.exists():
            return {"version": 1, "courses": {}}
        with self.config_path.open("r", encoding="utf-8") as file:
            return self.yaml.load(file) or {"version": 1, "courses": {}}

    def _write_and_validate(self, document: Any) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".yaml",
            dir=self.config_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                self.yaml.dump(document, file)
                file.flush()
                os.fsync(file.fileno())
            load_settings(temporary)
            if self.config_path.exists():
                shutil.copy2(
                    self.config_path,
                    self.config_path.with_suffix(f"{self.config_path.suffix}.bak"),
                )
            temporary.replace(self.config_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _quote_env(value: str) -> str:
    if not value or any(character.isspace() or character in {'#', '"', "'"} for character in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _merge_mapping(target: Any, source: dict[str, Any]) -> None:
    """Update a round-trip YAML mapping without discarding its comments or ordering."""
    for key, value in source.items():
        existing = target.get(key) if hasattr(target, "get") else None
        if isinstance(value, dict) and hasattr(existing, "items"):
            _merge_mapping(existing, value)
        else:
            target[key] = value
