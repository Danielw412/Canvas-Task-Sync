from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from canvas_task_sync.configuration import GoogleSlidesSourceSettings
from canvas_task_sync.models import AgendaBlock, BlockRole, SourceCapture

PRESENTATION_ID_PATTERN = re.compile(r"/presentation/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
EPHEMERAL_PAGE_KEYS = {"contentUrl", "revisionId", "thumbnailUrl"}
MAX_THUMBNAIL_BYTES = 20 * 1024 * 1024


class GoogleSlidesError(RuntimeError):
    pass


def presentation_id_from_url(url: str) -> str:
    match = PRESENTATION_ID_PATTERN.search(url)
    if not match:
        raise ValueError(f"Could not find a Google Slides presentation ID in URL: {url}")
    return match.group(1)


def text_from_content(content: dict[str, Any] | None) -> str:
    if not content:
        return ""
    return "".join(
        element.get("textRun", {}).get("content", "")
        for element in content.get("textElements", [])
    ).strip()


def _without_ephemeral_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_ephemeral_values(child)
            for key, child in value.items()
            if key not in EPHEMERAL_PAGE_KEYS
        }
    if isinstance(value, list):
        return [_without_ephemeral_values(child) for child in value]
    return value


def canonical_page_hash(page: dict[str, Any]) -> str:
    canonical = json.dumps(
        _without_ephemeral_values(deepcopy(page)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _geometry(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "size": element.get("size", {}),
        "transform": element.get("transform", {}),
    }


def _header_role(text: str) -> BlockRole | None:
    normalized = " ".join(text.casefold().split())
    if "learning activities" in normalized or normalized == "assignments":
        return BlockRole.HEADER
    if "learning targets" in normalized:
        return BlockRole.HEADER
    return None


def parse_page_blocks(page: dict[str, Any]) -> list[AgendaBlock]:
    blocks: list[AgendaBlock] = []

    for element in page.get("pageElements", []):
        element_id = str(element.get("objectId", "unknown"))
        shape = element.get("shape")
        if shape:
            text = text_from_content(shape.get("text"))
            if text:
                blocks.append(
                    AgendaBlock(
                        anchor=f"shape:{element_id}",
                        element_id=element_id,
                        kind="shape",
                        role=BlockRole.HEADER,
                        text=text,
                        geometry=_geometry(element),
                    )
                )

        table = element.get("table")
        if not table:
            continue

        rows = table.get("tableRows", [])
        cell_text: dict[tuple[int, int], str] = {}
        for fallback_row, row in enumerate(rows):
            for fallback_column, cell in enumerate(row.get("tableCells", [])):
                location = cell.get("location", {})
                row_index = int(location.get("rowIndex", fallback_row))
                column_index = int(location.get("columnIndex", fallback_column))
                cell_text[(row_index, column_index)] = text_from_content(cell.get("text"))

        role_by_column: dict[int, BlockRole] = {0: BlockRole.DAY}
        for (_row_index, column_index), text in cell_text.items():
            normalized = " ".join(text.casefold().split())
            if "learning activities" in normalized:
                role_by_column[column_index] = BlockRole.LEARNING
            elif normalized == "assignments":
                role_by_column[column_index] = BlockRole.ASSIGNMENTS

        row_labels = {
            row_index: text.strip()
            for (row_index, column_index), text in cell_text.items()
            if column_index == 0 and text.strip()
        }

        for (row_index, column_index), text in sorted(cell_text.items()):
            if not text:
                continue
            role = _header_role(text) or role_by_column.get(column_index, BlockRole.UNKNOWN)
            if row_index == 0:
                role = BlockRole.HEADER
            blocks.append(
                AgendaBlock(
                    anchor=f"table:{element_id}:r{row_index}:c{column_index}",
                    element_id=element_id,
                    kind="table_cell",
                    role=role,
                    row_index=row_index,
                    column_index=column_index,
                    row_label=row_labels.get(row_index),
                    text=text,
                    geometry=_geometry(element),
                )
            )

    return blocks


def build_anchor_transcript(blocks: list[AgendaBlock]) -> str:
    sections: list[str] = []
    for block in blocks:
        day = block.row_label or "-"
        sections.append(
            f"[anchor={block.anchor} role={block.role.value} day={day}]\n{block.text}"
        )
    return "\n\n".join(sections)


class GoogleSlidesSource:
    def __init__(
        self,
        settings: GoogleSlidesSourceSettings,
        credentials: Credentials,
        *,
        session: requests.Session | None = None,
        service: Any | None = None,
    ) -> None:
        self.settings = settings
        self.presentation_id = presentation_id_from_url(settings.url)
        self.session = session or requests.Session()
        self.service = service or build(
            "slides",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )

    @property
    def source_key(self) -> str:
        return f"google_slides:{self.presentation_id}:{self.settings.page_id}"

    def _get_page(self) -> dict[str, Any]:
        try:
            return (
                self.service.presentations()
                .pages()
                .get(
                    presentationId=self.presentation_id,
                    pageObjectId=self.settings.page_id,
                )
                .execute()
            )
        except Exception as error:
            raise GoogleSlidesError(
                f"Could not read target slide {self.settings.page_id}."
            ) from error

    def _get_thumbnail(self) -> tuple[bytes, str]:
        size = self.settings.extraction.thumbnail_size.upper()
        try:
            response = (
                self.service.presentations()
                .pages()
                .getThumbnail(
                    presentationId=self.presentation_id,
                    pageObjectId=self.settings.page_id,
                    thumbnailProperties_mimeType="PNG",
                    thumbnailProperties_thumbnailSize=size,
                )
                .execute()
            )
            content_url = response["contentUrl"]
            image_response = self.session.get(content_url, timeout=30)
            image_response.raise_for_status()
        except Exception as error:
            raise GoogleSlidesError(
                f"Could not capture a thumbnail for slide {self.settings.page_id}."
            ) from error

        image_bytes = image_response.content
        if not image_bytes:
            raise GoogleSlidesError("Google Slides returned an empty thumbnail.")
        if len(image_bytes) > MAX_THUMBNAIL_BYTES:
            raise GoogleSlidesError("Slide thumbnail exceeded the 20 MB Gemini inline limit.")
        return image_bytes, "image/png"

    def capture(self, *, include_image: bool) -> SourceCapture:
        page = self._get_page()
        blocks = parse_page_blocks(page)
        if not blocks:
            raise GoogleSlidesError("The target slide contains no readable text or tables.")

        image_bytes: bytes | None = None
        image_mime_type: str | None = None
        image_sha256: str | None = None
        if include_image:
            image_bytes, image_mime_type = self._get_thumbnail()
            image_sha256 = hashlib.sha256(image_bytes).hexdigest()

        return SourceCapture(
            source_key=self.source_key,
            source_url=self.settings.url,
            source_type="google_slides",
            resource_id=self.presentation_id,
            presentation_id=self.presentation_id,
            page_id=self.settings.page_id,
            page_hash=canonical_page_hash(page),
            transcript=build_anchor_transcript(blocks),
            blocks=blocks,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            image_sha256=image_sha256,
        )

    def add_image(self, capture: SourceCapture) -> SourceCapture:
        """Attach a fresh thumbnail to an already-hashed page capture."""
        if capture.source_key != self.source_key:
            raise ValueError("The capture does not belong to this Google Slides source.")
        if capture.image_bytes is not None:
            return capture
        image_bytes, image_mime_type = self._get_thumbnail()
        return capture.model_copy(
            update={
                "image_bytes": image_bytes,
                "image_mime_type": image_mime_type,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
        )
