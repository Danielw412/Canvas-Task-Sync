import { ACQUISITION_MODES, loadConfig, saveConfig } from './lib/config.js'
import { testConnection } from './lib/connection.js'

const labels = {
  screenshot: 'Screenshot only',
  text: 'Text only',
  both: 'Screenshot + text',
  prefer_screenshot: 'Prefer screenshot, then text',
  prefer_text: 'Prefer text, then screenshot',
}
const screenshotPermission = { origins: ['<all_urls>'] }

async function updateScreenshotAccess() {
  const enabled = await chrome.permissions.contains(screenshotPermission)
  const button = document.querySelector('#screenshot-access')
  const result = document.querySelector('#screenshot-access-result')
  button.textContent = enabled
    ? 'Remove linked-resource access'
    : 'Enable linked-resource access'
  button.dataset.enabled = String(enabled)
  result.textContent = enabled
    ? 'Enabled for authenticated linked pages and website-started screenshots.'
    : 'Google Workspace text capture remains available without this optional permission.'
}

function fillModes(select, inherit = false) {
  select.replaceChildren()
  if (inherit) select.append(new Option('Inherit global default', 'inherit'))
  for (const mode of ACQUISITION_MODES) select.append(new Option(labels[mode], mode))
}

async function values() {
  return {
    serverUrl: document.querySelector('#server-url').value,
    pairingToken: document.querySelector('#pairing-token').value,
    globalMode: document.querySelector('#global-mode').value,
    sourceOverrides: Object.fromEntries(
      [...document.querySelectorAll('[data-override]')].map((select) => [select.dataset.override, select.value]),
    ),
  }
}

async function initialize() {
  fillModes(document.querySelector('#global-mode'))
  document.querySelectorAll('[data-override]').forEach((select) => fillModes(select, true))
  const config = await loadConfig()
  document.querySelector('#server-url').value = config.serverUrl
  document.querySelector('#pairing-token').value = config.pairingToken
  document.querySelector('#global-mode').value = config.globalMode
  for (const [sourceType, mode] of Object.entries(config.sourceOverrides)) {
    document.querySelector(`[data-override="${sourceType}"]`).value = mode
  }
  await updateScreenshotAccess()
}

document.querySelector('#save').addEventListener('click', async () => {
  const result = document.querySelector('#save-result')
  try {
    await saveConfig(await values())
    result.textContent = 'Saved.'
  } catch (error) {
    result.textContent = error instanceof Error ? error.message : String(error)
  }
})
document.querySelector('#test').addEventListener('click', async () => {
  const result = document.querySelector('#connection-result')
  result.textContent = 'Testing…'
  try {
    const config = await saveConfig(await values())
    await testConnection(config)
    result.textContent = 'Connected to the local app.'
  } catch (error) {
    result.textContent = error instanceof Error ? error.message : String(error)
  }
})
document.querySelector('#screenshot-access').addEventListener('click', async (event) => {
  const result = document.querySelector('#screenshot-access-result')
  try {
    const enabled = event.currentTarget.dataset.enabled === 'true'
    const changed = enabled
      ? await chrome.permissions.remove(screenshotPermission)
      : await chrome.permissions.request(screenshotPermission)
    if (!changed && !enabled) result.textContent = 'Permission was not granted.'
    await updateScreenshotAccess()
  } catch (error) {
    result.textContent = error instanceof Error ? error.message : String(error)
  }
})

initialize()
