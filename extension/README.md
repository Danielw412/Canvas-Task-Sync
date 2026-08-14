# Chrome source connector

This Manifest V3 extension is a browser-session source connector for Canvas Task Sync. It supports
Google Slides, Google Docs, and Google Sheets that the current Chrome tab can already display. It
does not authenticate to Google, inspect cookies, store school credentials, call Google Tasks, or
implement a separate sync pipeline.

## Build and connect

The extension has no third-party runtime or build dependencies.

```powershell
Set-Location .\extension
npm test
npm run build
```

Load `extension\dist` from `chrome://extensions` with Developer mode enabled. Start the local app with
`canvas-task-sync web`, then copy the server URL and pairing token from **Settings → Connections →
Chrome source connector** into the extension options page. The **Test connection** button checks the
versioned localhost bridge without sending source content.

The default permissions are deliberately narrow:

- `activeTab`: temporary access to a source tab during manual capture.
- `alarms`: wakes the background queue so website-requested captures continue reliably.
- `scripting`: injects the selected source adapter into that active tab.
- `storage`: stores connection settings and acquisition defaults, never captured content.
- `http://127.0.0.1/*` and `http://localhost/*`: sends normalized captures to the local app only.
- `https://docs.google.com/*`: opens queued Google files and reads same-origin exports using only the
  authorization already present in the Chrome session.

No cookie permission, downloads permission, clipboard permission, or Google OAuth token is requested.
Chrome requires `<all_urls>` specifically for `captureVisibleTab` outside an extension-button click;
the connector declares it as optional and requests it only when the user clicks **Enable automatic
screenshots** in settings. Text-only automation does not require that optional grant.

Starting a browser-backed preview in the website automatically creates a capture request. The
extension processes those requests one at a time, opens a temporary Google tab, waits for it to load,
captures the configured screenshot/text mode, submits the normalized envelope, closes only that tab,
and restores the previously active tab. Manual popup capture remains supported.

## Source adapters and normalized output

All format adapters implement the same browser-side contract: detect, discover selectable targets,
extract structured text, enumerate screenshot targets, and display one target for capture. A new
format normally adds one file under `src/content/adapters/` and registers it in the content file list;
the acquisition, connection, popup, and backend bridge stay unchanged.

Every accepted capture includes:

- schema and capture versions, source type/URL, resource ID, title, and capture timestamp;
- requested acquisition mode, methods actually used, and fallback/warning details;
- ordered items with stable IDs, exact text or structured data, semantic roles, row/column context,
  and slide/section/sheet/range identifiers;
- ordered screenshot data with its target ID, dimensions, MIME type, SHA-256 digest, and
  source-specific metadata;
- the user's explicit selection and adapter-specific metadata.

The Python browser source adapter maps this envelope into the existing `SourceCapture` and
`AgendaBlock` models. Multiple screenshots are passed to Gemini in order; downstream date,
deduplication, dry-run, identity, and Google Tasks behavior remains shared.

## Format behavior

### Google Slides

The popup lists filmstrip slides and supports current, multiple, or all relevant slides. Each item
retains presentation ID, slide ID, slide order, text/table cell structure when Chrome exposes it, and
screenshot target metadata. Screenshot capture moves through selected slides and restores the
original slide afterward.

### Google Docs

The popup lists headings as selectable sections. Text acquisition first uses an authenticated DOCX
export so canvas-rendered and virtualized Docs still preserve headings, sections, tables, semantic
column roles, rows, and columns. It falls back to authenticated plain text and then rendered-DOM
viewport extraction. Screenshot acquisition walks document viewports with a Chrome-quota-safe delay
and restores the original scroll position. Documents without exposed headings use an entire-document
target.

### Google Sheets

The popup lists tabs and accepts an optional A1 range per tab. Text acquisition requests authenticated
CSV from the same Google origin using the page's existing session, then parses it locally into cells
with sheet ID/name, tab order, exact cell address, row/column indexes, headers, row labels, selected
range, and structured values. If CSV is unavailable, the adapter can fall back to the visible
accessible grid. Captures are capped at 10,000 non-empty cells; use a bounded range for large sheets.
Screenshots preserve tab/range metadata but represent the visible viewport.

## Acquisition modes

- **Screenshot only:** captures selected visible targets. Blank captures are rejected.
- **Text only:** returns structured text/cells without screenshot data.
- **Screenshot + text:** requires both methods; a partial result is not silently accepted.
- **Prefer screenshot, then text:** uses text only if screenshots fail.
- **Prefer text, then screenshot:** uses screenshots only if text extraction fails.

The default is prefer-text globally, with a screenshot+text override for Slides. Options support a
global default and independent Slides, Docs, and Sheets overrides. The popup can choose a one-time
mode without changing saved defaults.

Gemini mode on the course must be compatible with the capture: `text` needs extracted text, `image`
needs screenshots, `hybrid` needs both, and `auto` can use either. Screenshot-only evidence is kept
date-safe: when a deterministic row/date context cannot be established, the existing pipeline marks
the item uncertain instead of guessing a deadline.

## Errors and troubleshooting

- **Unsupported page:** open a Google Slides, Docs, or Sheets `/d/<id>/edit` URL.
- **Sign in required / access denied:** sign in or request access in the same Chrome profile; the
  extension never bypasses Google authorization.
- **Still loading:** wait until the editor finishes rendering and retry.
- **Blank screenshot:** keep the source tab visible, dismiss blocking dialogs, and retry.
- **No exposed text:** use screenshot mode or enable the editor's accessibility/screen-reader support.
- **Screenshot quota:** automatic and multi-target captures are throttled; if Chrome still reports a
  quota error, keep the source visible and retry after a moment.
- **Large Sheet:** select fewer tabs or a bounded A1 range.
- **Local app unreachable:** run `canvas-task-sync web` and verify the configured loopback port.
- **Pairing failed:** copy the current pairing token from the app; rotating a token invalidates the old
  one and clears pending captures.
- **Capture missing or expired:** send a fresh capture and preview within the configured freshness
  window (15 minutes by default).
- **Method mismatch:** recapture using content compatible with the course's Gemini mode.

The app's Settings page shows pending capture counts and can clear them immediately. Captures are
bounded, memory-only, expire automatically, and disappear when the local server stops.
