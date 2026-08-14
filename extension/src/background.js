import { acquireWithMode } from './lib/acquisition.js'
import { loadConfig, resolveMode, saveConfig } from './lib/config.js'
import {
  claimCaptureRequest,
  postCapture,
  reportCaptureFailure,
  testConnection,
} from './lib/connection.js'
import { buildEnvelope } from './lib/schema.js'

const CONTENT_FILES = [
  'content/registry.js',
  'content/adapters/slides.js',
  'content/adapters/docs.js',
  'content/adapters/sheets.js',
  'content/runtime.js',
]
const MAX_SCREENSHOTS = 30
const SCREENSHOT_INTERVAL_MS = 650
const AUTOMATIC_SCREENSHOT_PERMISSION = { origins: ['<all_urls>'] }
let lastVisibleCaptureAt = 0
let automaticQueuePromise = null

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true })
  if (!tab?.id || !tab.url) throw new Error('No active Chrome tab is available.')
  return tab
}

async function sendToTab(tabId, message) {
  const response = await chrome.tabs.sendMessage(tabId, message)
  if (!response?.ok) {
    const error = new Error(response?.error?.message || 'The page connector did not respond.')
    error.code = response?.error?.code || 'page_connector_failed'
    throw error
  }
  return response.value
}

async function ensureInjected(tabId) {
  try {
    await sendToTab(tabId, { type: 'CTS_PING' })
    return
  } catch {
    await chrome.scripting.executeScript({ target: { tabId }, files: CONTENT_FILES })
    await sendToTab(tabId, { type: 'CTS_PING' })
  }
}

async function discover(tab) {
  const target = tab || await activeTab()
  await ensureInjected(target.id)
  return sendToTab(target.id, { type: 'CTS_DISCOVER' })
}

async function sha256(dataUrl) {
  const bytes = new Uint8Array(await (await fetch(dataUrl)).arrayBuffer())
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
  return [...digest].map((value) => value.toString(16).padStart(2, '0')).join('')
}

async function inspectScreenshot(dataUrl) {
  if (!dataUrl.startsWith('data:image/png;base64,') || dataUrl.length < 300) {
    throw new Error('Chrome returned an empty screenshot.')
  }
  if (typeof createImageBitmap !== 'function' || typeof OffscreenCanvas !== 'function') {
    return { width: null, height: null }
  }
  const image = await createImageBitmap(await (await fetch(dataUrl)).blob())
  const dimensions = { width: image.width || null, height: image.height || null }
  const canvas = new OffscreenCanvas(48, 48)
  const context = canvas.getContext('2d', { willReadFrequently: true })
  context.drawImage(image, 0, 0, 48, 48)
  image.close()
  const pixels = context.getImageData(0, 0, 48, 48).data
  let minimum = 255
  let maximum = 0
  for (let index = 0; index < pixels.length; index += 4) {
    const luminance = (pixels[index] + pixels[index + 1] + pixels[index + 2]) / 3
    minimum = Math.min(minimum, luminance)
    maximum = Math.max(maximum, luminance)
  }
  if (maximum - minimum < 2) throw new Error('The visible tab capture appears blank.')
  return dimensions
}

async function captureText(tab, selection) {
  return sendToTab(tab.id, { type: 'CTS_ACQUIRE_TEXT', selection })
}

async function captureScreenshots(tab, selection) {
  const targets = await sendToTab(tab.id, { type: 'CTS_SCREENSHOT_TARGETS', selection })
  if (!targets?.length) throw new Error('The selected source portions have no screenshot targets.')
  if (targets.length > MAX_SCREENSHOTS) {
    throw new Error(`Select ${MAX_SCREENSHOTS} or fewer screenshot targets per capture.`)
  }
  const screenshots = []
  try {
    for (const target of targets) {
      const shown = await sendToTab(tab.id, { type: 'CTS_SHOW_TARGET', target })
      await chrome.tabs.update(tab.id, { active: true })
      const waitForQuota = SCREENSHOT_INTERVAL_MS - (Date.now() - lastVisibleCaptureAt)
      if (waitForQuota > 0) await sleep(waitForQuota)
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' })
      lastVisibleCaptureAt = Date.now()
      const dimensions = await inspectScreenshot(dataUrl)
      screenshots.push({
        id: `screenshot:${target.id}`,
        item_id: target.itemId || null,
        order: target.order ?? 0,
        mime_type: 'image/png',
        data_url: dataUrl,
        sha256: await sha256(dataUrl),
        width: dimensions.width,
        height: dimensions.height,
        metadata: { ...(target.metadata || {}), ...(shown?.metadata || {}) },
      })
    }
  } finally {
    await sendToTab(tab.id, { type: 'CTS_RESTORE' }).catch(() => {})
  }
  return {
    screenshots,
    metadata: { screenshot_count: screenshots.length, screenshot_scope: 'visible_viewport' },
    warnings: selection.sheets?.some((sheet) => sheet.range)
      ? ['Sheet screenshots show the viewport centered on the selected range; structured text is range-precise.']
      : [],
  }
}

async function captureAndSend({ selection = {}, mode: oneTimeMode } = {}) {
  const tab = await activeTab()
  return captureTabAndSend(tab, { selection, mode: oneTimeMode })
}

async function captureTabAndSend(tab, { selection = {}, mode: oneTimeMode, automatic = false } = {}) {
  await ensureInjected(tab.id)
  const discovery = await discover(tab)
  const config = await loadConfig()
  const requestedMode = resolveMode(config, discovery.sourceType, oneTimeMode)
  const acquisition = await acquireWithMode(requestedMode, {
    text: () => captureText(tab, selection),
    screenshot: async () => {
      if (automatic && !await chrome.permissions.contains(AUTOMATIC_SCREENSHOT_PERMISSION)) {
        const error = new Error(
          'Automatic screenshots need the optional permission in the extension settings. Text-only automatic capture remains available.',
        )
        error.code = 'automatic_screenshot_permission_required'
        throw error
      }
      return captureScreenshots(tab, selection)
    },
  })
  const envelope = buildEnvelope({ discovery, requestedMode, acquisition, selection })
  const accepted = await postCapture(config, envelope)
  return { discovery, requestedMode, acquisition, envelope, accepted }
}

async function waitForTabReady(tabId, timeoutMs = 45_000) {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener)
      reject(new Error('The automatic Google file tab did not finish loading.'))
    }, timeoutMs)
    function listener(updatedTabId, changeInfo) {
      if (updatedTabId !== tabId || changeInfo.status !== 'complete') return
      clearTimeout(timeout)
      chrome.tabs.onUpdated.removeListener(listener)
      resolve()
    }
    chrome.tabs.onUpdated.addListener(listener)
    chrome.tabs.get(tabId).then((current) => {
      if (current.status !== 'complete') return
      clearTimeout(timeout)
      chrome.tabs.onUpdated.removeListener(listener)
      resolve()
    }, (error) => {
      clearTimeout(timeout)
      chrome.tabs.onUpdated.removeListener(listener)
      reject(error)
    })
  })
  await sleep(800)
}

async function processAutomaticQueue() {
  const config = await loadConfig()
  if (!config.pairingToken) return { processed: 0 }
  let processed = 0
  while (true) {
    const request = await claimCaptureRequest(config)
    if (!request) return { processed }
    const [previousTab] = await chrome.tabs.query({ active: true, currentWindow: true })
    let temporaryTab = null
    try {
      temporaryTab = await chrome.tabs.create({ url: request.source_url, active: true })
      if (!temporaryTab?.id) throw new Error('Chrome could not open the queued source tab.')
      await waitForTabReady(temporaryTab.id)
      temporaryTab = await chrome.tabs.get(temporaryTab.id)
      await captureTabAndSend(temporaryTab, {
        selection: request.selection || {},
        mode: request.requested_mode,
        automatic: true,
      })
      processed += 1
    } catch (error) {
      await reportCaptureFailure(config, request.request_id, error).catch(() => {})
    } finally {
      if (temporaryTab?.id) await chrome.tabs.remove(temporaryTab.id).catch(() => {})
      if (previousTab?.id) await chrome.tabs.update(previousTab.id, { active: true }).catch(() => {})
    }
  }
}

function wakeAutomaticQueue() {
  if (!automaticQueuePromise) {
    automaticQueuePromise = processAutomaticQueue().finally(() => {
      automaticQueuePromise = null
    })
  }
  return automaticQueuePromise
}

function responseError(error) {
  return {
    ok: false,
    error: {
      code: error?.code || 'extension_error',
      message: error instanceof Error ? error.message : String(error),
    },
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  const config = await loadConfig()
  await saveConfig(config)
  await chrome.alarms.create('canvas-task-sync-capture-queue', { periodInMinutes: 0.5 })
})

chrome.runtime.onStartup.addListener(() => {
  void wakeAutomaticQueue()
})

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'canvas-task-sync-capture-queue') void wakeAutomaticQueue()
})

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || (!String(message.type || '').startsWith('CTS_UI_') && message.type !== 'CTS_AUTO_WAKE')) return undefined
  ;(async () => {
    if (message.type === 'CTS_UI_DISCOVER') return { ok: true, value: await discover() }
    if (message.type === 'CTS_UI_CAPTURE') return { ok: true, value: await captureAndSend(message) }
    if (message.type === 'CTS_UI_TEST_CONNECTION') {
      return { ok: true, value: await testConnection(await loadConfig()) }
    }
    if (message.type === 'CTS_UI_OPEN_OPTIONS') {
      await chrome.runtime.openOptionsPage()
      return { ok: true }
    }
    if (message.type === 'CTS_AUTO_WAKE') return { ok: true, value: await wakeAutomaticQueue() }
    throw new Error(`Unknown extension request: ${message.type}`)
  })().catch((error) => responseError(error)).then(sendResponse)
  return true
})
