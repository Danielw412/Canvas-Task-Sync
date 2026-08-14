from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CAPTURE_TEXT_BYTES = 5 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_CAPTURE_BYTES = 40 * 1024 * 1024
MIN_SCREENSHOT_BYTES = 128
DEFAULT_CAPTURE_TTL_SECONDS = 15 * 60
MAX_CAPTURE_RECORDS = 8
DEFAULT_CAPTURE_REQUEST_TIMEOUT_SECONDS = 120
CAPTURE_REQUEST_TTL_SECONDS = 5 * 60
CAPTURE_REQUEST_LEASE_SECONDS = 2 * 60

RESOURCE_PATTERNS = {
    "google_slides": re.compile(r"/presentation/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"),
    "google_docs": re.compile(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"),
    "google_sheets": re.compile(r"/spreadsheets/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"),
}
SENSITIVE_METADATA_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "refresh_token",
    "access_token",
    "client_secret",
)


class CaptureMethod(StrEnum):
    SCREENSHOT = "screenshot"
    TEXT = "text"


class AcquisitionMode(StrEnum):
    SCREENSHOT = "screenshot"
    TEXT = "text"
    BOTH = "both"
    PREFER_SCREENSHOT = "prefer_screenshot"
    PREFER_TEXT = "prefer_text"


class CaptureRequestState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class BrowserCaptureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def source_type_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        raise ValueError("Only supported docs.google.com editor URLs may be captured.")
    for source_type, pattern in RESOURCE_PATTERNS.items():
        if pattern.search(parsed.path):
            return source_type
    raise ValueError("The URL is not a supported Google Slides, Docs, or Sheets editor URL.")


def resource_id_from_url(url: str) -> str:
    source_type = source_type_from_url(url)
    match = RESOURCE_PATTERNS[source_type].search(urlparse(url).path)
    if not match:  # Defensive: source_type_from_url already checked the same pattern.
        raise ValueError("The Google Workspace resource ID is missing from the URL.")
    return match.group(1)


def _validate_metadata(value: Any, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in SENSITIVE_METADATA_PARTS):
                raise ValueError(f"{path} must not contain authentication or credential fields")
            _validate_metadata(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_metadata(child, f"{path}[{index}]")


class BrowserCaptureItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=80)
    order: int = Field(default=0, ge=0)
    text: str = Field(default="", max_length=1_000_000)
    role: str = Field(default="unknown", max_length=40)
    row_index: int | None = Field(default=None, ge=0)
    column_index: int | None = Field(default=None, ge=0)
    row_label: str | None = Field(default=None, max_length=500)
    slide_id: str | None = Field(default=None, max_length=512)
    section_id: str | None = Field(default=None, max_length=512)
    sheet_id: str | None = Field(default=None, max_length=512)
    sheet_name: str | None = Field(default=None, max_length=500)
    range_a1: str | None = Field(default=None, max_length=500)
    structured_data: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def reject_sensitive_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value


class BrowserCaptureScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    item_id: str | None = Field(default=None, max_length=512)
    order: int = Field(default=0, ge=0)
    mime_type: str = "image/png"
    data_url: str = Field(max_length=28 * 1024 * 1024)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    width: int | None = Field(default=None, gt=0, le=32768)
    height: int | None = Field(default=None, gt=0, le=32768)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("Screenshots must be PNG, JPEG, or WebP images")
        return normalized

    @field_validator("metadata")
    @classmethod
    def reject_sensitive_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @model_validator(mode="after")
    def validate_image(self) -> BrowserCaptureScreenshot:
        image_bytes = self.decoded_bytes()
        digest = hashlib.sha256(image_bytes).hexdigest()
        if self.sha256 is not None and self.sha256 != digest:
            raise ValueError("Screenshot sha256 does not match its image data")
        self.sha256 = digest
        return self

    def decoded_bytes(self) -> bytes:
        prefix = f"data:{self.mime_type};base64,"
        if not self.data_url.startswith(prefix):
            raise ValueError("Screenshot data_url does not match its declared MIME type")
        try:
            image_bytes = base64.b64decode(
                self.data_url[len(prefix) :],
                validate=True,
            )
        except (binascii.Error, ValueError) as error:
            raise ValueError("Screenshot data_url is not valid base64") from error
        if len(image_bytes) < MIN_SCREENSHOT_BYTES:
            raise ValueError("Screenshot is empty or blank")
        if len(image_bytes) > MAX_SCREENSHOT_BYTES:
            raise ValueError("An individual screenshot exceeds the 20 MB limit")
        signatures = {
            "image/png": b"\x89PNG\r\n\x1a\n",
            "image/jpeg": b"\xff\xd8\xff",
            "image/webp": b"RIFF",
        }
        if not image_bytes.startswith(signatures[self.mime_type]):
            raise ValueError("Screenshot bytes do not match their declared MIME type")
        if self.mime_type == "image/webp" and image_bytes[8:12] != b"WEBP":
            raise ValueError("Screenshot bytes are not a valid WebP image")
        return image_bytes


class BrowserCaptureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    capture_id: str = Field(min_length=8, max_length=128)
    source_type: str
    source_url: str
    resource_id: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=1000)
    captured_at: datetime
    requested_mode: AcquisitionMode
    methods_used: list[CaptureMethod] = Field(min_length=1, max_length=2)
    fallback_used: bool = False
    items: list[BrowserCaptureItem] = Field(default_factory=list, max_length=20_000)
    screenshots: list[BrowserCaptureScreenshot] = Field(default_factory=list, max_length=50)
    selection: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        if value not in RESOURCE_PATTERNS:
            raise ValueError("source_type must be google_slides, google_docs, or google_sheets")
        return value

    @field_validator("selection", "metadata")
    @classmethod
    def reject_sensitive_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_metadata(value)
        return value

    @model_validator(mode="after")
    def validate_capture(self) -> BrowserCaptureEnvelope:
        detected_type = source_type_from_url(self.source_url)
        if detected_type != self.source_type:
            raise ValueError("source_type does not match source_url")
        if resource_id_from_url(self.source_url) != self.resource_id:
            raise ValueError("resource_id does not match source_url")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        if self.captured_at.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("captured_at cannot be in the future")

        methods = list(dict.fromkeys(self.methods_used))
        self.methods_used = methods
        has_text = any(
            item.text.strip() or item.structured_data is not None for item in self.items
        )
        has_screenshot = bool(self.screenshots)
        if CaptureMethod.TEXT in methods and not has_text:
            raise ValueError("methods_used includes text but the capture contains no text data")
        if CaptureMethod.SCREENSHOT in methods and not has_screenshot:
            raise ValueError(
                "methods_used includes screenshot but the capture contains no screenshots"
            )
        if CaptureMethod.TEXT not in methods and has_text:
            raise ValueError("Screenshot-only captures must not include extracted text")
        if CaptureMethod.SCREENSHOT not in methods and has_screenshot:
            raise ValueError("Text-only captures must not include screenshots")
        if self.requested_mode == AcquisitionMode.BOTH and set(methods) != {
            CaptureMethod.TEXT,
            CaptureMethod.SCREENSHOT,
        }:
            raise ValueError("Both mode requires screenshot and text content")
        if self.requested_mode == AcquisitionMode.TEXT and methods != [CaptureMethod.TEXT]:
            raise ValueError("Text-only mode may contain only text content")
        if self.requested_mode == AcquisitionMode.SCREENSHOT and methods != [
            CaptureMethod.SCREENSHOT
        ]:
            raise ValueError("Screenshot-only mode may contain only screenshots")
        if self.fallback_used and self.requested_mode not in {
            AcquisitionMode.PREFER_SCREENSHOT,
            AcquisitionMode.PREFER_TEXT,
        }:
            raise ValueError("fallback_used is valid only for preferred-method modes")
        if not self.items and not self.screenshots:
            raise ValueError("Capture contains no usable source content")

        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Capture item IDs must be unique")
        screenshot_ids = [screenshot.id for screenshot in self.screenshots]
        if len(screenshot_ids) != len(set(screenshot_ids)):
            raise ValueError("Screenshot IDs must be unique")

        text_bytes = sum(len(item.text.encode("utf-8")) for item in self.items)
        structured_bytes = len(
            json.dumps(
                [item.structured_data for item in self.items],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        if text_bytes + structured_bytes > MAX_CAPTURE_TEXT_BYTES:
            raise ValueError("Extracted source content exceeds the 5 MB limit")
        screenshot_bytes = sum(len(item.decoded_bytes()) for item in self.screenshots)
        if screenshot_bytes > MAX_CAPTURE_BYTES:
            raise ValueError("Combined screenshot data exceeds the 40 MB capture limit")
        return self


@dataclass(frozen=True)
class BrowserCaptureStatus:
    source_type: str
    resource_id: str
    received_at: datetime
    captured_at: datetime
    expires_at: datetime
    methods_used: tuple[str, ...]
    item_count: int
    screenshot_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "resource_id": self.resource_id,
            "received_at": self.received_at.isoformat(),
            "captured_at": self.captured_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "methods_used": list(self.methods_used),
            "item_count": self.item_count,
            "screenshot_count": self.screenshot_count,
        }


@dataclass
class _CaptureRecord:
    capture: BrowserCaptureEnvelope
    received_at: datetime


@dataclass
class _CaptureRequestRecord:
    request_id: str
    source_url: str
    source_type: str
    resource_id: str
    requested_mode: AcquisitionMode
    selection: dict[str, Any]
    created_at: datetime
    state: CaptureRequestState = CaptureRequestState.QUEUED
    leased_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "resource_id": self.resource_id,
            "requested_mode": self.requested_mode.value,
            "selection": deepcopy(self.selection),
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "leased_at": self.leased_at.isoformat() if self.leased_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class BrowserCaptureBroker:
    """Process-local, bounded capture handoff. Source content is never written to disk."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_CAPTURE_TTL_SECONDS,
        max_records: int = MAX_CAPTURE_RECORDS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_records = max_records
        self._records: dict[tuple[str, str], _CaptureRecord] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._requests: dict[str, _CaptureRequestRecord] = {}

    def put(self, capture: BrowserCaptureEnvelope) -> BrowserCaptureStatus:
        now = datetime.now(UTC)
        key = (capture.source_type, capture.resource_id)
        with self._lock:
            self._prune(now)
            self._records[key] = _CaptureRecord(
                capture=capture.model_copy(deep=True),
                received_at=now,
            )
            if len(self._records) > self.max_records:
                oldest = min(self._records, key=lambda item: self._records[item].received_at)
                del self._records[oldest]
            for request in self._requests.values():
                if (
                    request.source_type == capture.source_type
                    and request.resource_id == capture.resource_id
                    and request.state in {CaptureRequestState.QUEUED, CaptureRequestState.LEASED}
                ):
                    request.state = CaptureRequestState.COMPLETED
                    request.finished_at = now
            self._condition.notify_all()
            return self._status(self._records[key])

    def request_capture(
        self,
        source_url: str,
        requested_mode: AcquisitionMode | str,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        mode = AcquisitionMode(requested_mode)
        source_type = source_type_from_url(source_url)
        resource_id = resource_id_from_url(source_url)
        safe_selection = deepcopy(selection or {})
        _validate_metadata(safe_selection, "selection")
        with self._condition:
            self._prune_requests(now)
            for request in self._requests.values():
                if (
                    request.source_type == source_type
                    and request.resource_id == resource_id
                    and request.requested_mode == mode
                    and request.selection == safe_selection
                    and request.state in {CaptureRequestState.QUEUED, CaptureRequestState.LEASED}
                ):
                    return request.as_dict()
            request = _CaptureRequestRecord(
                request_id=uuid.uuid4().hex,
                source_url=source_url,
                source_type=source_type,
                resource_id=resource_id,
                requested_mode=mode,
                selection=safe_selection,
                created_at=now,
            )
            self._requests[request.request_id] = request
            self._condition.notify_all()
            return request.as_dict()

    def claim_capture_request(self) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        with self._condition:
            self._prune_requests(now)
            queued = sorted(
                (
                    request
                    for request in self._requests.values()
                    if request.state == CaptureRequestState.QUEUED
                ),
                key=lambda request: (request.created_at, request.request_id),
            )
            if not queued:
                return None
            request = queued[0]
            request.state = CaptureRequestState.LEASED
            request.leased_at = now
            return request.as_dict()

    def fail_capture_request(
        self,
        request_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        with self._condition:
            request = self._requests.get(request_id)
            if request is None or request.state == CaptureRequestState.COMPLETED:
                return False
            request.state = CaptureRequestState.FAILED
            request.finished_at = datetime.now(UTC)
            request.error_code = str(error_code or "capture_failed")[:120]
            request.error_message = str(error_message or "Automatic capture failed.")[:1000]
            self._condition.notify_all()
            return True

    def wait_for_capture(
        self,
        source_type: str,
        resource_id: str,
        *,
        request_id: str,
        timeout_seconds: int = DEFAULT_CAPTURE_REQUEST_TIMEOUT_SECONDS,
        max_age_seconds: int | None = None,
    ) -> BrowserCaptureEnvelope:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                try:
                    return self.get(
                        source_type,
                        resource_id,
                        max_age_seconds=max_age_seconds,
                    )
                except BrowserCaptureError as error:
                    if error.code not in {"capture_missing", "capture_stale"}:
                        raise
                request = self._requests.get(request_id)
                if request is not None and request.state == CaptureRequestState.FAILED:
                    raise BrowserCaptureError(
                        request.error_code or "automatic_capture_failed",
                        request.error_message or "The extension could not capture this source.",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserCaptureError(
                        "automatic_capture_timeout",
                        "The Chrome extension did not finish the queued source capture within "
                        f"{timeout_seconds} seconds. Check that it is installed, paired, and "
                        "enabled.",
                    )
                self._condition.wait(timeout=min(remaining, 1.0))

    def list_capture_requests(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        with self._lock:
            self._prune_requests(now)
            return [
                request.as_dict()
                for request in sorted(
                    self._requests.values(),
                    key=lambda request: (request.created_at, request.request_id),
                    reverse=True,
                )
            ]

    def get(
        self,
        source_type: str,
        resource_id: str,
        *,
        max_age_seconds: int | None = None,
    ) -> BrowserCaptureEnvelope:
        now = datetime.now(UTC)
        key = (source_type, resource_id)
        with self._lock:
            self._prune(now)
            record = self._records.get(key)
            if record is None:
                raise BrowserCaptureError(
                    "capture_missing",
                    "No recent browser capture matches this source. Open the file in Chrome, "
                    "capture it with the extension, then retry the preview.",
                )
            allowed_age = min(max_age_seconds or self.ttl_seconds, self.ttl_seconds)
            if now - record.received_at > timedelta(seconds=allowed_age):
                del self._records[key]
                raise BrowserCaptureError(
                    "capture_stale",
                    "The browser capture expired. Capture the source again and retry.",
                )
            return record.capture.model_copy(deep=True)

    def list_statuses(self) -> list[BrowserCaptureStatus]:
        now = datetime.now(UTC)
        with self._lock:
            self._prune(now)
            return sorted(
                (self._status(record) for record in self._records.values()),
                key=lambda item: item.received_at,
                reverse=True,
            )

    def clear(self, source_type: str | None = None, resource_id: str | None = None) -> int:
        with self._condition:
            keys = [
                key
                for key in self._records
                if (source_type is None or key[0] == source_type)
                and (resource_id is None or key[1] == resource_id)
            ]
            for key in keys:
                del self._records[key]
            if source_type is None and resource_id is None:
                self._requests.clear()
            self._condition.notify_all()
            return len(keys)

    def _prune(self, now: datetime) -> None:
        expired = [
            key
            for key, record in self._records.items()
            if now - record.received_at > timedelta(seconds=self.ttl_seconds)
        ]
        for key in expired:
            del self._records[key]

    def _prune_requests(self, now: datetime) -> None:
        for request in self._requests.values():
            if (
                request.state == CaptureRequestState.LEASED
                and request.leased_at is not None
                and now - request.leased_at > timedelta(seconds=CAPTURE_REQUEST_LEASE_SECONDS)
            ):
                request.state = CaptureRequestState.QUEUED
                request.leased_at = None
        for request in self._requests.values():
            if (
                request.state in {CaptureRequestState.QUEUED, CaptureRequestState.LEASED}
                and now - request.created_at > timedelta(seconds=CAPTURE_REQUEST_TTL_SECONDS)
            ):
                request.state = CaptureRequestState.FAILED
                request.finished_at = now
                request.error_code = "capture_request_expired"
                request.error_message = (
                    "The extension did not claim this capture request before it expired."
                )
        expired = [
            request_id
            for request_id, request in self._requests.items()
            if now - request.created_at > timedelta(seconds=CAPTURE_REQUEST_TTL_SECONDS)
            and request.state in {CaptureRequestState.COMPLETED, CaptureRequestState.FAILED}
        ]
        for request_id in expired:
            del self._requests[request_id]

    def _status(self, record: _CaptureRecord) -> BrowserCaptureStatus:
        return BrowserCaptureStatus(
            source_type=record.capture.source_type,
            resource_id=record.capture.resource_id,
            received_at=record.received_at,
            captured_at=record.capture.captured_at,
            expires_at=record.received_at + timedelta(seconds=self.ttl_seconds),
            methods_used=tuple(method.value for method in record.capture.methods_used),
            item_count=len(record.capture.items),
            screenshot_count=len(record.capture.screenshots),
        )
