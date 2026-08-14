(() => {
  const connector = globalThis.CanvasTaskSyncConnector
  if (!connector || connector.adapters.some((item) => item.id === 'google-slides-v1')) return
  const { cleanText, elementText, ensureReady, headerRole, sleep, sourceInfo, stablePart, uniqueElements } = connector.utils

  function slideIdFromUrl(url) {
    const match = String(url).match(/[?#&]slide=id\.([^&#]+)/)
    return match ? decodeURIComponent(match[1]) : null
  }

  function scanSlides(doc = document, url = location.href) {
    const selectors = [
      '[role="option"][aria-label*="Slide"]',
      '.punch-filmstrip-page-thumbnail',
      '.punch-filmstrip-thumbnail',
      '[data-slide-id]',
      '[data-page-id]',
      '[data-slide-page-id]',
      '[id^="punch-filmstrip-thumbnail-"]',
    ]
    const nodes = uniqueElements(selectors.flatMap((selector) => [...doc.querySelectorAll(selector)]))
    const currentId = slideIdFromUrl(url)
    const slides = []
    const seen = new Set()
    nodes.forEach((node, index) => {
      const href = node.querySelector?.('a[href*="slide=id."]')?.href || ''
      const hrefId = slideIdFromUrl(href)
      const rawId = node.dataset?.slideId || node.dataset?.pageId || node.dataset?.slidePageId || hrefId || node.id?.replace(/^punch-filmstrip-thumbnail-/, '')
      const id = rawId || (node.getAttribute?.('aria-selected') === 'true' && currentId) || `slide-${index + 1}`
      if (seen.has(id)) return
      seen.add(id)
      const pageNumber = cleanText(node.querySelector?.('.punch-filmstrip-thumbnail-pagenumber')?.textContent)
      const ariaPosition = Number(node.getAttribute?.('aria-posinset'))
      const numericPage = Number(pageNumber)
      const rawLabel = node.getAttribute?.('aria-label') || `Slide ${pageNumber || index + 1}`
      slides.push({
        id,
        label: cleanText(rawLabel),
        order: ariaPosition > 0 ? ariaPosition - 1 : numericPage > 0 ? numericPage - 1 : index,
        current: node.getAttribute?.('aria-selected') === 'true'
          || node.classList?.contains('punch-filmstrip-selected-thumbnail')
          || Boolean(node.querySelector?.('.punch-filmstrip-selected-thumbnail-pagenumber'))
          || id === currentId,
      })
    })
    if (!slides.length) {
      slides.push({ id: currentId || 'current-slide', label: 'Current slide', order: 0, current: true })
    }
    return slides.sort((left, right) => left.order - right.order)
  }

  function normalize(raw, selection = {}) {
    const selected = new Set(selection.slideIds || raw.slides.filter((slide) => slide.current).map((slide) => slide.id))
    const items = []
    for (const slide of [...raw.slides].sort((a, b) => a.order - b.order)) {
      if (selected.size && !selected.has(slide.id)) continue
      const rowLabels = new Map()
      for (const block of slide.blocks || []) {
        if (block.columnIndex === 0 && block.rowIndex != null && cleanText(block.text)) {
          rowLabels.set(block.rowIndex, cleanText(block.text))
        }
      }
      ;(slide.blocks || []).forEach((block, index) => {
        const text = cleanText(block.text)
        if (!text) return
        items.push({
          id: `${slide.id}:${block.id || index}`,
          kind: block.kind || 'shape',
          order: (slide.order * 10_000) + (block.order ?? index),
          text,
          role: block.role || headerRole(text),
          row_index: block.rowIndex ?? null,
          column_index: block.columnIndex ?? null,
          row_label: block.rowLabel || rowLabels.get(block.rowIndex) || null,
          slide_id: slide.id,
          section_id: null,
          sheet_id: null,
          sheet_name: null,
          range_a1: null,
          structured_data: block.structuredData ?? null,
          metadata: { slide_order: slide.order, ...(block.metadata || {}) },
        })
      })
    }
    return items
  }

  function activeSlideBlocks(doc = document) {
    const blocks = []
    const editorParagraphs = uniqueElements([
      ...doc.querySelectorAll('g[id^="editor-"][id*="-paragraph-"]'),
    ]).map((node, index) => {
      const text = cleanText([...node.querySelectorAll('text')]
        .map((part) => cleanText(part.textContent))
        .filter(Boolean)
        .join(' '))
      const rect = node.getBoundingClientRect()
      return {
        node,
        index,
        text,
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      }
    }).filter((item) => item.text)
    if (editorParagraphs.length) {
      const assignmentHeader = editorParagraphs.find((item) => item.text.toLowerCase() === 'assignments')
      const learningHeader = editorParagraphs.find((item) => item.text.toLowerCase().includes('learning activities'))
      const dayPattern = /^(?:m|mon|monday|t|tu|tue|tuesday|w|wed|wednesday|th|thu|thursday|f|fri|friday)(?:\s*[-–—/]\s*(?:m|mon|monday|t|tu|tue|tuesday|w|wed|wednesday|th|thu|thursday|f|fri|friday))?$/i
      const rowLabels = editorParagraphs.filter((item) => dayPattern.test(item.text)).sort((left, right) => left.y - right.y)
      const dayBoundary = rowLabels.length
        ? Math.max(...rowLabels.map((item) => item.x + item.width)) + 24
        : learningHeader?.x ? learningHeader.x - 100 : Number.NEGATIVE_INFINITY
      const assignmentBoundary = assignmentHeader && learningHeader
        ? ((learningHeader.x + learningHeader.width / 2) + (assignmentHeader.x + assignmentHeader.width / 2)) / 2
        : assignmentHeader?.x ? assignmentHeader.x - 60 : Number.POSITIVE_INFINITY
      editorParagraphs.forEach((item) => {
        let rowIndex = null
        let rowLabel = null
        for (let index = 0; index < rowLabels.length; index += 1) {
          const current = rowLabels[index]
          const next = rowLabels[index + 1]
          if (item.y >= current.y - 4 && (!next || item.y < next.y - 4)) {
            rowIndex = index + 1
            rowLabel = current.text
            break
          }
        }
        const isHeader = item === learningHeader || item === assignmentHeader
        const columnIndex = isHeader
          ? item === assignmentHeader ? 2 : 1
          : item.x < dayBoundary ? 0 : item.x >= assignmentBoundary ? 2 : 1
        blocks.push({
          id: `${item.node.id || 'editor-text'}-${item.index}`,
          kind: rowIndex == null && !isHeader ? 'shape' : 'table_cell',
          order: item.index,
          text: item.text,
          rowIndex: isHeader ? 0 : rowIndex,
          columnIndex: isHeader ? columnIndex : rowIndex == null ? null : columnIndex,
          rowLabel,
          role: isHeader || rowIndex == null ? 'header' : columnIndex === 2 ? 'assignments' : columnIndex === 1 ? 'learning' : 'day',
          metadata: {
            geometry: { x: item.x, y: item.y, width: item.width, height: item.height },
            editor_object_id: item.node.id.replace(/^editor-/, '').replace(/-paragraph-\d+$/, ''),
          },
        })
      })
      return blocks
    }
    const cells = uniqueElements([
      ...doc.querySelectorAll('[role="gridcell"]'),
      ...doc.querySelectorAll('.punch-table-cell'),
    ])
    cells.forEach((cell, index) => {
      const text = elementText(cell)
      if (!text) return
      const rowIndex = Number(cell.getAttribute('aria-rowindex') || cell.dataset?.rowIndex || 1) - 1
      const columnIndex = Number(cell.getAttribute('aria-colindex') || cell.dataset?.columnIndex || 1) - 1
      blocks.push({
        id: cell.id || `cell-r${rowIndex}-c${columnIndex}`,
        kind: 'table_cell',
        order: index,
        text,
        rowIndex,
        columnIndex,
        role: rowIndex === 0 ? 'header' : undefined,
      })
    })
    if (blocks.length) return blocks

    const textNodes = uniqueElements([
      ...doc.querySelectorAll('[role="textbox"]'),
      ...doc.querySelectorAll('svg text'),
      ...doc.querySelectorAll('.sketchy-text-box'),
      ...doc.querySelectorAll('.punch-viewer-content text'),
    ])
    textNodes.forEach((node, index) => {
      const text = elementText(node)
      if (!text || /^slide \d+$/i.test(text)) return
      blocks.push({
        id: node.id || `text-${index}-${stablePart(text.slice(0, 40))}`,
        kind: 'shape',
        order: index,
        text,
        role: headerRole(text),
      })
    })
    return blocks
  }

  async function clickSlide(slideId, doc = document) {
    const current = scanSlides(doc).find((slide) => slide.id === slideId)
    const candidates = uniqueElements([
      ...doc.querySelectorAll(`[data-slide-id="${CSS.escape(slideId)}"]`),
      ...doc.querySelectorAll(`[data-page-id="${CSS.escape(slideId)}"]`),
      ...doc.querySelectorAll(`[data-slide-page-id="${CSS.escape(slideId)}"]`),
      ...doc.querySelectorAll(`[id="punch-filmstrip-thumbnail-${CSS.escape(slideId)}"]`),
      ...doc.querySelectorAll('[role="option"][aria-label*="Slide"]'),
      ...doc.querySelectorAll('.punch-filmstrip-page-thumbnail'),
      ...doc.querySelectorAll('.punch-filmstrip-thumbnail'),
    ])
    const node = candidates.find((candidate, index) => {
      const candidateId = candidate.dataset?.slideId || candidate.dataset?.pageId || candidate.dataset?.slidePageId || candidate.id?.replace(/^punch-filmstrip-thumbnail-/, '')
      return candidateId === slideId || (!candidateId && current?.order === index)
    })
    if (!node) {
      if (slideId === slideIdFromUrl(location.href) || slideId === 'current-slide') return
      throw new Error(`Slide ${slideId} is not currently available in the filmstrip.`)
    }
    node.scrollIntoView({ block: 'nearest' })
    if (typeof node.click === 'function') node.click()
    else node.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await sleep(450)
  }

  const adapter = {
    id: 'google-slides-v1',
    sourceType: 'google_slides',
    matches(url) {
      try { return sourceInfo(url).sourceType === 'google_slides' } catch { return false }
    },
    normalize,
    async discover(context = {}) {
      await ensureReady(context.document || document)
      const info = sourceInfo(context.url || location.href)
      const slides = scanSlides(context.document || document, context.url || location.href)
      return {
        adapterId: this.id,
        ...info,
        url: context.url || location.href,
        title: cleanText((context.document || document).title).replace(/\s+-\s+Google Slides.*$/i, ''),
        readyState: (context.document || document).readyState,
        targets: slides.map(({ id, label, order, current }) => ({ id, label, order, current })),
        selectionKind: 'slides',
        warnings: slides.length === 1 && slides[0].id === 'current-slide'
          ? ['The filmstrip was not readable; only the current slide can be selected.']
          : [],
      }
    },
    async acquireText({ selection = {}, document: doc = document } = {}) {
      const slides = scanSlides(doc)
      const selectedIds = selection.slideIds?.length
        ? selection.slideIds
        : slides.filter((slide) => slide.current).map((slide) => slide.id)
      const original = slides.find((slide) => slide.current)?.id
      const raw = { slides: [] }
      try {
        for (const slideId of selectedIds) {
          await clickSlide(slideId, doc)
          const slide = scanSlides(doc).find((item) => item.id === slideId) || { id: slideId, order: raw.slides.length }
          const blocks = activeSlideBlocks(doc)
          if (!blocks.length) throw new Error(`No text was exposed for ${slide.label || slideId}.`)
          raw.slides.push({ ...slide, blocks })
        }
      } finally {
        if (original && !selectedIds.includes(original)) await clickSlide(original, doc).catch(() => {})
      }
      return {
        items: normalize(raw, { slideIds: selectedIds }),
        metadata: { slide_ids: selectedIds, slide_count: selectedIds.length },
        warnings: [],
      }
    },
    async screenshotTargets({ selection = {}, document: doc = document } = {}) {
      const slides = scanSlides(doc)
      const selected = selection.slideIds?.length
        ? new Set(selection.slideIds)
        : new Set(slides.filter((slide) => slide.current).map((slide) => slide.id))
      return slides.filter((slide) => selected.has(slide.id)).map((slide) => ({
        id: slide.id,
        itemId: `${slide.id}:visual`,
        label: slide.label,
        order: slide.order,
        metadata: { slide_id: slide.id, slide_order: slide.order },
      }))
    },
    async showTarget(target, context = {}) {
      await clickSlide(target.id, context.document || document)
    },
  }
  connector.register(adapter)
})()
