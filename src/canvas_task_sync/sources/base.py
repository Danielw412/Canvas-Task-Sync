from __future__ import annotations

from typing import Protocol

from canvas_task_sync.models import SourceCapture


class SourceAdapter(Protocol):
    def capture(self, *, include_image: bool) -> SourceCapture:
        """Capture the configured agenda source without mutating it."""


class IncrementalImageSourceAdapter(SourceAdapter, Protocol):
    def add_image(self, capture: SourceCapture) -> SourceCapture:
        """Attach image bytes without repeating the canonical source read."""
