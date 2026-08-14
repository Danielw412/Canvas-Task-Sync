from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from canvas_task_sync.browser_capture import (
    AcquisitionMode,
    BrowserCaptureBroker,
    BrowserCaptureEnvelope,
    BrowserCaptureError,
    BrowserCaptureItem,
    CaptureMethod,
    resource_id_from_url,
    source_type_from_url,
)
from canvas_task_sync.configuration import BrowserSheetSelection, BrowserSourceSettings
from canvas_task_sync.models import (
    AgendaBlock,
    BlockRole,
    ExtractionMode,
    SourceCapture,
    SourceImage,
)


class BrowserConnectorError(BrowserCaptureError):
    pass


def automatic_acquisition_mode(settings: BrowserSourceSettings) -> AcquisitionMode:
    return {
        ExtractionMode.TEXT: AcquisitionMode.TEXT,
        ExtractionMode.IMAGE: AcquisitionMode.SCREENSHOT,
        ExtractionMode.HYBRID: AcquisitionMode.BOTH,
        ExtractionMode.AUTO: AcquisitionMode.PREFER_SCREENSHOT,
    }[settings.extraction.mode]


def extension_selection(settings: BrowserSourceSettings) -> dict[str, Any]:
    selection = settings.selection
    return {
        "slideIds": list(selection.slide_ids),
        "sectionIds": list(selection.section_ids),
        "sheets": [
            {
                "id": sheet.sheet_id,
                "name": sheet.sheet_name,
                "range": sheet.range_a1,
            }
            for sheet in selection.sheets
        ],
    }


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_anchor_part(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
    return normalized[:300] or "unknown"


def _block_role(value: str) -> BlockRole:
    try:
        return BlockRole(value)
    except ValueError:
        return BlockRole.UNKNOWN


def _structured_text(item: BrowserCaptureItem) -> str:
    if item.text.strip():
        return item.text.strip()
    if item.structured_data is not None:
        return json.dumps(item.structured_data, ensure_ascii=False, separators=(",", ":"))
    return ""


def _sheet_matches(item: BrowserCaptureItem, selection: BrowserSheetSelection) -> bool:
    if selection.sheet_id and item.sheet_id != selection.sheet_id:
        return False
    if (
        selection.sheet_name
        and (item.sheet_name or "").casefold() != selection.sheet_name.casefold()
    ):
        return False
    if selection.range_a1:
        parent_range = str(item.metadata.get("selection_range") or "")
        if parent_range.casefold() != selection.range_a1.casefold():
            return False
    return True


def _selected_items(
    envelope: BrowserCaptureEnvelope,
    settings: BrowserSourceSettings,
) -> list[BrowserCaptureItem]:
    items = list(envelope.items)
    selection = settings.selection
    if envelope.source_type == "google_slides" and selection.slide_ids:
        items = [item for item in items if item.slide_id in selection.slide_ids]
        present = {item.slide_id for item in items}
        missing = [slide_id for slide_id in selection.slide_ids if slide_id not in present]
        if missing:
            raise BrowserConnectorError(
                "selection_missing",
                "The browser capture is missing configured slide(s): " + ", ".join(missing),
            )
    elif envelope.source_type == "google_docs" and selection.section_ids:
        items = [item for item in items if item.section_id in selection.section_ids]
        present = {item.section_id for item in items}
        missing = [section_id for section_id in selection.section_ids if section_id not in present]
        if missing:
            raise BrowserConnectorError(
                "selection_missing",
                "The browser capture is missing configured section(s): " + ", ".join(missing),
            )
    elif envelope.source_type == "google_sheets" and selection.sheets:
        filtered: list[BrowserCaptureItem] = []
        missing: list[str] = []
        for sheet in selection.sheets:
            matches = [item for item in items if _sheet_matches(item, sheet)]
            if not matches:
                missing.append(sheet.sheet_name or sheet.sheet_id or "unknown")
            filtered.extend(matches)
        if missing:
            raise BrowserConnectorError(
                "selection_missing",
                "The browser capture is missing configured sheet/range selections: "
                + ", ".join(missing),
            )
        seen: set[str] = set()
        items = [item for item in filtered if not (item.id in seen or seen.add(item.id))]
    return sorted(items, key=lambda item: (item.order, item.id))


def _visual_items(envelope: BrowserCaptureEnvelope) -> list[BrowserCaptureItem]:
    return [
        BrowserCaptureItem(
            id=screenshot.item_id or screenshot.id,
            kind="visual_capture",
            order=screenshot.order,
            slide_id=str(screenshot.metadata.get("slide_id") or "") or None,
            section_id=str(screenshot.metadata.get("section_id") or "") or None,
            sheet_id=str(screenshot.metadata.get("sheet_id") or "") or None,
            sheet_name=str(screenshot.metadata.get("sheet_name") or "") or None,
            range_a1=str(screenshot.metadata.get("range_a1") or "") or None,
            metadata={**screenshot.metadata, "visual_only": True},
        )
        for screenshot in envelope.screenshots
    ]


def _screenshot_matches_items(
    screenshot: Any,
    items: list[BrowserCaptureItem],
) -> bool:
    if not items or screenshot.item_id is None:
        return True
    if any(item.id == screenshot.item_id for item in items):
        return True
    metadata = screenshot.metadata
    slide_id = str(metadata.get("slide_id") or "")
    if slide_id and any(item.slide_id == slide_id for item in items):
        return True
    section_id = str(metadata.get("section_id") or "")
    if section_id and any(item.section_id == section_id for item in items):
        return True
    sheet_id = str(metadata.get("sheet_id") or "")
    return bool(sheet_id and any(item.sheet_id == sheet_id for item in items))


def _to_blocks(items: list[BrowserCaptureItem], source_type: str) -> list[AgendaBlock]:
    blocks: list[AgendaBlock] = []
    for item in items:
        text = _structured_text(item)
        metadata = dict(item.metadata)
        if not text:
            metadata["visual_only"] = True
        element_id = (
            str(metadata.get("table_id") or "")
            or item.slide_id
            or item.section_id
            or item.sheet_id
            or item.id
        )
        blocks.append(
            AgendaBlock(
                anchor=f"browser:{source_type}:{_safe_anchor_part(item.id)}",
                element_id=element_id,
                kind=item.kind,
                role=_block_role(item.role),
                row_index=item.row_index,
                column_index=item.column_index,
                row_label=item.row_label,
                text=text,
                order=item.order,
                slide_id=item.slide_id,
                section_id=item.section_id,
                sheet_id=item.sheet_id,
                sheet_name=item.sheet_name,
                range_a1=item.range_a1,
                structured_data=item.structured_data,
                metadata=metadata,
            )
        )
    return blocks


def build_browser_transcript(blocks: list[AgendaBlock]) -> str:
    sections: list[str] = []
    for block in sorted(blocks, key=lambda item: (item.order, item.anchor)):
        context = [
            f"anchor={block.anchor}",
            f"role={block.role.value}",
            f"order={block.order}",
        ]
        for key, value in (
            ("day", block.row_label),
            ("slide", block.slide_id),
            ("section", block.section_id),
            ("sheet", block.sheet_name or block.sheet_id),
            ("range", block.range_a1),
            ("row", block.row_index),
            ("column", block.column_index),
        ):
            if value is not None:
                context.append(f"{key}={value}")
        sections.append(f"[{' '.join(context)}]\n{block.text}")
    return "\n\n".join(sections)


def canonical_browser_capture_hash(
    envelope: BrowserCaptureEnvelope,
    items: list[BrowserCaptureItem],
) -> str:
    material: dict[str, Any] = {
        "schema_version": envelope.schema_version,
        "source_type": envelope.source_type,
        "resource_id": envelope.resource_id,
        "selection": envelope.selection,
        "items": [item.model_dump(mode="json") for item in items],
        "metadata": envelope.metadata,
    }
    if CaptureMethod.TEXT not in envelope.methods_used:
        material["screenshots"] = [
            {
                "id": screenshot.id,
                "item_id": screenshot.item_id,
                "order": screenshot.order,
                "sha256": screenshot.sha256,
                "width": screenshot.width,
                "height": screenshot.height,
                "metadata": screenshot.metadata,
            }
            for screenshot in envelope.screenshots
        ]
    return _stable_hash(material)


class BrowserConnectorSource:
    def __init__(
        self,
        settings: BrowserSourceSettings,
        *,
        capture_broker: BrowserCaptureBroker | None,
    ) -> None:
        if capture_broker is None:
            raise BrowserConnectorError(
                "extension_bridge_unavailable",
                "Browser sources require the local control center. Start 'canvas-task-sync web' "
                "and send a capture from the Chrome extension.",
            )
        self.settings = settings
        self.capture_broker = capture_broker
        self.source_type = source_type_from_url(settings.url)
        self.resource_id = resource_id_from_url(settings.url)

    @property
    def source_key(self) -> str:
        selection = self.settings.selection.model_dump(mode="json")
        selection_suffix = f":{_stable_hash(selection)[:12]}" if any(selection.values()) else ""
        return f"browser:{self.source_type}:{self.resource_id}{selection_suffix}"

    def _envelope(self) -> BrowserCaptureEnvelope:
        try:
            envelope = self.capture_broker.get(
                self.source_type,
                self.resource_id,
                max_age_seconds=self.settings.freshness_seconds,
            )
        except BrowserCaptureError as error:
            if error.code not in {"capture_missing", "capture_stale"}:
                raise
            request = self.capture_broker.request_capture(
                self.settings.url,
                automatic_acquisition_mode(self.settings),
                extension_selection(self.settings),
            )
            envelope = self.capture_broker.wait_for_capture(
                self.source_type,
                self.resource_id,
                request_id=str(request["request_id"]),
                max_age_seconds=self.settings.freshness_seconds,
            )
        methods = set(envelope.methods_used)
        configured = self.settings.extraction.mode
        if (
            configured in {ExtractionMode.TEXT, ExtractionMode.HYBRID}
            and CaptureMethod.TEXT not in methods
        ):
            raise BrowserConnectorError(
                "capture_method_mismatch",
                f"Extraction mode '{configured.value}' needs text content. Capture this source "
                "again with text or screenshot + text enabled.",
            )
        return envelope

    def capture(self, *, include_image: bool) -> SourceCapture:
        envelope = self._envelope()
        items = _selected_items(envelope, self.settings)
        if not items and envelope.screenshots:
            items = _visual_items(envelope)
        if not items:
            raise BrowserConnectorError(
                "capture_blank",
                "The browser capture contains no selected text, cells, sections, or visual "
                "targets.",
            )
        blocks = _to_blocks(items, envelope.source_type)
        first = blocks[0]
        capture = SourceCapture(
            source_key=self.source_key,
            source_url=self.settings.url,
            source_type=envelope.source_type,
            resource_id=envelope.resource_id,
            presentation_id=(
                envelope.resource_id if envelope.source_type == "google_slides" else None
            ),
            page_id=(
                first.slide_id
                or first.section_id
                or first.sheet_id
                or envelope.resource_id
            ),
            page_hash=canonical_browser_capture_hash(envelope, items),
            transcript=build_browser_transcript(blocks),
            blocks=blocks,
            captured_at=envelope.captured_at,
            selection=envelope.selection,
            source_metadata={
                "capture_id": envelope.capture_id,
                "title": envelope.title,
                "requested_mode": envelope.requested_mode.value,
                "methods_used": [method.value for method in envelope.methods_used],
                "fallback_used": envelope.fallback_used,
                "warnings": envelope.warnings,
                "metadata": envelope.metadata,
                "screenshot_available": bool(envelope.screenshots),
            },
        )
        return self.add_image(capture) if include_image else capture

    def add_image(self, capture: SourceCapture) -> SourceCapture:
        if capture.source_key != self.source_key:
            raise ValueError("The capture does not belong to this browser source.")
        if capture.images:
            return capture
        envelope = self._envelope()
        items = _selected_items(envelope, self.settings)
        expected_hash = canonical_browser_capture_hash(
            envelope,
            items or _visual_items(envelope),
        )
        if expected_hash != capture.page_hash:
            raise BrowserConnectorError(
                "capture_changed",
                "The extension capture changed while the preview was starting. Retry the preview.",
            )
        if not envelope.screenshots:
            raise BrowserConnectorError(
                "screenshot_missing",
                "This extraction mode needs a screenshot. Capture the source again with screenshot "
                "or screenshot + text enabled.",
            )

        screenshots = [
            screenshot
            for screenshot in envelope.screenshots
            if _screenshot_matches_items(screenshot, items)
        ]
        if not screenshots:
            raise BrowserConnectorError(
                "selection_screenshot_missing",
                "No screenshot matches the configured source selection. Capture it again.",
            )
        images = [
            SourceImage(
                id=screenshot.id,
                item_id=screenshot.item_id,
                order=screenshot.order,
                mime_type=screenshot.mime_type,
                data=screenshot.decoded_bytes(),
                sha256=screenshot.sha256 or "",
                width=screenshot.width,
                height=screenshot.height,
                metadata=screenshot.metadata,
            )
            for screenshot in sorted(screenshots, key=lambda item: (item.order, item.id))
        ]
        first = images[0]
        return capture.model_copy(
            update={
                "images": images,
                "image_bytes": first.data,
                "image_mime_type": first.mime_type,
                "image_sha256": first.sha256,
            }
        )
