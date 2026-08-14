(() => {
  const connector = globalThis.CanvasTaskSyncConnector
  if (!connector || connector.adapters.some((item) => item.id === 'google-docs-v1')) return
  const { cleanText, elementText, ensureReady, headerRole, sleep, sourceInfo, stablePart, uniqueElements } = connector.utils

  function headingLevel(element) {
    const aria = Number(element.getAttribute?.('aria-level'))
    if (aria > 0) return aria
    const tag = String(element.tagName || '').toLowerCase()
    return /^h[1-6]$/.test(tag) ? Number(tag.slice(1)) : 2
  }

  function sectionId(text, index) {
    return `section-${index}-${stablePart(text.toLowerCase(), 'untitled')}`
  }

  function headingNodes(doc = document) {
    const editor = doc.querySelector('.kix-appview-editor, .docs-editor-container, [data-document-editor]')
    const rendered = editor ? uniqueElements([
      ...editor.querySelectorAll('h1, h2, h3, h4, h5, h6'),
      ...editor.querySelectorAll('[role="heading"]'),
      ...editor.querySelectorAll('[data-heading-id]'),
    ]) : []
    if (rendered.length) return rendered
    return uniqueElements([
      ...doc.querySelectorAll('.navigation-item[role="treeitem"], .navigation-item-content'),
      ...doc.querySelectorAll('[class*="outline-item"][role="treeitem"]'),
    ]).filter((node) => {
      const text = elementText(node)
      return text && !/headings you add|document tabs?|outline/i.test(text)
        && !node.closest('.navigation-widget-empty')
    })
  }

  function scanHeadings(doc = document) {
    const nodes = headingNodes(doc)
    return nodes.map((node, index) => ({
      id: node.dataset?.headingId || sectionId(elementText(node), index + 1),
      label: elementText(node) || `Section ${index + 1}`,
      order: index,
      level: headingLevel(node),
    })).filter((item) => item.label)
  }

  function normalize(raw, selection = {}) {
    const selected = new Set(selection.sectionIds || [])
    const items = []
    for (const section of [...raw.sections].sort((a, b) => a.order - b.order)) {
      if (selected.size && !selected.has(section.id)) continue
      ;(section.blocks || []).forEach((block, index) => {
        const text = cleanText(block.text)
        if (!text && block.structuredData == null) return
        items.push({
          id: `${section.id}:${block.id || index}`,
          kind: block.kind || 'paragraph',
          order: (section.order * 10_000) + (block.order ?? index),
          text,
          role: block.role || (block.kind === 'heading' ? 'header' : 'unknown'),
          row_index: block.rowIndex ?? null,
          column_index: block.columnIndex ?? null,
          row_label: block.rowLabel || null,
          slide_id: null,
          section_id: section.id,
          sheet_id: null,
          sheet_name: null,
          range_a1: null,
          structured_data: block.structuredData ?? null,
          metadata: {
            section_title: section.label,
            heading_level: section.level || null,
            ...(block.metadata || {}),
          },
        })
      })
    }
    return items
  }

  function zipEntries(buffer) {
    const view = new DataView(buffer)
    let end = view.byteLength - 22
    const minimum = Math.max(0, view.byteLength - 65_557)
    while (end >= minimum && view.getUint32(end, true) !== 0x06054b50) end -= 1
    if (end < minimum) throw new Error('The exported DOCX does not contain a readable ZIP directory.')
    const count = view.getUint16(end + 10, true)
    let offset = view.getUint32(end + 16, true)
    const entries = new Map()
    for (let index = 0; index < count; index += 1) {
      if (view.getUint32(offset, true) !== 0x02014b50) throw new Error('The DOCX ZIP directory is invalid.')
      const compression = view.getUint16(offset + 10, true)
      const compressedSize = view.getUint32(offset + 20, true)
      const nameLength = view.getUint16(offset + 28, true)
      const extraLength = view.getUint16(offset + 30, true)
      const commentLength = view.getUint16(offset + 32, true)
      const localOffset = view.getUint32(offset + 42, true)
      const name = new TextDecoder().decode(new Uint8Array(buffer, offset + 46, nameLength))
      entries.set(name, { compression, compressedSize, localOffset })
      offset += 46 + nameLength + extraLength + commentLength
    }
    return entries
  }

  async function unzipText(buffer, filename) {
    const entry = zipEntries(buffer).get(filename)
    if (!entry) return null
    const view = new DataView(buffer)
    if (view.getUint32(entry.localOffset, true) !== 0x04034b50) throw new Error('The DOCX file entry is invalid.')
    const nameLength = view.getUint16(entry.localOffset + 26, true)
    const extraLength = view.getUint16(entry.localOffset + 28, true)
    const start = entry.localOffset + 30 + nameLength + extraLength
    const bytes = new Uint8Array(buffer, start, entry.compressedSize)
    let output
    if (entry.compression === 0) output = bytes
    else if (entry.compression === 8 && typeof DecompressionStream === 'function') {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw'))
      output = new Uint8Array(await new Response(stream).arrayBuffer())
    } else {
      throw new Error('This browser cannot decompress the exported DOCX content.')
    }
    return new TextDecoder().decode(output)
  }

  function localElements(node, name) {
    return [...node.getElementsByTagNameNS('*', name)]
  }

  function localAttribute(node, name) {
    if (!node?.attributes) return ''
    const attribute = [...node.attributes].find((item) => item.localName === name)
    return attribute?.value || ''
  }

  function paragraphText(paragraph) {
    const pieces = []
    for (const node of paragraph.getElementsByTagName('*')) {
      if (node.localName === 't') pieces.push(node.textContent || '')
      else if (node.localName === 'tab') pieces.push('\t')
      else if (node.localName === 'br' || node.localName === 'cr') pieces.push('\n')
    }
    return cleanText(pieces.join(''))
  }

  function styleMap(stylesXml) {
    if (!stylesXml) return new Map()
    const xml = new DOMParser().parseFromString(stylesXml, 'application/xml')
    return new Map(localElements(xml, 'style').map((style) => {
      const name = localElements(style, 'name')[0]
      return [localAttribute(style, 'styleId'), localAttribute(name, 'val')]
    }))
  }

  function parseDocxStructure(documentXml, stylesXml) {
    const xml = new DOMParser().parseFromString(documentXml, 'application/xml')
    if (xml.querySelector('parsererror')) throw new Error('The exported DOCX XML is invalid.')
    const styles = styleMap(stylesXml)
    const body = localElements(xml, 'body')[0]
    if (!body) throw new Error('The exported document has no body content.')
    const sections = []
    let current = { id: 'document-start', label: 'Document start', order: 0, level: 1, blocks: [] }
    sections.push(current)
    let blockOrder = 0
    for (const child of [...body.children]) {
      if (child.localName === 'p') {
        const text = paragraphText(child)
        if (!text) continue
        const styleNode = localElements(child, 'pStyle')[0]
        const styleId = localAttribute(styleNode, 'val')
        const styleName = styles.get(styleId) || styleId
        const isHeading = /^(?:heading|title)/i.test(styleName)
        if (isHeading) {
          const match = styleName.match(/(\d+)/)
          current = {
            id: sectionId(text, sections.length),
            label: text,
            order: sections.length,
            level: match ? Number(match[1]) : 1,
            blocks: [],
          }
          sections.push(current)
        }
        current.blocks.push({
          id: `${isHeading ? 'heading' : 'paragraph'}-${blockOrder}`,
          kind: isHeading ? 'heading' : 'paragraph',
          order: blockOrder,
          text,
          role: isHeading ? 'header' : 'unknown',
          metadata: { paragraph_style: styleName || null },
        })
        blockOrder += 1
      } else if (child.localName === 'tbl') {
        const rows = localElements(child, 'tr').filter((row) => row.parentElement === child || row.parentElement?.localName === 'tbl')
        const tableId = `table-${sections.length}-${blockOrder}`
        const headerCells = rows[0] ? [...rows[0].children].filter((cell) => cell.localName === 'tc') : []
        const columnRoles = headerCells.map((cell) => headerRole(cleanText(localElements(cell, 'p').map(paragraphText).filter(Boolean).join('\n'))))
        rows.forEach((row, rowIndex) => {
          const cells = [...row.children].filter((cell) => cell.localName === 'tc')
          const values = cells.map((cell) => cleanText(localElements(cell, 'p').map(paragraphText).filter(Boolean).join('\n')))
          cells.forEach((cell, columnIndex) => {
            const text = values[columnIndex]
            if (!text) return
            current.blocks.push({
              id: `table-${blockOrder}-r${rowIndex}-c${columnIndex}`,
              kind: 'table_cell',
              order: blockOrder,
              text,
              role: rowIndex === 0 ? 'header' : columnIndex === 0 ? 'day' : columnRoles[columnIndex] || 'unknown',
              rowIndex,
              columnIndex,
              rowLabel: values[0] || null,
              structuredData: { row: rowIndex, column: columnIndex, value: text },
              metadata: { table_id: tableId },
            })
            blockOrder += 1
          })
        })
      }
    }
    return sections.filter((section) => section.blocks.length)
  }

  let cachedExport = null
  function exportUrl(resourceId, format) {
    const account = location.pathname.match(/^\/document(\/u\/\d+)?\/d\//)?.[1] || ''
    return new URL(`/document${account}/d/${resourceId}/export?format=${format}`, location.origin)
  }

  async function exportedDocument(resourceId) {
    if (cachedExport?.resourceId === resourceId && Date.now() - cachedExport.createdAt < 30_000) {
      return cachedExport.sections
    }
    const response = await fetch(exportUrl(resourceId, 'docx'), {
      credentials: 'include',
      redirect: 'follow',
      cache: 'no-store',
    })
    if (!response.ok || /\/accounts\//.test(response.url)) {
      throw new Error(`Google Docs export returned ${response.status || 'a sign-in page'}.`)
    }
    const buffer = await response.arrayBuffer()
    if (buffer.byteLength > 25 * 1024 * 1024) throw new Error('The document export exceeds the 25 MB extraction limit.')
    const documentXml = await unzipText(buffer, 'word/document.xml')
    const stylesXml = await unzipText(buffer, 'word/styles.xml')
    if (!documentXml) throw new Error('The exported DOCX is missing document.xml.')
    const sections = parseDocxStructure(documentXml, stylesXml)
    cachedExport = { resourceId, createdAt: Date.now(), sections }
    return sections
  }

  async function exportedPlainText(resourceId) {
    const response = await fetch(exportUrl(resourceId, 'txt'), {
      credentials: 'include',
      redirect: 'follow',
      cache: 'no-store',
    })
    if (!response.ok || /\/accounts\//.test(response.url)) {
      throw new Error(`Google Docs text export returned ${response.status || 'a sign-in page'}.`)
    }
    const lines = (await response.text()).split(/\r?\n/).map(cleanText).filter(Boolean)
    if (!lines.length) throw new Error('The Google Docs text export was empty.')
    return [{
      id: 'document-start',
      label: 'Entire document',
      order: 0,
      level: 1,
      blocks: lines.map((text, order) => ({
        id: `text-line-${order}`,
        kind: 'paragraph',
        order,
        text,
        role: 'unknown',
        metadata: { extraction_fallback: 'plain_text_export' },
      })),
    }]
  }

  function tableBlocks(table, startOrder = 0) {
    const rows = [...table.querySelectorAll('tr')]
    const blocks = []
    const tableId = table.id || `rendered-table-${startOrder}`
    const columnRoles = rows[0]
      ? [...rows[0].querySelectorAll(':scope > th, :scope > td')].map((cell) => headerRole(elementText(cell)))
      : []
    rows.forEach((row, rowIndex) => {
      const cells = [...row.querySelectorAll(':scope > th, :scope > td')]
      const rowLabel = elementText(cells[0]) || null
      cells.forEach((cell, columnIndex) => {
        const text = elementText(cell)
        if (!text) return
        blocks.push({
          id: cell.id || `table-cell-r${rowIndex}-c${columnIndex}`,
          kind: 'table_cell',
          order: startOrder + blocks.length,
          text,
          role: rowIndex === 0 ? 'header' : columnIndex === 0 ? 'day' : columnRoles[columnIndex] || 'unknown',
          rowIndex,
          columnIndex,
          rowLabel,
          structuredData: { row: rowIndex, column: columnIndex, value: text },
          metadata: { table_id: tableId },
        })
      })
    })
    return blocks
  }

  function collectVisibleBlocks(doc = document) {
    const editor = doc.querySelector('.kix-appview-editor, .docs-editor-container, [data-document-editor]') || doc
    const nodes = uniqueElements([
      ...editor.querySelectorAll('h1, h2, h3, h4, h5, h6, [role="heading"]'),
      ...editor.querySelectorAll('table'),
      ...editor.querySelectorAll('p, [role="paragraph"], .kix-lineview-text-block'),
    ]).filter((node) => !node.closest('.docs-toolbar, .navigation-widget, .docs-sidebar'))
    const sections = []
    let current = { id: 'document-start', label: 'Document start', order: 0, level: 1, blocks: [] }
    sections.push(current)
    const seenText = new Set()
    nodes.forEach((node, index) => {
      if (node.matches?.('table')) {
        current.blocks.push(...tableBlocks(node, current.blocks.length))
        return
      }
      if (node.closest?.('table')) return
      const text = elementText(node)
      if (!text || seenText.has(`${current.id}:${text}`)) return
      const isHeading = node.matches?.('h1, h2, h3, h4, h5, h6, [role="heading"]')
      if (isHeading) {
        current = {
          id: node.dataset?.headingId || sectionId(text, sections.length),
          label: text,
          order: sections.length,
          level: headingLevel(node),
          blocks: [],
        }
        sections.push(current)
      }
      current.blocks.push({
        id: node.id || `${isHeading ? 'heading' : 'paragraph'}-${index}-${stablePart(text.slice(0, 40))}`,
        kind: isHeading ? 'heading' : 'paragraph',
        order: current.blocks.length,
        text,
        role: isHeading ? 'header' : 'unknown',
      })
      seenText.add(`${current.id}:${text}`)
    })
    return sections.filter((section) => section.blocks.length)
  }

  function documentScroller(doc = document) {
    return doc.querySelector('.kix-appview-editor, .docs-scrollable-container, [role="main"]')
      || doc.scrollingElement
      || doc.documentElement
  }

  function documentViewportTargets(doc = document) {
    const scroller = documentScroller(doc)
    const viewport = Math.max(scroller.clientHeight || innerHeight || 800, 400)
    const maximum = Math.max(scroller.scrollHeight || viewport, viewport)
    const step = Math.max(Math.floor(viewport * 0.82), 320)
    const targets = []
    for (let top = 0, order = 0; top < maximum && order < 30; top += step, order += 1) {
      const scrollTop = Math.min(top, Math.max(0, maximum - viewport))
      if (targets.some((target) => target.scrollTop === scrollTop)) break
      targets.push({
        id: `document-viewport-${order + 1}`,
        itemId: `document-start:viewport-${order + 1}:visual`,
        label: `Document viewport ${order + 1}`,
        order,
        scrollTop,
        metadata: {
          section_id: 'document-start',
          capture_scope: 'document_viewport',
          viewport_index: order,
        },
      })
    }
    return targets
  }

  async function collectLongDocument(doc = document) {
    const scroller = documentScroller(doc)
    const original = scroller.scrollTop
    const snapshots = new Map()
    const viewport = Math.max(scroller.clientHeight || innerHeight || 800, 400)
    const maximum = Math.max(scroller.scrollHeight || viewport, viewport)
    try {
      for (let top = 0, steps = 0; top <= maximum && steps < 200; top += viewport * 0.8, steps += 1) {
        scroller.scrollTop = top
        await sleep(80)
        for (const section of collectVisibleBlocks(doc)) {
          const existing = snapshots.get(section.id) || { ...section, blocks: [] }
          const ids = new Set(existing.blocks.map((block) => block.id))
          for (const block of section.blocks) if (!ids.has(block.id)) existing.blocks.push(block)
          snapshots.set(section.id, existing)
        }
        if (top + viewport >= maximum) break
      }
    } finally {
      scroller.scrollTop = original
    }
    return [...snapshots.values()].sort((left, right) => left.order - right.order)
  }

  async function scrollToSection(sectionId, doc = document) {
    const headings = scanHeadings(doc)
    const targetIndex = headings.findIndex((heading) => heading.id === sectionId)
    const nodes = headingNodes(doc)
    const node = nodes[targetIndex]
    if (!node) {
      if (sectionId === 'document-start') documentScroller(doc).scrollTop = 0
      else throw new Error(`Section ${sectionId} is not currently rendered.`)
    } else if (node.closest('.navigation-widget')) {
      node.click()
    } else {
      node.scrollIntoView({ block: 'start' })
    }
    await sleep(300)
  }

  const adapter = {
    id: 'google-docs-v1',
    sourceType: 'google_docs',
    matches(url) {
      try { return sourceInfo(url).sourceType === 'google_docs' } catch { return false }
    },
    normalize,
    async discover(context = {}) {
      const doc = context.document || document
      await ensureReady(doc)
      const info = sourceInfo(context.url || location.href)
      let sections = []
      const warnings = []
      try {
        sections = await exportedDocument(info.resourceId)
      } catch (error) {
        warnings.push(`Structured document export was unavailable: ${error.message}`)
      }
      const headings = sections.filter((section) => section.id !== 'document-start')
      const domHeadings = headings.length ? [] : scanHeadings(doc)
      const navigableHeadings = scanHeadings(doc)
      const targets = headings.length
        ? headings.map(({ id, label, order }) => ({ id, label, order, current: false }))
        : domHeadings.length
          ? domHeadings.map(({ id, label, order }) => ({ id, label, order, current: false }))
        : [{ id: 'document-start', label: 'Entire document', order: 0, current: true }]
      return {
        adapterId: this.id,
        ...info,
        url: context.url || location.href,
        title: cleanText(doc.title).replace(/\s+-\s+Google Docs.*$/i, ''),
        readyState: doc.readyState,
        targets,
        selectionKind: 'sections',
        warnings: [
          ...warnings,
          ...(headings.length && !navigableHeadings.length
            ? ['Heading-level text selection is available; screenshots cover document viewports because this editor did not expose navigable headings.']
            : []),
          ...(targets.length === 1 && targets[0].id === 'document-start'
            ? ['No document headings were exposed; capture uses the entire document.']
            : []),
        ],
      }
    },
    async acquireText({ selection = {}, document: doc = document } = {}) {
      const info = sourceInfo(location.href)
      let sections
      const warnings = []
      try {
        sections = await exportedDocument(info.resourceId)
      } catch (error) {
        try {
          sections = await exportedPlainText(info.resourceId)
          warnings.push(`Structured DOCX extraction failed; used authenticated plain-text export: ${error.message}`)
        } catch (plainTextError) {
          sections = await collectLongDocument(doc)
          warnings.push(`Structured exports failed; used rendered DOM text: ${error.message}; ${plainTextError.message}`)
        }
        if (!sections.length) {
          throw new Error(`Google Docs exposed no readable content. Structured extraction failed: ${error.message}`)
        }
      }
      if (!sections.length) throw new Error('Google Docs exposed no readable paragraphs or tables.')
      const selectedIds = selection.sectionIds || []
      const items = normalize({ sections }, { sectionIds: selectedIds })
      if (!items.length) throw new Error('The selected document portions contain no readable content.')
      return {
        items,
        metadata: { section_ids: selectedIds, section_count: new Set(items.map((item) => item.section_id)).size },
        warnings,
      }
    },
    async screenshotTargets({ selection = {}, document: doc = document } = {}) {
      const headings = scanHeadings(doc)
      const selected = new Set(selection.sectionIds || [])
      if (!headings.length) return documentViewportTargets(doc)
      const targets = headings
      return targets.filter((target) => !selected.size || selected.has(target.id)).map((target) => ({
        id: target.id,
        itemId: `${target.id}:visual`,
        label: target.label,
        order: target.order,
        metadata: { section_id: target.id, section_title: target.label },
      }))
    },
    async showTarget(target, context = {}) {
      if (Number.isFinite(target.scrollTop)) {
        documentScroller(context.document || document).scrollTop = target.scrollTop
        await sleep(300)
        return
      }
      await scrollToSection(target.id, context.document || document)
    },
  }
  connector.register(adapter)
})()
