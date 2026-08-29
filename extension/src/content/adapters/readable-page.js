(() => {
  const connector = globalThis.CanvasTaskSyncConnector
  if (!connector || connector.adapters.some((item) => item.id === 'readable-page-v1')) return
  const { blockedPageError, cleanText, ensureReady, stablePart, uniqueElements } = connector.utils
  const MAX_ITEMS = 500
  const MAX_TEXT_CHARACTERS = 300_000

  function matches(urlValue) {
    try {
      const url = new URL(urlValue)
      return url.protocol === 'https:' && url.hostname !== 'docs.google.com'
    } catch {
      return false
    }
  }

  function canonicalUrl(urlValue = location.href) {
    const url = new URL(urlValue)
    url.hash = ''
    return url.href
  }

  async function resourceId(urlValue) {
    const bytes = new TextEncoder().encode(canonicalUrl(urlValue))
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))
    return `web_${[...digest].map((value) => value.toString(16).padStart(2, '0')).join('').slice(0, 40)}`
  }

  function pageMetadata(doc = document) {
    const description = doc.querySelector('meta[name="description"]')?.content
      || doc.querySelector('meta[property="og:description"]')?.content
      || ''
    return {
      description: cleanText(description).slice(0, 2_000),
      language: doc.documentElement?.lang || null,
      content_type: doc.contentType || null,
    }
  }

  function readableRoot(doc = document) {
    return doc.querySelector('main, article, [role="main"]') || doc.body
  }

  function readableElements(root) {
    return uniqueElements([
      ...root.querySelectorAll('h1, h2, h3, h4, h5, h6, p, li, blockquote, pre, table tr, dt, dd'),
    ]).filter((element) => !element.closest('nav, footer, header, aside, dialog, script, style, noscript'))
  }

  function normalize(raw) {
    let consumed = 0
    const items = []
    for (const [index, block] of raw.blocks.entries()) {
      const text = cleanText(block.text)
      if (!text || consumed >= MAX_TEXT_CHARACTERS || items.length >= MAX_ITEMS) continue
      const bounded = text.slice(0, MAX_TEXT_CHARACTERS - consumed)
      consumed += bounded.length
      items.push({
        id: `page:${index}:${stablePart(bounded.slice(0, 80), 'content')}`,
        kind: block.kind,
        order: index,
        text: bounded,
        role: block.role,
        row_index: null,
        column_index: null,
        row_label: null,
        slide_id: null,
        section_id: block.sectionId || null,
        sheet_id: null,
        sheet_name: null,
        range_a1: null,
        structured_data: block.structuredData || null,
        metadata: block.metadata || {},
      })
    }
    return items
  }

  async function discover(doc = document) {
    await ensureReady(doc)
    const blocked = blockedPageError(doc)
    if (blocked) throw blocked
    const url = canonicalUrl()
    return {
      adapterId: 'readable-page-v1',
      sourceType: 'web_page',
      resourceId: await resourceId(url),
      url,
      canonicalUrl: url,
      title: cleanText(doc.title) || new URL(url).hostname,
      readyState: doc.readyState,
      targets: [{ id: 'page', label: 'Readable page', order: 0, current: true }],
      metadata: pageMetadata(doc),
      warnings: [],
    }
  }

  async function acquireText({ doc = document } = {}) {
    await ensureReady(doc)
    const root = readableRoot(doc)
    if (!root) throw Object.assign(new Error('The page has no readable document body.'), { code: 'no_readable_content' })
    const links = [...root.querySelectorAll('a[href]')].slice(0, 250).map((anchor) => ({
      text: cleanText(anchor.innerText || anchor.textContent).slice(0, 500),
      url: new URL(anchor.href, location.href).href,
    })).filter((link) => /^https?:/i.test(link.url))
    const blocks = readableElements(root).map((element) => {
      const tag = String(element.tagName || '').toLowerCase()
      const heading = tag.match(/^h([1-6])$/)
      return {
        kind: heading ? 'heading' : tag === 'tr' ? 'table_row' : tag || 'paragraph',
        text: element.innerText || element.textContent || '',
        role: heading ? 'header' : 'unknown',
        sectionId: element.closest('section, article')?.id || null,
        structuredData: null,
        metadata: heading ? { heading_level: Number(heading[1]) } : {},
      }
    })
    blocks.unshift({
      kind: 'document_metadata',
      text: cleanText(doc.title),
      role: 'header',
      sectionId: null,
      structuredData: { ...pageMetadata(doc), links },
      metadata: {},
    })
    const items = normalize({ blocks })
    if (!items.some((item) => item.text.trim() || item.structured_data != null)) {
      throw Object.assign(new Error('The authenticated page exposed no readable content.'), { code: 'no_readable_content' })
    }
    return {
      items,
      metadata: { ...pageMetadata(doc), link_count: links.length },
      warnings: blocks.length > MAX_ITEMS ? ['Readable page content was bounded to the first 500 blocks.'] : [],
    }
  }

  async function screenshotTargets() {
    return [{ id: 'page', itemId: 'page:visual', order: 0, metadata: { page_url: canonicalUrl() } }]
  }

  async function showTarget() {
    window.scrollTo({ top: 0, behavior: 'instant' })
    return { metadata: { page_url: canonicalUrl() } }
  }

  connector.register({
    id: 'readable-page-v1',
    sourceType: 'web_page',
    matches,
    discover,
    normalize,
    acquireText,
    screenshotTargets,
    showTarget,
  })
})()
