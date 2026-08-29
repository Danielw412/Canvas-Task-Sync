# Canvas Task Sync

Canvas Task Sync turns Canvas and visually structured course agendas into deterministic Google Tasks.
The first configured fallback source is the Honors Spanish IV Google Slide. An optional Chrome extension can
also capture selected Google Slides, Docs, and Sheets through the user's existing browser session.
Source capture, Gemini
interpretation, deadline policy, identity, and Google reconciliation are separate modules so Canvas
and other agenda formats can be added without rewriting the sync logic.

The default command is always a dry run. Nothing is created, updated, or deleted unless `--apply`
is present.

## What it does

- Searches Canvas course modules, pages, assignments, the syllabus, and home page first.
- Reads only the configured Google Slides fallback page with `presentations.pages.get`.
- Downloads a `LARGE` PNG with `presentations.pages.getThumbnail` when the selected mode needs it.
  The temporary URL is consumed immediately and is never logged or stored.
- Sends an image, anchor-labeled exact page text, or both to Gemini using a structured response.
- Uses application code—not Gemini—to calculate next-class and same-row deadlines.
- Reconciles concise `[SPANISH] …` assignments against `School` and assessments against `Tests`.
- Keeps sync identities and source evidence in ignored local SQLite state instead of task notes.
- Preserves completion state and user-authored notes while removing legacy managed note blocks.
- Uses UUIDv5 source identities, same-class history, and local state to make repeated runs safe.
- Lets each web preview target Previous Week, This Week (default), or Next Week.
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
    assessment_task_list: Tests
    ai_instructions: Keep workbook and online practice as separate tasks.
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

A Canvas-only course needs no fallback URL:

```yaml
courses:
  canvas12604:
    name: AP English 4 Literature & Composition
    prefix: ENGLISH
    task_list: School
    assessment_task_list: Tests
    timezone: America/New_York
    meeting_days: [mon, tue, wed, thu, fri]
    canvas_course_id: '12604'
    source:
      type: none
      extraction:
        mode: text
        assignments_default_due: next_class
        same_day_action_kinds: [bring, present, submit]
```

`canvas_course_id` enables API-first agenda discovery. The app reads `CANVAS_BASE_URL` and
`CANVAS_TOKEN` from `.env`; `source` can be a direct API fallback, a browser fallback, or
`type: none` for Canvas-only acquisition. The example configuration uses Canvas course IDs
`12604`, `11126`, `11517`, and `12506`. The run UI can force Canvas-only or the configured fallback
when troubleshooting.

`ai_instructions` is optional free-form guidance applied only to that course's Gemini extraction.
For example, one course can say `Do not create homework tasks for reading assignments` without
changing how any other course is interpreted. Editing the guidance invalidates that course's
extraction cache.

Week matching is start-date based. A target week beginning August 17 accepts headings such as
`Week of August 17`, `August 17-21`, and `Week of August 17 to August 20`; an incorrect range end
does not disqualify an otherwise strong match. Internal Canvas page and assignment links are
followed through same-origin API endpoints, and the bearer token is never sent to external links.

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
# Read source, call Gemini, reconcile School and Tests, and print zero-write actions.
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
# Opens http://127.0.0.1:8790 in the default browser.
canvas-task-sync web

# Choose other loopback ports or leave browser opening to yourself.
canvas-task-sync web --port 8890 --simple-port 8891 --no-open
```

To start the control center automatically when you sign in to Windows and add a desktop website
shortcut, run this once from the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-startup.ps1
```

The installer registers one hidden per-user scheduled task, starts it immediately, and verifies both
the full control center at `http://127.0.0.1:8790/` and the minimal sync console at
`http://127.0.0.1:8791/`. Both desktop shortcuts use the same authoritative sync queue, scheduler,
and databases. The servers run without a terminal or browser window and do not expose either site
to the local network. To remove the scheduled task and both shortcuts later:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\remove-windows-startup.ps1
```

The full web app provides an overview, live run progress, immutable preview plans, guarded apply,
course and schedule management, connection setup, structured diagnostics, and sanitized support
bundles. Its normal **Sync all courses** and **Sync selected course** actions automatically apply the
safe subset; Advanced Preview and preview-mode schedules retain the approval workflow. The minimal
UI provides the two sync actions plus live JSON-lines output and historical operation replay.
Schedules run only while this process is running. Operational history is written to
`.canvas-task-sync/control.sqlite3` and retained for 90 days by default.

The server always binds to `127.0.0.1`; it validates same-origin requests and requires a
per-process CSRF token for every mutation. It is intended as a local control center, not a remotely
hosted multi-user service.

### Local JSON API

The control center exposes a read-only API under `http://127.0.0.1:8790/api/v1` for local companion
apps such as School Dashboard:

```text
GET /api/v1/bootstrap
GET /api/v1/overview
GET /api/v1/courses
GET /api/v1/runs
GET /api/v1/runs/{run_id}
GET /api/v1/tasks?completed=false
GET /api/v1/tasks/{logical_id}
```

The task routes expose Task Sync's durable logical identity together with course, title, due date,
completion state, Google Task identity, source provenance, and Canvas identifiers/URLs when known.
The `completed=false` feed is strict: it returns only tasks whose live Google status is
`needsAction`. Unknown, missing, or unavailable Google status is excluded so a checked-off task can
never reappear as unfinished. A detail request still reports `completed: null` and an explanatory
`completion_status` when the live status cannot be established.
Restart the local control center after updating the repository so the new routes are registered.

Every report has `CREATE`, `UPDATE`, `UNCHANGED`, and `UNCERTAIN` sections plus ignored, remote
missing, source missing, and historical-blocked sections. `--apply` never deletes a task. A task that
disappeared remotely or from the source requires human review. When only the due date is uncertain,
the task is created without a date and with the standalone note `Due date uncertain`; a later dated
source updates that same task and removes the marker.

The canonical page hash excludes temporary content URLs and revision metadata but includes meaningful
text, table structure, geometry, and styles. A successful apply stores its structured extraction in
SQLite. A later unchanged run reuses that extraction and avoids the expensive thumbnail request.
Dry runs never create or modify the SQLite file.

## Chrome source connector

The Manifest V3 extension in `extension/` is an acquisition client, not a second sync system. It
captures source content, sends a normalized capture to the loopback-only control center, and then the
existing `GeminiExtractor`, date policy, identity, planner, dry-run, and Google Tasks code run exactly
as they do for the direct Slides API source. It does not contain Canvas integration.

Build and load it:

```powershell
Set-Location .\extension
npm test
npm run build
```

1. Start `canvas-task-sync web` and open **Settings → Connections → Chrome source connector**.
2. In Chrome, open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select
   the displayed `extension\dist` directory.
3. Open the extension's settings, paste the local server address and pairing token, then choose
   **Test connection**.
4. Configure a course with `source.type: browser`. Starting its sync now queues the extension,
   which opens the Google file, captures it with the configured mode, closes only the temporary tab,
   and lets the existing sync continue. The popup remains available for one-off manual captures
   and fine-grained selection.
5. Use **Sync all courses** to start all enabled courses together. Non-browser capture and Gemini
   processing run in parallel; extension acquisition is deliberately FIFO to keep tab activation and
   screenshot capture reliable. Safe changes apply automatically; conflicting items remain available
   for review, and automated tests never create real Google Tasks.

Example browser-backed course source:

```yaml
source:
  type: browser
  url: https://docs.google.com/spreadsheets/d/18ngIjJOcG6UC5sig7NEv_l209WZpdrRLd8JtBIo1Img/edit?gid=0
  source_format: google_sheets # or auto, google_slides, google_docs
  freshness_seconds: 900
  selection:                  # optional; empty means use the extension's selection
    slide_ids: []
    section_ids: []
    sheets:
      - sheet_id: "0"
        sheet_name: Agenda
        range_a1: A1:F60
  extraction:
    mode: auto
    assignments_default_due: next_class
    same_day_action_kinds: [bring, present, submit]
```

Extension acquisition modes are independent from Gemini extraction modes:

- **Screenshot only** and **text only** acquire only that content type.
- **Screenshot + text** requires both methods to succeed.
- **Prefer screenshot** and **prefer text** try the selected method first and clearly report when the
  other method was used as a fallback.
- One global default can be overridden for Slides, Docs, or Sheets in the extension settings. The
  popup can also override the mode for one capture.

The extension requests only `activeTab`, `alarms`, `scripting`, `storage`, loopback HTTP access, and
`https://docs.google.com/*`. Persistent Docs host access is needed for background-requested capture
and authenticated Docs exports; it authorizes only content the current Chrome session can already
display. The extension never requests cookie access, reads credential stores, or sends school login
credentials. Pairing configuration is stored in extension local storage;
screenshots and school content are not. The app validates type, URL, IDs, sizes, MIME signatures, and
credential-like metadata, keeps at most eight captures in memory for 15 minutes, and clears them on
server shutdown. Pairing tokens can be rotated and captures cleared immediately from Settings.
Chrome additionally requires `<all_urls>` for a background tab screenshot. This is declared as an
optional host permission and requested only when the user clicks **Enable automatic screenshots**;
automatic text capture and all other connector features work without it.

See [extension/README.md](extension/README.md) for format behavior, selection details, error messages,
and troubleshooting.

## Deadline and identity policy

The target-page date heading and row labels are parsed deterministically:

- Assignments-column work defaults to the next configured class day.
- Homework with no stated date also defaults to the next configured school day.
- `bring`, `present`, and `submit` use the end date of their agenda row.
- A weekday explicitly stated in the source (for example, "Bring the FRQ Thursday") overrides the
  ordinary same-row action rule.
- A compound `W-Th` row ends Thursday, so next class is Friday.
- A repeated Monday after Friday resolves to the following week.
- Explicit dates are accepted only when the exact source evidence supports them.
- Ordinary class activities, holidays, learning targets, and teacher narration are ignored.

Gemini receives unfinished tasks for the same course plus completed tasks from the preceding 14
days across the assignment and assessment lists as duplicate context. It is instructed to reuse one
concise title while deterministic local identity and deduplication remain authoritative. The model chain is
`gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, then `gemini-3.5-flash-lite`; quota,
rate-limit, unavailable-model, and unsupported-model errors advance to the next model. Each course
can choose a low, medium, or high Gemini reasoning budget in **Courses → Source & extraction**.

Initial logical IDs use course, presentation/page, page element, table row/column, and deterministic
in-cell order—not model wording. Stored IDs survive wording and deadline edits. Reordered actions use
unique normalized-token matching only at a score of at least `0.75` with a `0.15` margin; ambiguous
duplicates are never guessed. Legacy managed-note metadata is recovered into SQLite once and then
removed from Google Tasks; subsequent syncs use local mappings and same-class title/date collisions.
Each synced task gets a managed AI-generated description section. An exact Canvas assignment link is
included when it can be associated unambiguously; agenda and course-page links are not used as
fallbacks. Later syncs refresh only this generated section and preserve user-written text outside it.

## Adding sources

An adapter implements `SourceAdapter.capture()` and returns a `SourceCapture` containing a stable
source key, canonical hash, exact transcript, anchored blocks, and optional image bytes. Image-capable
adapters may implement `add_image()` so the application can hash/check its cache before fetching an
expensive image. Register the source at `canvas_task_sync.sources.create_source_adapter` and add its
validated configuration model.

Canvas pages/assignments, PDFs, local images, and general URLs should do format-specific acquisition
only. Browser formats register an extension adapter and reuse the generic local browser source
adapter. They then reuse `GeminiExtractor`, `build_draft_tasks`,
`SyncPlanner`, private local identity state, and Google Tasks reconciliation unchanged.

## Tests

```powershell
python -m pytest
python -m ruff check src tests
Set-Location extension; npm test; npm run build
```

The test fixtures contain sanitized target-page JSON, a text-safe base64 PNG transport fixture, and
structured Spanish Gemini output. Tests cover all extraction modes, exact Unicode evidence, auto
fallback, the six expected rebased tasks, date safety, duplicate VHL identities, row/wording/deadline
changes, automatic extension queuing, Docs/Slides/Sheets metadata, model failover, recent-assignment
context, marker recovery, user-note preservation, pagination, collisions, disappearance, parallel
run-all dispatch, and dry-run safety.
