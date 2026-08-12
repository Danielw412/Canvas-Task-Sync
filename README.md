# Canvas Task Sync

Canvas Task Sync turns visually structured course agendas into deterministic Google Tasks. The
first configured source is the Honors Spanish IV Google Slide, but source capture, Gemini
interpretation, deadline policy, identity, and Google reconciliation are separate modules so Canvas
and other agenda formats can be added without rewriting the sync logic.

The default command is always a dry run. Nothing is created, updated, or deleted unless `--apply`
is present.

## What it does

- Reads only the configured Google Slides page with `presentations.pages.get`.
- Downloads a `LARGE` PNG with `presentations.pages.getThumbnail` when the selected mode needs it.
  The temporary URL is consumed immediately and is never logged or stored.
- Sends an image, anchor-labeled exact page text, or both to Gemini using a structured response.
- Uses application code—not Gemini—to calculate next-class and same-row deadlines.
- Reconciles concise `[SPANISH] …` tasks against the existing `School` list.
- Preserves exact Spanish evidence and useful assignment details in a delimited managed notes block.
- Preserves completion state and notes written outside that block.
- Uses UUIDv5 source identities, managed markers, and ignored SQLite state to make repeated runs safe.
- Never automatically deletes a task, recreates a remotely deleted task, or claims an unmanaged
  title/date collision.

No whole-deck `presentations.get` request or Drive-wide OAuth scope is used. See the official
[target-page API](https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations.pages/get),
[thumbnail API](https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations.pages/getThumbnail),
and [Gemini image input guide](https://ai.google.dev/gemini-api/docs/image-understanding).

## Setup

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Keep the existing `GEMINI_API_KEY` in `.env`. In a Google Cloud project, enable the Google Slides
API and Google Tasks API, create an OAuth Desktop application, and place its downloaded client
configuration at `credentials.json`. Then authorize the two requested scopes:

```powershell
canvas-task-sync auth
canvas-task-sync doctor
```

The scopes are exactly:

- `https://www.googleapis.com/auth/presentations.readonly`
- `https://www.googleapis.com/auth/tasks`

If an older `token.json` has only Tasks permission, `auth` requests consent again and safely replaces
it. `.env`, `credentials.json`, `token.json`, `.canvas-task-sync/state.sqlite3`, virtual environments,
and caches are ignored by Git.

## Course configuration

Courses live in `config/courses.yaml`; URLs and course metadata are not hardcoded in the importer.
The Spanish configuration is:

```yaml
courses:
  spanish:
    name: Honors Spanish IV
    prefix: SPANISH
    task_list: School
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    source:
      type: google_slides
      url: https://docs.google.com/presentation/d/.../edit
      page_id: g8596fffd0c_4_6
      extraction:
        mode: hybrid
        thumbnail_size: large
        assignments_default_due: next_class
        same_day_action_kinds: [bring, present, submit]
```

Extraction modes:

- `image`: the PNG is the only agenda content sent to Gemini; target-page text is still used locally
  for identity and evidence validation.
- `text`: Gemini receives only anchor-labeled target-page text.
- `hybrid`: Gemini receives both. The image controls visual row/column association and exact page
  text controls spelling, accents, numbers, ranges, and names.
- `auto`: tries image first, then retries hybrid if OCR/evidence, anchor, confidence, or row/date
  context is weak.

An unreconciled image/text disagreement is reported as uncertain and is never synced. The diagnostic
`--extraction-mode` flag can temporarily override one course without changing its YAML.

## Usage

```powershell
# Read source, call Gemini, reconcile all pages of School, and print zero-write actions.
canvas-task-sync sync --course spanish

# Same plan as JSON.
canvas-task-sync sync --course spanish --json

# Perform only the displayed creates and managed-field updates.
canvas-task-sync sync --course spanish --apply

# Explicitly permit historical creates/updates.
canvas-task-sync sync --course spanish --apply --include-past

# Test the historical fixture as the week beginning August 10, 2026; never compatible with --apply.
canvas-task-sync sync --course spanish --test-rebase-week 2026-08-10

# Compare the two visual strategies without editing YAML.
canvas-task-sync sync --course spanish --extraction-mode image
canvas-task-sync sync --course spanish --extraction-mode hybrid
```

### Local web control center

Start the single-user control center on the loopback interface:

```powershell
# Opens http://127.0.0.1:8787 in the default browser.
canvas-task-sync web

# Choose another loopback port or leave browser opening to yourself.
canvas-task-sync web --port 8790 --no-open
```

The web app provides an overview, live run progress, immutable preview plans, guarded apply,
course and schedule management, connection setup, structured diagnostics, and sanitized support
bundles. Schedules run only while this process is running. Previewing writes sanitized operational
history to `.canvas-task-sync/control.sqlite3`, but it never changes Google Tasks or
`.canvas-task-sync/state.sqlite3`. Operational history is retained for 90 days by default.

The server always binds to `127.0.0.1`; it validates same-origin requests and requires a
per-process CSRF token for every mutation. It is intended as a local control center, not a remotely
hosted multi-user service.

Every report has `CREATE`, `UPDATE`, `UNCHANGED`, and `UNCERTAIN` sections plus ignored, remote
missing, source missing, and historical-blocked sections. `--apply` never deletes a task. A task that
disappeared remotely or from the source requires human review.

The canonical page hash excludes temporary content URLs and revision metadata but includes meaningful
text, table structure, geometry, and styles. A successful apply stores its structured extraction in
SQLite. A later unchanged run reuses that extraction and avoids the expensive thumbnail request.
Dry runs never create or modify the SQLite file.

## Deadline and identity policy

The target-page date heading and row labels are parsed deterministically:

- Assignments-column work defaults to the next configured class day.
- `bring`, `present`, and `submit` use the end date of their agenda row.
- A compound `W-Th` row ends Thursday, so next class is Friday.
- A repeated Monday after Friday resolves to the following week.
- Explicit dates are accepted only when the exact source evidence supports them.
- Ordinary class activities, holidays, learning targets, and teacher narration are ignored.

Initial logical IDs use course, presentation/page, page element, table row/column, and deterministic
in-cell order—not model wording. Stored IDs survive wording and deadline edits. Reordered actions use
unique normalized-token matching only at a score of at least `0.75` with a `0.15` margin; ambiguous
duplicates are never guessed. Managed note metadata can recover mappings if SQLite is lost or a
process stops after Google creation.

## Adding sources

An adapter implements `SourceAdapter.capture()` and returns a `SourceCapture` containing a stable
source key, canonical hash, exact transcript, anchored blocks, and optional image bytes. Image-capable
adapters may implement `add_image()` so the application can hash/check its cache before fetching an
expensive image. Register the source at `canvas_task_sync.sources.create_source_adapter` and add its
validated configuration model.

Canvas pages/assignments, Google Docs, Google Sheets, PDFs, local images, and general URLs should do
format-specific acquisition only. They then reuse `GeminiExtractor`, `build_draft_tasks`,
`SyncPlanner`, managed markers, and Google Tasks unchanged.

## Tests

```powershell
python -m pytest
python -m ruff check src tests
```

The test fixtures contain sanitized target-page JSON, a text-safe base64 PNG transport fixture, and
structured Spanish Gemini output. Tests cover all extraction modes, exact Unicode evidence, auto
fallback, the six expected rebased tasks, date safety, duplicate VHL identities, row/wording/deadline
changes, marker recovery, user-note preservation, pagination, collisions, disappearance, and dry-run
safety.
