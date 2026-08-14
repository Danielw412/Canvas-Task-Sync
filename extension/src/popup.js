const connection = document.querySelector('#connection')
const loading = document.querySelector('#loading')
const errorBox = document.querySelector('#error')
const form = document.querySelector('#capture-form')
const targetsBox = document.querySelector('#targets')
const mode = document.querySelector('#mode')
const captureButton = document.querySelector('#capture')
const progress = document.querySelector('#progress')
let discovery = null

function send(message) {
  const timeout = new Promise((_, reject) => {
    setTimeout(() => {
      const error = new Error('The extension background worker did not answer. Reload the unpacked extension and try again.')
      error.code = 'background_worker_timeout'
      reject(error)
    }, 10_000)
  })
  return Promise.race([chrome.runtime.sendMessage(message), timeout]).then((response) => {
    if (!response?.ok) {
      const error = new Error(response?.error?.message || 'The extension request failed.')
      error.code = response?.error?.code
      throw error
    }
    return response.value
  })
}

function showError(error) {
  loading.hidden = true
  errorBox.hidden = false
  errorBox.textContent = error instanceof Error ? error.message : String(error)
}

function renderTargets(value) {
  targetsBox.replaceChildren()
  const defaultTargets = value.targets.filter((target) => target.current)
  const defaults = new Set((defaultTargets.length ? defaultTargets : value.selectionKind === 'sections' ? value.targets : value.targets.slice(0, 1)).map((target) => target.id))
  for (const target of value.targets) {
    const row = document.createElement('label')
    row.className = 'target'
    const checkbox = document.createElement('input')
    checkbox.type = 'checkbox'
    checkbox.name = 'target'
    checkbox.value = target.id
    checkbox.dataset.label = target.label
    checkbox.dataset.current = String(Boolean(target.current))
    checkbox.checked = defaults.has(target.id)
    const label = document.createElement('span')
    label.textContent = target.label
    row.append(checkbox, label)
    if (value.selectionKind === 'sheets') {
      const range = document.createElement('input')
      range.type = 'text'
      range.className = 'range'
      range.placeholder = 'Optional range, e.g. A1:F40'
      range.dataset.for = target.id
      row.append(range)
    }
    targetsBox.append(row)
  }
}

function selection() {
  const selected = [...targetsBox.querySelectorAll('input[name="target"]:checked')]
  if (!selected.length) throw new Error('Select at least one source portion.')
  if (discovery.selectionKind === 'slides') return { slideIds: selected.map((item) => item.value) }
  if (discovery.selectionKind === 'sections') return { sectionIds: selected.map((item) => item.value) }
  return {
    sheets: selected.map((item) => ({
      id: item.value,
      name: item.dataset.label,
      range: targetsBox.querySelector(`input.range[data-for="${CSS.escape(item.value)}"]`)?.value.trim() || '',
    })),
  }
}

async function initialize() {
  send({ type: 'CTS_UI_TEST_CONNECTION' }).then(() => {
    connection.textContent = 'Local app connected'
    connection.className = 'badge ok'
  }).catch((error) => {
    connection.textContent = error.code === 'pairing_token_missing' ? 'Pairing needed' : 'Local app unavailable'
    connection.className = 'badge bad'
  })
  discovery = await send({ type: 'CTS_UI_DISCOVER' })
  document.querySelector('#source-title').textContent = discovery.title || 'Untitled Google file'
  document.querySelector('#source-kind').textContent = discovery.sourceType.replace('google_', 'Google ')
  document.querySelector('#selection-title').textContent = {
    slides: 'Relevant slides',
    sections: 'Relevant sections',
    sheets: 'Relevant sheets and ranges',
  }[discovery.selectionKind] || 'Relevant portions'
  renderTargets(discovery)
  loading.hidden = true
  form.hidden = false
}

document.querySelector('#settings').addEventListener('click', () => send({ type: 'CTS_UI_OPEN_OPTIONS' }))
document.querySelector('#select-all').addEventListener('click', () => {
  targetsBox.querySelectorAll('input[name="target"]').forEach((item) => { item.checked = true })
})
document.querySelector('#select-current').addEventListener('click', () => {
  const inputs = [...targetsBox.querySelectorAll('input[name="target"]')]
  const current = inputs.filter((item) => item.dataset.current === 'true')
  inputs.forEach((item, index) => { item.checked = current.length ? current.includes(item) : index === 0 })
})
form.addEventListener('submit', async (event) => {
  event.preventDefault()
  errorBox.hidden = true
  captureButton.disabled = true
  progress.hidden = false
  progress.textContent = 'Capturing the selected content… Keep this tab visible.'
  try {
    const result = await send({ type: 'CTS_UI_CAPTURE', selection: selection(), mode: mode.value || undefined })
    const method = result.acquisition.methodsUsed.join(' + ')
    progress.textContent = `Sent ${result.envelope.items.length} structured items and ${result.envelope.screenshots.length} screenshot(s) using ${method}. You can now preview the course in the local app.`
    connection.textContent = 'Capture accepted'
    connection.className = 'badge ok'
  } catch (error) {
    showError(error)
    progress.hidden = true
  } finally {
    captureButton.disabled = false
  }
})

initialize().catch(showError)
