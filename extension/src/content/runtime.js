(() => {
  const connector = globalThis.CanvasTaskSyncConnector
  if (!connector || connector.runtimeInstalled || typeof chrome === 'undefined') return
  connector.runtimeInstalled = true
  const state = { originalTarget: null }

  function currentAdapter() {
    const adapter = connector.adapterFor(location.href)
    if (!adapter) {
      throw Object.assign(new Error('This page is not supported. Open a Google Slides, Docs, or Sheets editor.'), {
        code: 'unsupported_page',
      })
    }
    return adapter
  }

  function serializedError(error) {
    return {
      ok: false,
      error: {
        code: error?.code || 'extraction_failed',
        message: error instanceof Error ? error.message : String(error),
      },
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || !String(message.type || '').startsWith('CTS_')) return undefined
    ;(async () => {
      try {
        const adapter = currentAdapter()
        if (message.type === 'CTS_PING') return { ok: true, adapterId: adapter.id }
        if (message.type === 'CTS_DISCOVER') return { ok: true, value: await adapter.discover() }
        if (message.type === 'CTS_ACQUIRE_TEXT') {
          return { ok: true, value: await adapter.acquireText({ selection: message.selection || {} }) }
        }
        if (message.type === 'CTS_SCREENSHOT_TARGETS') {
          const discovery = await adapter.discover()
          state.originalTarget = discovery.targets.find((target) => target.current)?.id || null
          return {
            ok: true,
            value: await adapter.screenshotTargets({ selection: message.selection || {} }),
          }
        }
        if (message.type === 'CTS_SHOW_TARGET') {
          return { ok: true, value: await adapter.showTarget(message.target) }
        }
        if (message.type === 'CTS_RESTORE') {
          if (state.originalTarget && typeof adapter.showTarget === 'function') {
            await adapter.showTarget({ id: state.originalTarget }).catch(() => {})
          }
          state.originalTarget = null
          return { ok: true }
        }
        throw Object.assign(new Error(`Unknown connector request: ${message.type}`), {
          code: 'unknown_request',
        })
      } catch (error) {
        return serializedError(error)
      }
    })().then(sendResponse)
    return true
  })
})()
