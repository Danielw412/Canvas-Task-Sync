# AGENTS.md

## Start here

Canvas Task Sync is a local-first pipeline that turns Canvas/course-agenda evidence into deterministic Google Tasks. Keep these responsibilities separate:

`source capture -> Gemini semantic extraction -> deterministic scheduling -> stable identity -> reconciliation plan -> revalidation -> Google Tasks apply -> local state`

The web app and Chrome extension are control/acquisition layers around that same pipeline; they are not alternate sync implementations. The default CLI path is dry-run. Writes require explicit apply/auto-apply paths and must pass preview revalidation.

## Repository roadmap

### Core Python: `src/canvas_task_sync/`

- `sync_service.py` — main orchestration. Builds immutable `PreparedPlan`s, hashes config/source/remote state, revalidates before writes, then delegates apply. Start here for end-to-end sync flow; keep domain policy in the modules below instead of growing this file.
- `gemini.py` — Gemini prompt/schema, model fallback, extraction modes, evidence reconciliation, and extraction quality checks. Gemini determines semantic meaning only; it does **not** own dates, IDs, or final sync decisions.
- `scheduling.py` — authoritative deadline/date policy and conversion from extracted candidates to deterministic draft tasks. Date bugs belong here. A row may state its own dates (`metadata.row_dates`, as a daily slide's heading does) instead of being walked from the start of the week, and the due date of the one Canvas assignment linked from a task's evidence fills in timing the source leaves unstated — never for an assessment, and never over a stated date or weekday.
- `identity.py` — durable logical IDs and conservative matching across source edits/reordering. Never base primary identity on Gemini wording. Evidence is compared with dates stripped, and differing item numbers ("Unit 2" vs "Unit 3") never match.
- `planner.py` — desired-vs-Google reconciliation and action selection. Owns create/update/unchanged/uncertain/source-missing/remote-missing behavior, and carry-over: each Canvas week is its own source, so an open task from an earlier week's agenda is adopted (not duplicated) when the item reappears.
- `google_tasks.py` — Google Tasks transport only. Updates deliberately preserve completion/user-controlled fields.
- `state.py` — durable sync identity mappings + extraction cache in `.canvas-task-sync/state.sqlite3`.
- `memory.py` — `release_memory()`: collect, then hand glibc's freed pages back. Called when the
  run queue drains and on the idle loop, because the backend is long-lived and a run's peak would
  otherwise become its permanent size.
- `managed_notes.py` — legacy managed-note recovery/cleanup and preservation of user-authored task notes.
- `models.py` — shared Pydantic domain models/enums used by extraction, scheduling, planning, and state.

### Source acquisition: `src/canvas_task_sync/sources/`

- `__init__.py` — source registration and `CourseAgendaSource`, which owns Canvas-first vs configured-fallback selection.
- `canvas.py` — Canvas API agenda discovery, same-origin link following, week selection, assignment/source context, canonical capture hashing. It also ranks the published Slides decks a course embeds, reads the most agenda-like one, and lets a daily deck compete with Canvas pages for the week; slide links to module items resolve through the module listing to the Canvas assignment (with its `due_at`).
- `published_slides.py` — reads a "Publish to web" Slides deck (`/presentation/d/e/2PACX-.../pub`) from its public viewer page: per-slide SVG, text boxes, links, and class-day headings. Acquisition only, and fetched with a separate credential-free session — the Canvas bearer token must never reach `docs.google.com`.
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
- `server.py` — binds the loopback sockets and runs one uvicorn server for both dashboards.
  `PortRouter` sends each connection to the backend app or the simple app, based on the port it
  arrived on.
- `windows_startup.py` / `scripts/` — Windows scheduled-startup integration, in two modes. Without
  `--ssh-target`, it runs the whole application on this machine. With `--ssh-target`, the server
  hosts everything, and this process only supervises `ssh -N -L 8890:…:8790 -L 8891:…:8891`. The
  second mode stays in memory all day, so it must import nothing beyond the standard library and
  `web_constants`, and `tests/test_windows_startup.py` checks that.
- `deploy/` — systemd user service and installer for the Linux server that hosts the backend and
  both dashboards. Lingering starts the service at boot.
- `week.py` — week-selection helpers.

### Web control center backend

- `web_app.py` — FastAPI composition and `/api/v1` routes. This is also the local security boundary: loopback host checks, CSRF for normal mutations, and a separate extension pairing token.
- `run_manager.py` — the run queue, schedules, and the lifetime of the sync worker process. Imports
  nothing from the pipeline on purpose: that is what keeps the web process small.
- `run_executor.py` — what a run actually does. Hosted by the worker process, and in-process only for
  a course whose source is a Chrome capture. One implementation, two hosts.
- `worker.py` — the worker entry point. Reads `{"run_id": N}` lines on stdin, replies on stdout, and
  exits on EOF; the web process decides when that happens. Never print to stdout here.
- `control_store.py` — operational SQLite (`control.sqlite3`): run history/events, schedules, occurrences, and control settings. It is not sync identity state.
- `web_models.py` — Pydantic request/response models for the local API.
- `tracked_tasks.py` — read-only canonical task feed that merges sync state with live Google completion. `completed=false` is intentionally strict: only live `needsAction` counts as unfinished. School Dashboard consumes this contract.
- `redaction.py` — secret/binary sanitization before logs/support data are persisted or returned.
- `browser_capture.py` — bounded, validated, **memory-only** browser capture broker and automatic capture-request queue.
- `simple_web_app.py` / `web_constants.py` — the secondary simple UI, the shared loopback ports,
  and the public-origin helpers. `CANVAS_TASK_SYNC_PUBLIC_ORIGIN` is the address the browser types
  when an SSH tunnel maps it onto a different backend port. The Google redirect uses it, the
  simple UI's `api_base` uses it, the extension pairing URL uses it, and the backend's
  host/origin guards accept its port in addition to the bound port.
- `google_oauth.py` — browser-delegated OAuth for a headless backend. The backend mints the consent
  URL, the person's own browser completes consent, and the code returns through the dashboard origin.
  `auth.load_google_credentials` still owns non-interactive refresh.

### React frontend: `web/`

- `src/App.tsx` — routes: Overview, Runs/Run Detail, Courses, Schedules, Diagnostics, Settings.
- `src/lib/api.ts` — browser API/CSRF boundary and SWR helpers.
- `src/types.ts` — TypeScript mirror of backend API shapes; update with `web_models.py`/routes when contracts change.
- `src/pages/` — full control-center screens.
- `src/components/` — shared shell/context/UI.
- `src/simple/` — lightweight UI served separately on port 8891.
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
  `test_server.py` covers server hosting: the tunneled public origin, and both dashboards served
  from one server on real sockets.
- `tests/fixtures/` — sanitized extraction fixtures; prefer these over live services.
- `web/src/**/*.test.ts(x)` — React/API UI tests.
- `design/reference/` and `design/implementation/` — screenshots for visual comparison only; not runtime code.
- `canvas_front_page.html`, `canvas_agenda.txt`, `canvas_extraction_probe.py` — historical/manual Canvas extraction probe artifacts. They are not imported by the package; do not start there for production behavior.

## Invariants and security

- Preserve dry-run safety. Preparing/previewing must not create writable sync state or mutate Google Tasks.
- Never auto-delete a Google Task, recreate a mapped task that was deleted remotely, or claim an ambiguous unmanaged collision.
- Apply only the reviewed/prepared state: config, source capture, and relevant remote tasks are re-hashed/revalidated before writes.
- Deadlines and identity are deterministic application policy. If Gemini output conflicts with exact evidence, keep the item uncertain rather than guessing.
- An unchanged source page reuses its cached extraction. Keep volatile Google Tasks context out of the extraction cache key; it changes after every write and made due dates flip between runs.
- Preserve user completion state and user-authored notes. Google Tasks writes should touch only managed title/notes/due fields.
- Canvas bearer tokens stay local and may only be sent to the configured same-origin Canvas API. A published Slides deck is public: fetch it with its own credential-free session, never the Canvas session and never Google credentials.
- Browser captures remain bounded and memory-only; do not persist screenshots/page content or accept credential-like metadata.
- Keep the web server loopback-only. Do not weaken host/origin/CSRF/extension-token checks.
- Persisted run/support data must pass the redaction layer.
- Keep `google.genai`, `googleapiclient`, `google_auth_oauthlib`, and `google.auth` out of module
  scope, and keep the pipeline out of the web process entirely. Both are enforced by
  `tests/test_memory_footprint.py`, which fails if an import leaks back to module load.
- Run state, progress events, and cancellation cross the process boundary through `control.sqlite3`,
  never through the worker pipe. Keep it that way: the pipe carries only "this run finished", so a
  worker that dies cannot lose a run's recorded outcome.
- In a split deployment the server hosts the backend and both dashboards and is authoritative. The
  laptop only forwards loopback ports over SSH. Do not add an HTTP proxy, web server, sync logic,
  credentials, or SQLite state back to the laptop side. Do not copy `token.json` or either SQLite
  database to the laptop.
- The only thing that widens the backend's loopback guards is the configured public origin, which
  `normalize_loopback_origin` restricts to loopback. Never accept a non-loopback `Host` or `Origin`.
  Do not bind anything but `127.0.0.1`. Remote access goes through SSH.
- Google authorization is delegated to the person's browser, never `run_local_server`, on any backend
  that may be headless. Reject a mismatched, reused, or expired OAuth `state`, and never overwrite a
  working `token.json` with credentials that lack a refresh token or a required scope.
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
python -m pytest tests/test_server.py tests/test_windows_startup.py tests/test_google_oauth.py
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
