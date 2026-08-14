import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import { acquireWithMode, AcquisitionError } from '../src/lib/acquisition.js'
import { DEFAULT_CONFIG, normalizeConfig, normalizeServerUrl, resolveMode } from '../src/lib/config.js'
import {
  claimCaptureRequest,
  ConnectionError,
  postCapture,
  reportCaptureFailure,
  testConnection,
} from '../src/lib/connection.js'
import { buildEnvelope } from '../src/lib/schema.js'

await import('../src/content/registry.js')
await import('../src/content/adapters/slides.js')
await import('../src/content/adapters/docs.js')
await import('../src/content/adapters/sheets.js')

const registry = globalThis.CanvasTaskSyncConnector
const slides = registry.adapters.find((adapter) => adapter.sourceType === 'google_slides')
const docs = registry.adapters.find((adapter) => adapter.sourceType === 'google_docs')
const sheets = registry.adapters.find((adapter) => adapter.sourceType === 'google_sheets')

test('Manifest V3 background worker parses and registers its listeners', async () => {
  const listeners = []
  globalThis.chrome = {
    runtime: {
      onInstalled: { addListener(listener) { listeners.push(listener) } },
      onStartup: { addListener(listener) { listeners.push(listener) } },
      onMessage: { addListener(listener) { listeners.push(listener) } },
    },
    alarms: {
      create: async () => {},
      onAlarm: { addListener(listener) { listeners.push(listener) } },
    },
  }
  await import('../src/background.js')
  assert.equal(listeners.length, 4)
})

test('Manifest grants only the Google and loopback access needed for automatic capture', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'))
  assert.equal(manifest.manifest_version, 3)
  assert.ok(manifest.permissions.includes('alarms'))
  assert.ok(manifest.host_permissions.includes('https://docs.google.com/*'))
  assert.deepEqual(manifest.optional_host_permissions, ['<all_urls>'])
  assert.ok(manifest.content_scripts[0].matches.includes('http://localhost/*'))
  assert.equal(manifest.permissions.includes('cookies'), false)
  assert.equal(manifest.permissions.includes('history'), false)
})

test('Slides adapter preserves multi-slide ordering, IDs, rows, and roles', () => {
  const items = slides.normalize({
    slides: [
      {
        id: 'slide-b', order: 1, blocks: [{ id: 'shape', text: 'Later slide', kind: 'shape' }],
      },
      {
        id: 'slide-a', order: 0, current: true, blocks: [
          { id: 'day', text: 'M', kind: 'table_cell', rowIndex: 1, columnIndex: 0 },
          { id: 'assignment', text: 'Read chapter 2', kind: 'table_cell', rowIndex: 1, columnIndex: 2, role: 'assignments' },
        ],
      },
    ],
  }, { slideIds: ['slide-a', 'slide-b'] })
  assert.deepEqual(items.map((item) => item.slide_id), ['slide-a', 'slide-a', 'slide-b'])
  assert.equal(items[1].id, 'slide-a:assignment')
  assert.equal(items[1].row_label, 'M')
  assert.equal(items[1].role, 'assignments')
  assert.ok(items[2].order > items[1].order)
})

test('Docs adapter preserves headings, sections, paragraphs, and table structure', () => {
  const items = docs.normalize({
    sections: [
      {
        id: 'overview', label: 'Week of August 10, 2026', level: 1, order: 0, blocks: [
          { id: 'heading', kind: 'heading', text: 'Week of August 10, 2026' },
          {
            id: 'cell', kind: 'table_cell', text: 'Submit project', rowIndex: 2, columnIndex: 1,
            rowLabel: 'W', structuredData: { row: 2, column: 1, value: 'Submit project' },
          },
        ],
      },
      { id: 'notes', label: 'Teacher notes', level: 2, order: 1, blocks: [{ id: 'p', text: 'Notes' }] },
    ],
  }, { sectionIds: ['overview'] })
  assert.equal(items.length, 2)
  assert.equal(items[0].kind, 'heading')
  assert.equal(items[0].role, 'header')
  assert.equal(items[1].section_id, 'overview')
  assert.deepEqual(items[1].structured_data, { row: 2, column: 1, value: 'Submit project' })
})

test('Sheets adapter preserves tabs, ranges, rows, columns, headers, and cells', () => {
  const items = sheets.normalize({
    sheets: [
      { id: '0', name: 'Agenda', order: 0, range: 'B2:D4', rows: [
        ['Day', 'Learning activities', 'Assignments'],
        ['M', 'Review', 'Read chapter 2'],
      ] },
      { id: '22', name: 'Archive', order: 1, rows: [['Old']] },
    ],
  }, { sheets: [{ id: '0', name: 'Agenda', range: 'B2:D4' }] })
  assert.equal(items.length, 6)
  assert.ok(items.every((item) => item.sheet_id === '0'))
  assert.equal(items.find((item) => item.text === 'Read chapter 2').range_a1, 'D3')
  assert.equal(items.find((item) => item.text === 'Read chapter 2').row_label, 'M')
  assert.equal(items.find((item) => item.text === 'Read chapter 2').role, 'assignments')
  assert.equal(items[0].metadata.selection_range, 'B2:D4')
})

test('Acquisition supports screenshot, text, and both modes', async () => {
  const callOrder = []
  const operations = {
    text: async () => { callOrder.push('text'); return { items: [{ id: 'text' }] } },
    screenshot: async () => { callOrder.push('screenshot'); return { screenshots: [{ id: 'shot' }] } },
  }
  assert.deepEqual((await acquireWithMode('text', operations)).methodsUsed, ['text'])
  assert.deepEqual((await acquireWithMode('screenshot', operations)).methodsUsed, ['screenshot'])
  assert.deepEqual((await acquireWithMode('both', operations)).methodsUsed, ['text', 'screenshot'])
  assert.deepEqual(callOrder.slice(-2), ['text', 'screenshot'])
})

test('Preferred acquisition falls back clearly and both mode does not silently degrade', async () => {
  const fallback = await acquireWithMode('prefer_screenshot', {
    screenshot: async () => { throw new Error('blank capture') },
    text: async () => ({ items: [{ id: 'text' }] }),
  })
  assert.equal(fallback.fallbackUsed, true)
  assert.deepEqual(fallback.methodsUsed, ['text'])
  assert.match(fallback.warnings[0], /blank capture/)
  await assert.rejects(
    acquireWithMode('both', {
      screenshot: async () => { throw new Error('blank capture') },
      text: async () => ({ items: [{ id: 'text' }] }),
    }),
    (error) => error instanceof AcquisitionError && error.code === 'both_methods_required',
  )
})

test('Configuration applies sensible global defaults and per-source overrides', () => {
  const config = normalizeConfig({ globalMode: 'prefer_text', sourceOverrides: { google_slides: 'both' } })
  assert.equal(resolveMode(config, 'google_slides'), 'both')
  assert.equal(resolveMode(config, 'google_docs'), 'prefer_text')
  assert.equal(resolveMode(config, 'google_sheets', 'screenshot'), 'screenshot')
  assert.equal(DEFAULT_CONFIG.serverUrl, 'http://127.0.0.1:8790')
  assert.throws(() => normalizeServerUrl('https://example.com'), /local app address/)
})

test('Normalized envelope preserves source metadata and fallback state', () => {
  const envelope = buildEnvelope({
    discovery: {
      adapterId: 'google-sheets-v1', sourceType: 'google_sheets', resourceId: 'book-id',
      url: 'https://docs.google.com/spreadsheets/d/book-id/edit?gid=0', title: 'Agenda', readyState: 'complete',
    },
    requestedMode: 'prefer_screenshot',
    acquisition: {
      results: { text: { items: [{ id: '0:A1', kind: 'cell', order: 0, text: 'Assignments', role: 'header', sheet_id: '0', sheet_name: 'Agenda', range_a1: 'A1', metadata: {} }], metadata: { cell_count: 1 } } },
      methodsUsed: ['text'], fallbackUsed: true, warnings: ['Screenshot capture failed: blank.'],
    },
    selection: { sheets: [{ id: '0', name: 'Agenda', range: 'A1:F10' }] },
  })
  assert.equal(envelope.source_type, 'google_sheets')
  assert.equal(envelope.resource_id, 'book-id')
  assert.equal(envelope.fallback_used, true)
  assert.deepEqual(envelope.selection.sheets[0], { id: '0', name: 'Agenda', range: 'A1:F10' })
  assert.equal(envelope.metadata.cell_count, 1)
})

test('Extension-to-app connection sends only the pairing token and normalized payload', async () => {
  let observed
  const fetchImpl = async (url, options) => {
    observed = { url, options }
    return { ok: true, status: 202, json: async () => ({ accepted: true }) }
  }
  const config = { serverUrl: 'http://127.0.0.1:8790', pairingToken: 'pair-token' }
  const response = await postCapture(config, { schema_version: 1 }, fetchImpl)
  assert.equal(response.accepted, true)
  assert.equal(observed.url, 'http://127.0.0.1:8790/api/v1/extension/captures')
  assert.equal(observed.options.headers['X-Extension-Token'], 'pair-token')
  assert.equal(observed.options.headers.Authorization, undefined)
})

test('Automatic capture queue claims work and reports a structured failure', async () => {
  const observed = []
  const fetchImpl = async (url, options) => {
    observed.push({ url, options })
    if (url.endsWith('/next')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ request_id: 'request-1', source_url: 'https://docs.google.com/document/d/example/edit' }),
      }
    }
    return { ok: true, status: 204, json: async () => ({}) }
  }
  const config = { serverUrl: 'http://127.0.0.1:8790', pairingToken: 'pair-token' }
  const request = await claimCaptureRequest(config, fetchImpl)
  assert.equal(request.request_id, 'request-1')
  const error = Object.assign(new Error('No access.'), { code: 'access_denied' })
  await reportCaptureFailure(config, 'request-1', error, fetchImpl)
  assert.match(observed[1].url, /capture-requests\/request-1\/failed$/)
  assert.deepEqual(JSON.parse(observed[1].options.body), { code: 'access_denied', message: 'No access.' })
})

test('Connection errors distinguish unreachable app and rejected pairing', async () => {
  const config = { serverUrl: 'http://127.0.0.1:8790', pairingToken: 'bad-token' }
  await assert.rejects(
    testConnection(config, async () => { throw new Error('ECONNREFUSED') }),
    (error) => error instanceof ConnectionError && error.code === 'local_app_unreachable',
  )
  await assert.rejects(
    testConnection(config, async () => ({
      ok: false,
      status: 401,
      json: async () => ({ error: { code: 'extension_pairing_failed', message: 'Pairing failed.' } }),
    })),
    (error) => error instanceof ConnectionError && error.code === 'extension_pairing_failed',
  )
})

test('Page errors distinguish access denial, sign-in, and unsupported URLs', () => {
  const denied = registry.utils.blockedPageError(
    { body: { innerText: 'You need access to open this file' } },
    { pathname: '/presentation/d/example/edit' },
  )
  assert.equal(denied.code, 'access_denied')
  const signedOut = registry.utils.blockedPageError(
    { body: { innerText: 'Sign in to continue' } },
    { pathname: '/accounts/signin' },
  )
  assert.equal(signedOut.code, 'sign_in_required')
  assert.equal(registry.adapterFor('https://example.com/not-supported'), null)
})
