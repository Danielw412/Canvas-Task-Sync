from __future__ import annotations

from copy import deepcopy

from canvas_task_sync.sources.google_slides import (
    GoogleSlidesSource,
    canonical_page_hash,
    parse_page_blocks,
    presentation_id_from_url,
)


def test_parses_only_target_page_structure_and_exact_unicode(spanish_page):
    blocks = parse_page_blocks(spanish_page)
    by_anchor = {block.anchor: block for block in blocks}

    assert by_anchor["table:agenda_table:r3:c2"].role.value == "assignments"
    assert by_anchor["table:agenda_table:r4:c1"].role.value == "learning"
    assert by_anchor["table:agenda_table:r3:c2"].text == "Completar actividades de práctica - VHL"
    assert "Presentar Conversaciones hipotéticas" in by_anchor["table:agenda_table:r4:c1"].text
    assert "entregarla aquí" in by_anchor["table:agenda_table:r4:c2"].text
    assert "De Niño" in by_anchor["table:agenda_table:r5:c1"].text
    assert "¡Traer DINERO!" in by_anchor["table:agenda_table:r5:c2"].text


def test_canonical_hash_ignores_ephemeral_urls_but_tracks_visual_changes(spanish_page):
    changed_ephemeral = deepcopy(spanish_page)
    changed_ephemeral["contentUrl"] = "https://temporary.invalid/another"
    changed_ephemeral["revisionId"] = "another-revision"
    assert canonical_page_hash(changed_ephemeral) == canonical_page_hash(spanish_page)

    changed_style = deepcopy(spanish_page)
    changed_style["pageElements"][0]["transform"]["translateY"] = 7
    assert canonical_page_hash(changed_style) != canonical_page_hash(spanish_page)

    changed_text = deepcopy(spanish_page)
    changed_text["pageElements"][1]["table"]["tableRows"][3]["tableCells"][2]["text"][
        "textElements"
    ][0]["textRun"]["content"] = "Otra práctica\n"
    assert canonical_page_hash(changed_text) != canonical_page_hash(spanish_page)


def test_presentation_id_parser_accepts_edit_url():
    url = "https://docs.google.com/presentation/d/abc_123-Z/edit?slide=id.page"
    assert presentation_id_from_url(url) == "abc_123-Z"


class _Request:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class _Pages:
    def __init__(self, page):
        self.page = page
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(("pages.get", kwargs))
        return _Request(self.page)

    def getThumbnail(self, **kwargs):  # Google discovery method name is camelCase.
        self.calls.append(("pages.getThumbnail", kwargs))
        return _Request({"contentUrl": "https://temporary.invalid/thumbnail"})


class _Presentations:
    def __init__(self, pages):
        self._pages = pages

    def pages(self):
        return self._pages


class _Service:
    def __init__(self, page):
        self.pages_resource = _Pages(page)

    def presentations(self):
        return _Presentations(self.pages_resource)


class _Response:
    content = b"\x89PNG\r\nfixture"

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout):
        self.urls.append((url, timeout))
        return _Response()


def test_capture_calls_page_get_and_thumbnail_never_whole_deck(
    spanish_page, spanish_course
):
    service = _Service(spanish_page)
    session = _Session()
    source = GoogleSlidesSource(
        spanish_course.source,
        credentials=None,
        service=service,
        session=session,
    )

    capture = source.capture(include_image=True)

    names = [name for name, _ in service.pages_resource.calls]
    assert names == ["pages.get", "pages.getThumbnail"]
    get_args = service.pages_resource.calls[0][1]
    thumbnail_args = service.pages_resource.calls[1][1]
    assert get_args["pageObjectId"] == "g8596fffd0c_4_6"
    assert thumbnail_args["thumbnailProperties_mimeType"] == "PNG"
    assert thumbnail_args["thumbnailProperties_thumbnailSize"] == "LARGE"
    assert capture.image_bytes.startswith(b"\x89PNG")
    assert session.urls == [("https://temporary.invalid/thumbnail", 30)]


def test_add_image_does_not_reread_page(spanish_page, spanish_course):
    service = _Service(spanish_page)
    source = GoogleSlidesSource(
        spanish_course.source,
        credentials=None,
        service=service,
        session=_Session(),
    )
    capture = source.capture(include_image=False)
    with_image = source.add_image(capture)
    assert [name for name, _ in service.pages_resource.calls] == [
        "pages.get",
        "pages.getThumbnail",
    ]
    assert with_image.page_hash == capture.page_hash

