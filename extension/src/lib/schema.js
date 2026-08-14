function nowIso() {
  return new Date().toISOString()
}

function randomId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`
}

export function buildEnvelope({
  discovery,
  requestedMode,
  acquisition,
  selection,
}) {
  const textResult = acquisition.results.text
  const screenshotResult = acquisition.results.screenshot
  let items = textResult?.items || []
  const screenshots = screenshotResult?.screenshots || []
  if (!items.length && screenshots.length) {
    items = screenshots.map((screenshot, index) => ({
      id: screenshot.item_id || screenshot.id,
      kind: 'visual_capture',
      order: screenshot.order ?? index,
      text: '',
      role: 'unknown',
      slide_id: screenshot.metadata?.slide_id || null,
      section_id: screenshot.metadata?.section_id || null,
      sheet_id: screenshot.metadata?.sheet_id || null,
      sheet_name: screenshot.metadata?.sheet_name || null,
      range_a1: screenshot.metadata?.range_a1 || null,
      metadata: { ...(screenshot.metadata || {}), visual_only: true },
    }))
  }
  return {
    schema_version: 1,
    capture_id: randomId(),
    source_type: discovery.sourceType,
    source_url: discovery.canonicalUrl || discovery.url,
    resource_id: discovery.resourceId,
    title: discovery.title || '',
    captured_at: nowIso(),
    requested_mode: requestedMode,
    methods_used: acquisition.methodsUsed,
    fallback_used: acquisition.fallbackUsed,
    items,
    screenshots,
    selection: selection || {},
    metadata: {
      adapter_id: discovery.adapterId,
      page_ready_state: discovery.readyState,
      ...(textResult?.metadata || {}),
      ...(screenshotResult?.metadata || {}),
    },
    warnings: [
      ...(acquisition.warnings || []),
      ...(discovery.warnings || []),
      ...(textResult?.warnings || []),
      ...(screenshotResult?.warnings || []),
    ],
  }
}
