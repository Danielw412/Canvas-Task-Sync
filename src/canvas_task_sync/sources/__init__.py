from __future__ import annotations

from typing import Any

from google.oauth2.credentials import Credentials

from canvas_task_sync.configuration import (
    BrowserSourceSettings,
    SourceSettings,
)
from canvas_task_sync.sources.base import IncrementalImageSourceAdapter, SourceAdapter
from canvas_task_sync.sources.browser_connector import BrowserConnectorSource
from canvas_task_sync.sources.google_slides import GoogleSlidesSource


def create_source_adapter(
    settings: SourceSettings,
    credentials: Credentials,
    **kwargs: Any,
) -> SourceAdapter:
    """Create a configured adapter; future source types register at this boundary."""
    if settings.type == "google_slides":
        kwargs.pop("capture_broker", None)
        return GoogleSlidesSource(settings, credentials, **kwargs)
    if settings.type == "browser":
        capture_broker = kwargs.pop("capture_broker", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"Unexpected browser source option(s): {unexpected}")
        return BrowserConnectorSource(settings, capture_broker=capture_broker)
    raise ValueError(f"Unsupported source adapter type: {settings.type}")


__all__ = [
    "BrowserConnectorSource",
    "BrowserSourceSettings",
    "GoogleSlidesSource",
    "IncrementalImageSourceAdapter",
    "SourceAdapter",
    "create_source_adapter",
]
