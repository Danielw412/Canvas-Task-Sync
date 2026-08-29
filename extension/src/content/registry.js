(() => {
  if (globalThis.CanvasTaskSyncConnector) return

  const adapters = []
  const RESOURCE_PATTERNS = {
    google_slides: /\/presentation\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]+)/,
    google_docs: /\/document\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]+)/,
    google_sheets: /\/spreadsheets\/(?:u\/\d+\/)?d\/([A-Za-z0-9_-]+)/,
  }

  function cleanText(value) {
    return String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim()
  }

  function sourceInfo(url) {
    const parsed = new URL(url)
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'docs.google.com') {
      throw Object.assign(new Error('Open a supported Google Slides, Docs, or Sheets file.'), {
        code: 'unsupported_page',
      })
    }
    for (const [sourceType, pattern] of Object.entries(RESOURCE_PATTERNS)) {
      const match = parsed.pathname.match(pattern)
      if (match) return { sourceType, resourceId: match[1], canonicalUrl: parsed.href }
    }
    throw Object.assign(new Error('This Google page is not a supported editor URL.'), {
      code: 'unsupported_page',
    })
  }

  function blockedPageError(doc = document, locationValue = location) {
    const text = cleanText(doc.body?.innerText).toLowerCase()
    if (/\/accounts\//.test(locationValue.pathname) || /sign in(?: to continue)?|choose an account|log in|authentication required/.test(text)) {
      return Object.assign(new Error('Sign in to this resource in Chrome, then retry.'), {
        code: 'sign_in_required',
      })
    }
    if (/you need access|request access|access denied|permission denied|forbidden|unauthorized|file does not exist/.test(text)) {
      return Object.assign(new Error('The current Chrome session cannot access this resource.'), {
        code: 'access_denied',
      })
    }
    return null
  }

  function sleep(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds))
  }

  async function ensureReady(doc = document) {
    const blocked = blockedPageError(doc)
    if (blocked) throw blocked
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const bodyText = cleanText(doc.body?.innerText)
      const canvases = doc.querySelectorAll?.('canvas, [role="textbox"], [role="grid"], table').length || 0
      if (doc.readyState === 'complete' && (bodyText.length > 20 || canvases > 0)) return
      await sleep(250)
      const error = blockedPageError(doc)
      if (error) throw error
    }
    throw Object.assign(new Error('The file is still loading. Wait for Google to finish rendering it.'), {
      code: 'page_incomplete',
    })
  }

  function elementText(element) {
    return cleanText(element?.innerText || element?.textContent || element?.getAttribute?.('aria-label'))
  }

  function uniqueElements(elements) {
    const seen = new Set()
    return [...elements].filter((element) => {
      if (!element || seen.has(element)) return false
      seen.add(element)
      return true
    })
  }

  function stablePart(value, fallback = 'item') {
    const normalized = cleanText(value).replace(/[^A-Za-z0-9_.:-]+/g, '_').slice(0, 180)
    return normalized || fallback
  }

  function headerRole(text) {
    const normalized = cleanText(text).toLowerCase()
    if (normalized === 'assignments' || normalized.includes('homework')) return 'assignments'
    if (normalized.includes('learning activities') || normalized.includes('classwork')) return 'learning'
    if (normalized.includes('learning target')) return 'header'
    return 'unknown'
  }

  function register(adapter) {
    const requiredMethods = ['matches', 'discover', 'normalize', 'acquireText', 'screenshotTargets', 'showTarget']
    if (!adapter?.id || !adapter.sourceType || requiredMethods.some((name) => typeof adapter[name] !== 'function')) {
      throw new Error('Source adapters must implement the complete connector adapter interface.')
    }
    if (!adapters.some((candidate) => candidate.id === adapter.id)) adapters.push(adapter)
  }

  function adapterFor(url) {
    return adapters.find((adapter) => adapter.matches(url)) || null
  }

  globalThis.CanvasTaskSyncConnector = {
    adapters,
    register,
    adapterFor,
    utils: {
      blockedPageError,
      cleanText,
      elementText,
      ensureReady,
      headerRole,
      sleep,
      sourceInfo,
      stablePart,
      uniqueElements,
    },
  }
})()
