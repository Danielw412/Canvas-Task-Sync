export const ACQUISITION_MODES = Object.freeze([
  'screenshot',
  'text',
  'both',
  'prefer_screenshot',
  'prefer_text',
])

export const SOURCE_TYPES = Object.freeze([
  'google_slides',
  'google_docs',
  'google_sheets',
])

export const DEFAULT_CONFIG = Object.freeze({
  serverUrl: 'http://127.0.0.1:8790',
  pairingToken: '',
  globalMode: 'prefer_text',
  sourceOverrides: {
    google_slides: 'both',
    google_docs: 'inherit',
    google_sheets: 'inherit',
  },
})

export function normalizeServerUrl(value) {
  const parsed = new URL(String(value || '').trim())
  if (parsed.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(parsed.hostname)) {
    throw new Error('The local app address must use http://127.0.0.1 or http://localhost.')
  }
  if (parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    throw new Error('Enter only the local app origin, for example http://127.0.0.1:8790.')
  }
  return parsed.origin
}

export function normalizeConfig(value = {}) {
  const overrides = { ...DEFAULT_CONFIG.sourceOverrides, ...(value.sourceOverrides || {}) }
  const globalMode = ACQUISITION_MODES.includes(value.globalMode)
    ? value.globalMode
    : DEFAULT_CONFIG.globalMode
  for (const sourceType of SOURCE_TYPES) {
    if (!['inherit', ...ACQUISITION_MODES].includes(overrides[sourceType])) {
      overrides[sourceType] = 'inherit'
    }
  }
  return {
    serverUrl: normalizeServerUrl(value.serverUrl || DEFAULT_CONFIG.serverUrl),
    pairingToken: String(value.pairingToken || '').trim(),
    globalMode,
    sourceOverrides: overrides,
  }
}

export function resolveMode(config, sourceType, oneTimeMode) {
  if (oneTimeMode && ACQUISITION_MODES.includes(oneTimeMode)) return oneTimeMode
  const normalized = normalizeConfig(config)
  const override = normalized.sourceOverrides[sourceType]
  return override && override !== 'inherit' ? override : normalized.globalMode
}

export async function loadConfig(storage = chrome.storage.local) {
  const stored = await storage.get('connectorConfig')
  return normalizeConfig(stored.connectorConfig)
}

export async function saveConfig(config, storage = chrome.storage.local) {
  const normalized = normalizeConfig(config)
  await storage.set({ connectorConfig: normalized })
  return normalized
}
