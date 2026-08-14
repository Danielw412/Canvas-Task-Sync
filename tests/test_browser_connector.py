from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canvas_task_sync.browser_capture import (
    AcquisitionMode,
    BrowserCaptureBroker,
    BrowserCaptureEnvelope,
    BrowserCaptureError,
    BrowserCaptureItem,
    BrowserCaptureScreenshot,
    CaptureMethod,
)
from canvas_task_sync.configuration import BrowserSourceSettings, CourseSettings
from canvas_task_sync.gemini import GeminiExtractor
from canvas_task_sync.models import ExtractionMode
from canvas_task_sync.sources.browser_connector import BrowserConnectorSource
from canvas_task_sync.web_app import create_web_app

FIXTURES = Path(__file__).parent / "fixtures"
SLIDES_URL = "https://docs.google.com/presentation/d/slides_fixture/edit?slide=id.slide-a"
DOCS_URL = "https://docs.google.com/document/d/docs_fixture/edit?tab=t.0"
SHEETS_URL = "https://docs.google.com/spreadsheets/d/sheets_fixture/edit?gid=0"


def _png_data_url() -> str:
    encoded = (FIXTURES / "spanish_target_page.png.b64").read_text(encoding="ascii")
    return f"data:image/png;base64,{''.join(encoded.split())}"


def _screenshot(
    screenshot_id: str,
    *,
    item_id: str,
    order: int = 0,
    metadata: dict[str, object] | None = None,
) -> BrowserCaptureScreenshot:
    return BrowserCaptureScreenshot(
        id=screenshot_id,
        item_id=item_id,
        order=order,
        mime_type="image/png",
        data_url=_png_data_url(),
        width=1200,
        height=800,
        metadata=metadata or {},
    )


def _envelope(
    *,
    source_type: str,
    source_url: str,
    resource_id: str,
    items: list[BrowserCaptureItem],
    screenshots: list[BrowserCaptureScreenshot] | None = None,
    requested_mode: AcquisitionMode = AcquisitionMode.TEXT,
    methods_used: list[CaptureMethod] | None = None,
    fallback_used: bool = False,
    selection: dict[str, object] | None = None,
) -> BrowserCaptureEnvelope:
    return BrowserCaptureEnvelope(
        capture_id=f"capture-{source_type}-fixture",
        source_type=source_type,
        source_url=source_url,
        resource_id=resource_id,
        title="Fixture agenda",
        captured_at=datetime.now(UTC),
        requested_mode=requested_mode,
        methods_used=methods_used or [CaptureMethod.TEXT],
        fallback_used=fallback_used,
        items=items,
        screenshots=screenshots or [],
        selection=selection or {},
        metadata={"adapter_id": f"{source_type}-v1"},
    )


def test_capture_schema_enforces_modes_urls_images_and_sensitive_metadata():
    screenshot = _screenshot("shot", item_id="visual")
    capture = _envelope(
        source_type="google_slides",
        source_url=SLIDES_URL,
        resource_id="slides_fixture",
        items=[
            BrowserCaptureItem(
                id="visual",
                kind="visual_capture",
                slide_id="slide-a",
                metadata={"visual_only": True},
            )
        ],
        screenshots=[screenshot],
        requested_mode=AcquisitionMode.PREFER_TEXT,
        methods_used=[CaptureMethod.SCREENSHOT],
        fallback_used=True,
    )
    assert capture.fallback_used is True
    assert capture.screenshots[0].sha256

    payload = capture.model_dump(mode="json")
    payload["source_url"] = DOCS_URL
    with pytest.raises(ValueError, match="source_type does not match"):
        BrowserCaptureEnvelope.model_validate(payload)

    payload = capture.model_dump(mode="json")
    payload["metadata"] = {"authorization": "Bearer should-not-be-accepted"}
    with pytest.raises(ValueError, match="authentication or credential"):
        BrowserCaptureEnvelope.model_validate(payload)

    payload = capture.model_dump(mode="json")
    payload["requested_mode"] = "both"
    with pytest.raises(ValueError, match="Both mode requires"):
        BrowserCaptureEnvelope.model_validate(payload)


def test_browser_configuration_accepts_google_multi_account_urls():
    settings = BrowserSourceSettings(
        url="https://docs.google.com/document/u/2/d/docs_fixture/edit",
        source_format="google_docs",
        extraction={"mode": "text"},
    )
    assert settings.source_format == "google_docs"


def test_broker_is_bounded_ephemeral_and_reports_missing_or_stale_captures():
    broker = BrowserCaptureBroker(ttl_seconds=60, max_records=1)
    slides = _envelope(
        source_type="google_slides",
        source_url=SLIDES_URL,
        resource_id="slides_fixture",
        items=[BrowserCaptureItem(id="slide-a:text", kind="shape", text="Read chapter")],
    )
    docs = _envelope(
        source_type="google_docs",
        source_url=DOCS_URL,
        resource_id="docs_fixture",
        items=[BrowserCaptureItem(id="intro:p", kind="paragraph", text="Read chapter")],
    )
    broker.put(slides)
    broker.put(docs)
    with pytest.raises(BrowserCaptureError, match="No recent browser capture"):
        broker.get("google_slides", "slides_fixture")
    assert broker.get("google_docs", "docs_fixture").resource_id == "docs_fixture"

    record = broker._records[("google_docs", "docs_fixture")]  # Controlled clock edge fixture.
    record.received_at = datetime.now(UTC) - timedelta(minutes=2)
    with pytest.raises(BrowserCaptureError, match="No recent browser capture|expired"):
        broker.get("google_docs", "docs_fixture")


def test_automatic_capture_queue_is_fifo_deduplicated_and_reports_failures():
    broker = BrowserCaptureBroker()
    first = broker.request_capture(
        DOCS_URL,
        AcquisitionMode.BOTH,
        {"sectionIds": ["document-start"]},
    )
    duplicate = broker.request_capture(
        DOCS_URL,
        AcquisitionMode.BOTH,
        {"sectionIds": ["document-start"]},
    )
    second = broker.request_capture(SHEETS_URL, AcquisitionMode.TEXT)

    assert duplicate["request_id"] == first["request_id"]
    assert broker.claim_capture_request()["request_id"] == first["request_id"]
    assert broker.claim_capture_request()["request_id"] == second["request_id"]

    docs = _envelope(
        source_type="google_docs",
        source_url=DOCS_URL,
        resource_id="docs_fixture",
        items=[BrowserCaptureItem(id="document-start:p", kind="paragraph", text="Agenda")],
        requested_mode=AcquisitionMode.BOTH,
        methods_used=[CaptureMethod.TEXT, CaptureMethod.SCREENSHOT],
        screenshots=[_screenshot("docs-shot", item_id="document-start:visual")],
    )
    broker.put(docs)
    captured = broker.wait_for_capture(
        "google_docs",
        "docs_fixture",
        request_id=first["request_id"],
        timeout_seconds=1,
    )
    assert captured.resource_id == "docs_fixture"

    assert broker.fail_capture_request(
        second["request_id"],
        error_code="access_denied",
        error_message="This browser session cannot open the workbook.",
    )
    with pytest.raises(BrowserCaptureError, match="cannot open the workbook"):
        broker.wait_for_capture(
            "google_sheets",
            "sheets_fixture",
            request_id=second["request_id"],
            timeout_seconds=1,
        )


def test_slides_browser_adapter_preserves_selection_order_metadata_and_multiple_images():
    broker = BrowserCaptureBroker()
    items = [
        BrowserCaptureItem(
            id="slide-b:shape",
            kind="shape",
            order=10_000,
            text="Submit project",
            slide_id="slide-b",
            metadata={"slide_order": 1},
        ),
        BrowserCaptureItem(
            id="slide-a:assignment",
            kind="table_cell",
            order=2,
            text="Read chapter 2",
            role="assignments",
            row_index=1,
            column_index=2,
            row_label="M",
            slide_id="slide-a",
            metadata={"slide_order": 0},
        ),
    ]
    screenshots = [
        _screenshot(
            "shot-a",
            item_id="slide-a:visual",
            metadata={"slide_id": "slide-a"},
        ),
        _screenshot(
            "shot-b",
            item_id="slide-b:visual",
            order=1,
            metadata={"slide_id": "slide-b"},
        ),
    ]
    broker.put(
        _envelope(
            source_type="google_slides",
            source_url=SLIDES_URL,
            resource_id="slides_fixture",
            items=items,
            screenshots=screenshots,
            requested_mode=AcquisitionMode.BOTH,
            methods_used=[CaptureMethod.TEXT, CaptureMethod.SCREENSHOT],
            selection={"slideIds": ["slide-a", "slide-b"]},
        )
    )
    settings = BrowserSourceSettings(
        url=SLIDES_URL,
        source_format="google_slides",
        selection={"slide_ids": ["slide-a", "slide-b"]},
        extraction={"mode": "hybrid"},
    )
    source = BrowserConnectorSource(settings, capture_broker=broker)
    capture = source.capture(include_image=False)
    assert [block.slide_id for block in capture.blocks] == ["slide-a", "slide-b"]
    assert capture.blocks[0].row_label == "M"
    assert capture.blocks[0].role.value == "assignments"
    assert capture.source_metadata["methods_used"] == ["text", "screenshot"]
    with_images = source.add_image(capture)
    assert len(with_images.images) == 2
    assert with_images.image_bytes == with_images.images[0].data

    class ImagesBackend:
        def __init__(self) -> None:
            self.images = []

        def generate(self, *, prompt, image_bytes, image_mime_type, images=None):
            del prompt, image_bytes, image_mime_type
            self.images = images or []
            return []

    image_settings = settings.model_copy(deep=True)
    image_settings.extraction.mode = ExtractionMode.IMAGE
    course = CourseSettings(
        name="Browser Slides",
        prefix="SLIDES",
        task_list="School",
        source=image_settings,
    )
    backend = ImagesBackend()
    GeminiExtractor(backend).extract(with_images, course)
    assert [image.id for image in backend.images] == ["shot-a", "shot-b"]


def test_docs_and_sheets_browser_adapters_preserve_source_specific_structure():
    broker = BrowserCaptureBroker()
    docs_items = [
        BrowserCaptureItem(
            id="week:heading",
            kind="heading",
            text="August 10-14, 2026",
            role="header",
            section_id="week",
            metadata={"heading_level": 1, "section_title": "Week"},
        ),
        BrowserCaptureItem(
            id="week:cell",
            kind="table_cell",
            order=1,
            text="Bring worksheet",
            row_index=2,
            column_index=1,
            row_label="W",
            section_id="week",
            structured_data={"row": 2, "column": 1},
        ),
    ]
    broker.put(
        _envelope(
            source_type="google_docs",
            source_url=DOCS_URL,
            resource_id="docs_fixture",
            items=docs_items,
            selection={"sectionIds": ["week"]},
        )
    )
    docs_source = BrowserConnectorSource(
        BrowserSourceSettings(
            url=DOCS_URL,
            source_format="google_docs",
            selection={"section_ids": ["week"]},
            extraction={"mode": "text"},
        ),
        capture_broker=broker,
    ).capture(include_image=False)
    assert docs_source.blocks[0].section_id == "week"
    assert docs_source.blocks[1].structured_data == {"row": 2, "column": 1}

    sheet_items = [
        BrowserCaptureItem(
            id="0:A1",
            kind="cell",
            text="Assignments",
            role="header",
            sheet_id="0",
            sheet_name="Agenda",
            range_a1="A1",
            structured_data={"row": 0, "column": 0, "value": "Assignments"},
            metadata={"selection_range": "A1:C20", "sheet_order": 0},
        ),
        BrowserCaptureItem(
            id="0:C2",
            kind="cell",
            order=2,
            text="Read chapter 2",
            role="assignments",
            row_index=1,
            column_index=2,
            row_label="M",
            sheet_id="0",
            sheet_name="Agenda",
            range_a1="C2",
            structured_data={"row": 1, "column": 2, "value": "Read chapter 2"},
            metadata={"selection_range": "A1:C20", "sheet_order": 0},
        ),
    ]
    broker.put(
        _envelope(
            source_type="google_sheets",
            source_url=SHEETS_URL,
            resource_id="sheets_fixture",
            items=sheet_items,
            selection={"sheets": [{"id": "0", "name": "Agenda", "range": "A1:C20"}]},
        )
    )
    sheets_source = BrowserConnectorSource(
        BrowserSourceSettings(
            url=SHEETS_URL,
            source_format="google_sheets",
            selection={
                "sheets": [
                    {"sheet_id": "0", "sheet_name": "Agenda", "range_a1": "A1:C20"}
                ]
            },
            extraction={"mode": "text"},
        ),
        capture_broker=broker,
    ).capture(include_image=False)
    assert sheets_source.blocks[1].sheet_name == "Agenda"
    assert sheets_source.blocks[1].range_a1 == "C2"
    assert sheets_source.blocks[1].row_index == 1
    assert sheets_source.blocks[1].column_index == 2


def test_browser_adapter_rejects_method_mismatch_and_missing_connection():
    with pytest.raises(BrowserCaptureError, match="local control center"):
        BrowserConnectorSource(
            BrowserSourceSettings(url=DOCS_URL, extraction={"mode": "text"}),
            capture_broker=None,
        )

    broker = BrowserCaptureBroker()
    broker.put(
        _envelope(
            source_type="google_slides",
            source_url=SLIDES_URL,
            resource_id="slides_fixture",
            items=[
                BrowserCaptureItem(
                    id="visual",
                    kind="visual_capture",
                    slide_id="slide-a",
                    metadata={"visual_only": True},
                )
            ],
            screenshots=[_screenshot("shot", item_id="visual")],
            requested_mode=AcquisitionMode.SCREENSHOT,
            methods_used=[CaptureMethod.SCREENSHOT],
        )
    )
    with pytest.raises(BrowserCaptureError, match="needs text content"):
        BrowserConnectorSource(
            BrowserSourceSettings(url=SLIDES_URL, extraction={"mode": "text"}),
            capture_broker=broker,
        ).capture(include_image=False)


def _write_browser_project(root: Path) -> Path:
    config = root / "config" / "courses.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"""version: 1
state_path: .canvas-task-sync/state.sqlite3
gemini_model: test-model
courses:
  agenda:
    name: Browser Agenda
    prefix: SCHOOL
    task_list: School
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    source:
      type: browser
      url: {SHEETS_URL}
      source_format: google_sheets
      freshness_seconds: 900
      extraction:
        mode: text
""",
        encoding="utf-8",
    )
    return config


def test_extension_bridge_pairs_accepts_and_clears_without_persisting_content(tmp_path):
    app = create_web_app(_write_browser_project(tmp_path))
    unique_content = "PRIVATE-SCHOOL-CONTENT-NEVER-PERSIST"
    capture = _envelope(
        source_type="google_sheets",
        source_url=SHEETS_URL,
        resource_id="sheets_fixture",
        items=[
            BrowserCaptureItem(
                id="0:A1",
                kind="cell",
                text=unique_content,
                sheet_id="0",
                sheet_name="Agenda",
                range_a1="A1",
            )
        ],
    )
    with TestClient(app) as client:
        setup = client.get("/api/v1/settings/extension").json()
        token = setup["pairing_token"]
        origin = f"chrome-extension://{'a' * 32}"

        rejected = client.post(
            "/api/v1/extension/captures",
            headers={"origin": origin, "x-extension-token": "wrong"},
            json=capture.model_dump(mode="json"),
        )
        assert rejected.status_code == 401
        assert rejected.json()["error"]["code"] == "extension_pairing_failed"

        bad_origin = client.post(
            "/api/v1/extension/captures",
            headers={
                "origin": "https://attacker.invalid",
                "x-extension-token": token,
            },
            json=capture.model_dump(mode="json"),
        )
        assert bad_origin.status_code == 403

        accepted = client.post(
            "/api/v1/extension/captures",
            headers={"origin": origin, "x-extension-token": token},
            json=capture.model_dump(mode="json"),
        )
        assert accepted.status_code == 202, accepted.text
        status = client.get(
            "/api/v1/extension/status",
            headers={"origin": origin, "x-extension-token": token},
        )
        assert status.status_code == 200
        assert status.json()["captures"][0]["item_count"] == 1

        source = app.state.runtime.create_source_adapter(
            app.state.runtime.settings.course("agenda").source,
            credentials=object(),
        )
        assert source.capture(include_image=False).blocks[0].text == unique_content

        cleared = client.delete(
            "/api/v1/extension/captures",
            headers={"origin": origin, "x-extension-token": token},
        )
        assert cleared.status_code == 204
        with pytest.raises(BrowserCaptureError, match="No recent browser capture"):
            app.state.runtime.capture_broker.get("google_sheets", "sheets_fixture")

    persisted = b"".join(
        path.read_bytes()
        for path in (tmp_path / ".canvas-task-sync").glob("*")
        if path.is_file()
    )
    assert unique_content.encode() not in persisted


def test_run_all_dispatches_courses_together_and_queues_browser_capture_fifo(tmp_path):
    app = create_web_app(_write_browser_project(tmp_path))
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.settings.courses["docs"] = CourseSettings(
            name="Docs Agenda",
            prefix="DOCS",
            task_list="School",
            source=BrowserSourceSettings(
                url=DOCS_URL,
                source_format="google_docs",
                extraction={"mode": "text"},
            ),
        )
        queued_courses: list[str] = []

        def record_preview(payload, **_kwargs):
            queued_courses.append(payload.course_id)
            return len(queued_courses)

        runtime.runs.create_preview = record_preview
        csrf = client.get("/api/v1/bootstrap").json()["csrf_token"]
        response = client.post(
            "/api/v1/runs/all",
            headers={"x-csrf-token": csrf},
            json={"include_past": False},
        )

        assert response.status_code == 202, response.text
        assert queued_courses == ["agenda", "docs"]
        assert response.json()["run_ids"] == [1, 2]
        assert len(response.json()["capture_request_ids"]) == 2

        setup = client.get("/api/v1/settings/extension").json()
        headers = {
            "origin": f"chrome-extension://{'a' * 32}",
            "x-extension-token": setup["pairing_token"],
        }
        first = client.get("/api/v1/extension/capture-requests/next", headers=headers)
        second = client.get("/api/v1/extension/capture-requests/next", headers=headers)
        assert [first.json()["source_type"], second.json()["source_type"]] == [
            "google_sheets",
            "google_docs",
        ]


def test_existing_google_slides_configuration_remains_unchanged(spanish_course):
    assert spanish_course.source.type == "google_slides"
    assert spanish_course.source.page_id == "g8596fffd0c_4_6"
    assert spanish_course.source.extraction.mode == ExtractionMode.HYBRID
