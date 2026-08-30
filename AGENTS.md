# AGENTS.md

## Start here

Canvas Task Sync is a local-first pipeline that turns Canvas/course-agenda evidence into deterministic Google Tasks. Keep these responsibilities separate:

`source capture -> Gemini semantic extraction -> deterministic scheduling -> stable identity -> reconciliation plan -> revalidation -> Google Tasks apply -> local state`

The web app and Chrome extension are control/acquisition layers around that same pipeline; they are not alternate sync implementations. The default CLI path is dry-run. Writes require explicit apply/auto-apply paths and must pass preview revalidation.

## Repository roadmap

### Core Python: `src/canvas_task_sync/`

- `sync_service.py` — main orchestration. Builds immutable `PreparedPlan`s, hashes config/source/remote state, revalidates before writes, then delegates apply. Start here for end-to-end sync flow; keep domain policy in the modules below instead of growing this file.
- `gemini.py` — Gemini prompt/schema, model fallback, extraction modes, evidence reconciliation, and extraction quality checks. Gemini determines semantic meaning only; it does **not** own dates, IDs, or final sync decisions.
- `scheduling.py` — authoritative deadline/date policy and conversion from extracted candidates to deterministic draft tasks. Date bugs belong here.
- `identity.py` — durable logical IDs and conservative matching across source edits/reordering. Never base primary identity on Gemini wording.
- `planner.py` — desired-vs-Google reconciliation and action selection. Owns create/update/unchanged/uncertain/source-missing/remote-missing behavior.
- `google_tasks.py` — Google Tasks transport only. Updates deliberately preserve completion/user-controlled fields.
- `state.py` — durable sync identity mappings + extraction cache in `.canvas-task-sync/state.sqlite3`.
- `managed_notes.py` — legacy managed-note recovery/cleanup and preservation of user-authored task notes.
- `models.py` — shared Pydantic domain models/enums used by extraction, scheduling, planning, and state.

### Source acquisition: `src/canvas_task_sync/sources/`

- `__init__.py` — source registration and `CourseAgendaSource`, which owns Canvas-first vs configured-fallback selection.
- `canvas.py` — Canvas API agenda discovery, same-origin link following, week selection, assignment/source context, canonical capture hashing.
- `google_slides.py` — target-page Slides API capture and optional thumbnail retrieval.
- `browser_connector.py` — adapts the in-memory Chrome capture envelope to `SourceCapture`.
- `base.py` — `SourceAdapter` contract.

New formats should do acquisition only, register through `create_source_adapter`, then reuse Gemini, scheduling, identity, and planner unchanged.

### Configuration, CLI, and runtime

- `configuration.py` — validated YAML schema and course/source settings.
- `configuration_service.py` — safe web-driven config/credential writes; preserves YAML comments and creates `.bak` files.
- `config/courses.yaml` — live user configuration. Do not hard-code behavior around its current courses and do not edit it unless the task actually changes user configuration.
- `config/courses.yaml.bak` — automatic backup; ignore/edit only for explicit recovery work.
- `cli.py` / `app.py` — CLI parsing and thin command entry points (`auth`, `doctor`, `sync`, `web`).
- `auth.py` — Google OAuth scopes/token refresh and atomic token persistence.
- `health.py` — connection/source/task-list diagnostics.
- `server.py` / `windows_startup.py` / `scripts/` — loopback servers and Windows scheduled-startup integration.
- `week.py` — week-selection helpers.

### Web control center backend

- `web_app.py` — FastAPI composition and `/api/v1` routes. This is also the local security boundary: loopback host checks, CSRF for normal mutations, and a separate extension pairing token.
- `run_manager.py` — queued preview/apply/health runs and schedules; delegates actual sync work to `SyncService`.
- `control_store.py` — operational SQLite (`control.sqlite3`): run history/events, schedules, occurrences, and control settings. It is not sync identity state.
- `web_models.py` — Pydantic request/response models for the local API.
- `tracked_tasks.py` — read-only canonical task feed that merges sync state with live Google completion. `completed=false` is intentionally strict: only live `needsAction` counts as unfinished. School Dashboard consumes this contract.
- `redaction.py` — secret/binary sanitization before logs/support data are persisted or returned.
- `browser_capture.py` — bounded, validated, **memory-only** browser capture broker and automatic capture-request queue.
- `simple_web_app.py` / `web_constants.py` — secondary simple UI server and shared loopback ports.

### React frontend: `web/`

- `src/App.tsx` — routes: Overview, Runs/Run Detail, Courses, Schedules, Diagnostics, Settings.
- `src/lib/api.ts` — browser API/CSRF boundary and SWR helpers.
- `src/types.ts` — TypeScript mirror of backend API shapes; update with `web_models.py`/routes when contracts change.
- `src/pages/` — full control-center screens.
- `src/components/` — shared shell/context/UI.
- `src/simple/` — lightweight UI served separately on port 8791.
- `src/styles.css` / `src/simple/simple.css` — bulk styling.
- `vite.config.ts` builds **directly into** `src/canvas_task_sync/web_dist/`.

`src/canvas_task_sync/web_dist/` is committed generated Vite output. Never inspect it to understand frontend behavior and never hand-edit it; change `web/` and rebuild.

### Chrome source connector: `extension/`

- `src/background.js` — automatic/manual capture orchestration, temporary tabs, screenshot throttling, queue processing.
- `src/content/registry.js` — browser adapter contract/registration.
- `src/content/runtime.js` — message dispatch to the active adapter.
- `src/content/adapters/{slides,docs,sheets,readable-page}.js` — format-specific acquisition only.
- `src/lib/acquisition.js` — screenshot/text/fallback mode semantics.
- `src/lib/connection.js` — paired localhost API calls.
- `src/lib/schema.js` — normalized capture envelope.
- `src/lib/config.js` — local connector settings; only pairing/acquisition configuration is stored.
- `src/local-bridge.js` — wakes automatic capture from the local web UI.
- `popup.*` / `options.*` — extension UI.
- `tests/index.test.mjs` — extension behavior/security tests.

`extension/dist/` is generated by `npm run build` and ignored. The extension must not authenticate to Google itself, read cookies/credentials, persist captured school content, call Google Tasks, or implement sync policy.

### Tests and reference material

- `tests/` — Python tests are organized by production module: sources/extraction, scheduling, identity, planner, Google Tasks/state, orchestration, web runtime/API, CLI/auth/startup.
- `tests/fixtures/` — sanitized extraction fixtures; prefer these over live services.
- `web/src/**/*.test.ts(x)` — React/API UI tests.
- `design/reference/` and `design/implementation/` — screenshots for visual comparison only; not runtime code.
- `canvas_front_page.html`, `canvas_agenda.txt`, `canvas_extraction_probe.py` — historical/manual Canvas extraction probe artifacts. They are not imported by the package; do not start there for production behavior.

## Invariants and security

- Preserve dry-run safety. Preparing/previewing must not create writable sync state or mutate Google Tasks.
- Never auto-delete a Google Task, recreate a mapped task that was deleted remotely, or claim an ambiguous unmanaged collision.
- Apply only the reviewed/prepared state: config, source capture, and relevant remote tasks are re-hashed/revalidated before writes.
- Deadlines and identity are deterministic application policy. If Gemini output conflicts with exact evidence, keep the item uncertain rather than guessing.
- Preserve user completion state and user-authored notes. Google Tasks writes should touch only managed title/notes/due fields.
- Canvas bearer tokens stay local and may only be sent to the configured same-origin Canvas API.
- Browser captures remain bounded and memory-only; do not persist screenshots/page content or accept credential-like metadata.
- Keep the web server loopback-only. Do not weaken host/origin/CSRF/extension-token checks.
- Persisted run/support data must pass the redaction layer.
- If `/api/v1/tasks` or browser-resource endpoints change, check the School Dashboard `server/task-sync.ts` integration too.

## Avoid wasting context

Unless the task specifically needs them, skip:

- `src/canvas_task_sync/web_dist/` — generated frontend bundle/source maps.
- `web/package-lock.json` — dependency lock; inspect only for dependency work.
- `design/` — image references only.
- `config/courses.yaml.bak` — stale backup, not source of truth.
- root Canvas sample/probe files — debugging/reference only.
- ignored `.canvas-task-sync/`, `.env`, credentials/token files, caches, venvs, and `extension/dist/` — runtime/generated state.

Prefer targeted files/tests from the map above over repository-wide searches. Search generated bundles only when debugging packaging/build output.

## Build, style, and tests

Python is 3.11+, typed, Pydantic-heavy, and linted with Ruff (100-char line length). Install once with:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the narrowest relevant tests first. Useful groups:

```powershell
python -m pytest tests/test_scheduling.py tests/test_identity.py tests/test_planner.py
python -m pytest tests/test_sync_service.py
python -m pytest tests/test_web_runtime.py tests/test_tasks_api.py
```

Before finishing a broad Python/backend change:

```powershell
python -m pytest
python -m ruff check src tests
```

For React changes (`web/`):

```powershell
Set-Location web
npm test
npm run typecheck
npm run lint
npm run build
```

The build rewrites committed `src/canvas_task_sync/web_dist/`; include the generated changes when the frontend source changed.

For extension changes:

```powershell
Set-Location extension
npm run check
```

Do not use live Google Tasks writes as tests. Existing tests use fakes/sanitized fixtures for mutation-sensitive behavior.