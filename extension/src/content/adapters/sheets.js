(() => {
  const connector = globalThis.CanvasTaskSyncConnector
  if (!connector || connector.adapters.some((item) => item.id === 'google-sheets-v1')) return
  const { cleanText, elementText, ensureReady, headerRole, sleep, sourceInfo, uniqueElements } = connector.utils
  const MAX_CELLS = 10_000

  function currentGid(url = location.href) {
    const parsed = new URL(url)
    return parsed.searchParams.get('gid') || new URLSearchParams(parsed.hash.replace(/^#/, '')).get('gid') || '0'
  }

  function scanSheetTabs(doc = document, url = location.href) {
    const nodes = uniqueElements([
      ...doc.querySelectorAll('.docs-sheet-tab'),
      ...doc.querySelectorAll('[role="tab"][data-gid], [role="tab"][data-sheet-id]'),
      ...doc.querySelectorAll('[data-gid].docs-sheet-tab, [data-sheet-id].docs-sheet-tab'),
    ])
    const selectedGid = currentGid(url)
    const seen = new Set()
    const sheets = []
    nodes.forEach((node, index) => {
      const href = node.querySelector?.('a[href*="gid="]')?.href || node.getAttribute?.('href') || ''
      let hrefGid = null
      try { hrefGid = href ? currentGid(href) : null } catch { hrefGid = null }
      const active = node.classList?.contains('docs-sheet-active-tab')
        || node.getAttribute?.('aria-selected') === 'true'
      const id = String(node.dataset?.gid || node.dataset?.sheetId || hrefGid || '')
      const name = elementText(node) || node.getAttribute?.('aria-label') || `Sheet ${index + 1}`
      const normalizedId = id || (active ? selectedGid : `tab-${index}`)
      if (seen.has(normalizedId)) return
      seen.add(normalizedId)
      sheets.push({
        id: normalizedId,
        name: cleanText(name),
        order: index,
        current: active || normalizedId === selectedGid,
      })
    })
    if (!sheets.length) sheets.push({ id: selectedGid, name: 'Current sheet', order: 0, current: true })
    return sheets
  }

  function parseCsv(value) {
    const rows = []
    let row = []
    let field = ''
    let quoted = false
    for (let index = 0; index < value.length; index += 1) {
      const character = value[index]
      if (quoted) {
        if (character === '"' && value[index + 1] === '"') {
          field += '"'
          index += 1
        } else if (character === '"') {
          quoted = false
        } else {
          field += character
        }
      } else if (character === '"') {
        quoted = true
      } else if (character === ',') {
        row.push(field)
        field = ''
      } else if (character === '\n') {
        row.push(field.replace(/\r$/, ''))
        rows.push(row)
        row = []
        field = ''
      } else {
        field += character
      }
    }
    if (field || row.length) {
      row.push(field.replace(/\r$/, ''))
      rows.push(row)
    }
    return rows
  }

  function columnNumber(label) {
    let result = 0
    for (const character of label.toUpperCase()) result = (result * 26) + character.charCodeAt(0) - 64
    return result
  }

  function columnLabel(index) {
    let value = index + 1
    let result = ''
    while (value > 0) {
      value -= 1
      result = String.fromCharCode(65 + (value % 26)) + result
      value = Math.floor(value / 26)
    }
    return result
  }

  function rangeOrigin(rangeA1 = '') {
    const match = String(rangeA1).match(/(?:^[^!]+!)?\$?([A-Za-z]+)\$?(\d+)/)
    return match ? { column: columnNumber(match[1]) - 1, row: Number(match[2]) - 1 } : { column: 0, row: 0 }
  }

  function normalize(raw, selection = {}) {
    const selectedSheets = selection.sheets || []
    const items = []
    for (const sheet of [...raw.sheets].sort((a, b) => a.order - b.order)) {
      const selected = selectedSheets.find((item) => item.id === sheet.id || item.name === sheet.name)
      if (selectedSheets.length && !selected) continue
      const requestedRange = selected?.range || sheet.range || ''
      const origin = rangeOrigin(requestedRange)
      const headers = sheet.rows?.[0] || []
      let cellCount = 0
      for (let rowIndex = 0; rowIndex < (sheet.rows || []).length; rowIndex += 1) {
        const row = sheet.rows[rowIndex]
        const rowLabel = cleanText(row[0]) || null
        for (let columnIndex = 0; columnIndex < row.length; columnIndex += 1) {
          const text = cleanText(row[columnIndex])
          if (!text) continue
          cellCount += 1
          if (cellCount > MAX_CELLS) throw new Error(`Sheet ${sheet.name} exceeds the ${MAX_CELLS}-cell capture limit. Select a smaller range.`)
          const absoluteRow = origin.row + rowIndex
          const absoluteColumn = origin.column + columnIndex
          const address = `${columnLabel(absoluteColumn)}${absoluteRow + 1}`
          const header = cleanText(headers[columnIndex])
          const role = rowIndex === 0
            ? 'header'
            : columnIndex === 0
              ? 'day'
              : headerRole(header)
          items.push({
            id: `${sheet.id}:${address}`,
            kind: 'cell',
            order: (sheet.order * 1_000_000) + (absoluteRow * 1_000) + absoluteColumn,
            text,
            role,
            row_index: absoluteRow,
            column_index: absoluteColumn,
            row_label: rowIndex === 0 ? null : rowLabel,
            slide_id: null,
            section_id: null,
            sheet_id: String(sheet.id),
            sheet_name: sheet.name,
            range_a1: address,
            structured_data: { row: absoluteRow, column: absoluteColumn, address, value: text },
            metadata: {
              sheet_order: sheet.order,
              column_header: header || null,
              selection_range: requestedRange || null,
            },
          })
        }
      }
    }
    return items
  }

  async function clickSheet(sheetId, doc = document) {
    const sheets = scanSheetTabs(doc)
    const sheet = sheets.find((item) => item.id === String(sheetId))
    const nodes = uniqueElements([
      ...doc.querySelectorAll(`[data-gid="${CSS.escape(String(sheetId))}"]`),
      ...doc.querySelectorAll(`[data-sheet-id="${CSS.escape(String(sheetId))}"]`),
      ...doc.querySelectorAll('.docs-sheet-tab'),
      ...doc.querySelectorAll('[role="tab"][data-gid], [role="tab"][data-sheet-id]'),
    ])
    const node = nodes.find((candidate, index) => {
      const id = String(candidate.dataset?.gid || candidate.dataset?.sheetId || '')
      return id === String(sheetId) || (!id && sheet?.order === index)
    })
    if (!node) {
      if (String(sheetId) === currentGid()) return currentGid()
      throw new Error(`Sheet ${sheetId} is not currently available in the tab bar.`)
    }
    node.scrollIntoView({ inline: 'nearest' })
    node.click()
    await sleep(350)
    return currentGid()
  }

  async function showRange(rangeA1, doc = document) {
    const range = cleanText(rangeA1)
    if (!range) return
    const nameBox = doc.querySelector('#t-name-box, input[aria-label^="Name box"], .waffle-name-box')
    if (!nameBox) throw new Error(`Range ${range} could not be shown because the Sheets name box is unavailable.`)
    nameBox.focus()
    if ('value' in nameBox) {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
      if (setter) setter.call(nameBox, range)
      else nameBox.value = range
    } else {
      nameBox.textContent = range
    }
    nameBox.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: range }))
    nameBox.dispatchEvent(new Event('change', { bubbles: true }))
    for (const type of ['keydown', 'keypress', 'keyup']) {
      nameBox.dispatchEvent(new KeyboardEvent(type, {
        bubbles: true,
        cancelable: true,
        key: 'Enter',
        code: 'Enter',
        keyCode: 13,
        which: 13,
      }))
    }
    await sleep(400)
  }

  function gridRows(doc = document) {
    const cells = uniqueElements([
      ...doc.querySelectorAll('[role="gridcell"]'),
      ...doc.querySelectorAll('[data-row][data-column]'),
    ])
    const matrix = []
    cells.forEach((cell) => {
      const row = Math.max(0, Number(cell.getAttribute?.('aria-rowindex') || cell.dataset?.row || 1) - 1)
      const column = Math.max(0, Number(cell.getAttribute?.('aria-colindex') || cell.dataset?.column || 1) - 1)
      matrix[row] ||= []
      matrix[row][column] = elementText(cell)
    })
    return matrix.map((row) => row || [])
  }

  async function fetchSheetRows(resourceId, sheetId, range) {
    const url = new URL(`/spreadsheets/d/${resourceId}/gviz/tq`, location.origin)
    url.searchParams.set('tqx', 'out:csv')
    url.searchParams.set('gid', sheetId)
    if (range) url.searchParams.set('range', range)
    const response = await fetch(url, { credentials: 'include', redirect: 'follow' })
    if (!response.ok || /\/accounts\//.test(response.url)) {
      throw new Error(`Google Sheets export returned ${response.status || 'a sign-in page'}.`)
    }
    const csv = await response.text()
    const rows = parseCsv(csv)
    if (!rows.some((row) => row.some((cell) => cleanText(cell)))) throw new Error('The selected sheet or range is blank.')
    return rows
  }

  const adapter = {
    id: 'google-sheets-v1',
    sourceType: 'google_sheets',
    matches(url) {
      try { return sourceInfo(url).sourceType === 'google_sheets' } catch { return false }
    },
    normalize,
    parseCsv,
    async discover(context = {}) {
      const doc = context.document || document
      await ensureReady(doc)
      const info = sourceInfo(context.url || location.href)
      const sheets = scanSheetTabs(doc, context.url || location.href)
      return {
        adapterId: this.id,
        ...info,
        url: context.url || location.href,
        title: cleanText(doc.title).replace(/\s+-\s+Google Sheets.*$/i, ''),
        readyState: doc.readyState,
        targets: sheets.map(({ id, name, order, current }) => ({ id, label: name, order, current })),
        selectionKind: 'sheets',
        warnings: sheets.length === 1 && sheets[0].name === 'Current sheet'
          ? ['The sheet tab bar was not readable; only the current tab can be selected.']
          : [],
      }
    },
    async acquireText({ selection = {}, document: doc = document } = {}) {
      const info = sourceInfo(location.href)
      const available = scanSheetTabs(doc)
      const selected = selection.sheets?.length
        ? selection.sheets
        : available.filter((sheet) => sheet.current).map((sheet) => ({ id: sheet.id, name: sheet.name, range: '' }))
      const raw = { sheets: [] }
      const resolvedSelections = []
      const warnings = []
      for (const choice of selected) {
        const sheet = available.find((item) => item.id === String(choice.id) || item.name === choice.name)
        if (!sheet) throw new Error(`Selected sheet ${choice.name || choice.id} is unavailable.`)
        const resolvedSheetId = await clickSheet(sheet.id, doc)
        let rows
        try {
          rows = await fetchSheetRows(info.resourceId, resolvedSheetId, choice.range)
        } catch (error) {
          rows = gridRows(doc)
          if (!rows.length) throw error
          warnings.push(`CSV extraction failed for ${sheet.name}; used the visible grid instead.`)
        }
        raw.sheets.push({ ...sheet, id: resolvedSheetId, range: choice.range || '', rows })
        resolvedSelections.push({ id: resolvedSheetId, name: sheet.name, range: choice.range || '' })
      }
      const items = normalize(raw, { sheets: resolvedSelections })
      if (!items.length) throw new Error('The selected sheet tabs or ranges contain no readable cells.')
      return {
        items,
        metadata: {
          sheets: resolvedSelections,
          sheet_count: resolvedSelections.length,
          cell_count: items.length,
        },
        warnings,
      }
    },
    async screenshotTargets({ selection = {}, document: doc = document } = {}) {
      const available = scanSheetTabs(doc)
      const selected = selection.sheets?.length
        ? selection.sheets
        : available.filter((sheet) => sheet.current).map((sheet) => ({ id: sheet.id, name: sheet.name, range: '' }))
      return selected.map((choice, index) => {
        const sheet = available.find((item) => item.id === String(choice.id) || item.name === choice.name)
        if (!sheet) throw new Error(`Selected sheet ${choice.name || choice.id} is unavailable.`)
        return {
          id: sheet.id,
          itemId: `${sheet.id}:${choice.range || 'visible'}:visual`,
          label: sheet.name,
          order: sheet.order ?? index,
          metadata: {
            sheet_id: String(sheet.id),
            sheet_name: sheet.name,
            range_a1: choice.range || null,
          },
        }
      })
    },
    async showTarget(target, context = {}) {
      const doc = context.document || document
      const resolvedSheetId = await clickSheet(target.id, doc)
      await showRange(target.metadata?.range_a1, doc)
      return { metadata: { sheet_id: resolvedSheetId } }
    },
  }
  connector.register(adapter)
})()
