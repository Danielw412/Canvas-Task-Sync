export class AcquisitionError extends Error {
  constructor(code, message, causes = []) {
    super(message)
    this.name = 'AcquisitionError'
    this.code = code
    this.causes = causes
  }
}

function failureMessage(method, error) {
  const detail = error instanceof Error ? error.message : String(error)
  return `${method === 'text' ? 'Text extraction' : 'Screenshot capture'} failed: ${detail}`
}

async function attempt(method, operations) {
  try {
    const value = await operations[method]()
    if (!value) throw new Error('The page returned no content.')
    return { method, value }
  } catch (error) {
    throw new AcquisitionError(`${method}_failed`, failureMessage(method, error), [error])
  }
}

export async function acquireWithMode(mode, operations) {
  if (mode === 'text' || mode === 'screenshot') {
    const result = await attempt(mode, operations)
    return { results: { [mode]: result.value }, methodsUsed: [mode], fallbackUsed: false, warnings: [] }
  }

  if (mode === 'both') {
    // Source adapters navigate slides, sections, and tabs. Run the methods in order so
    // screenshot navigation cannot race the text extractor on the same active page.
    const values = []
    const failures = []
    for (const method of ['text', 'screenshot']) {
      try {
        values.push(await attempt(method, operations))
      } catch (error) {
        failures.push(error)
      }
    }
    if (failures.length) {
      throw new AcquisitionError(
        'both_methods_required',
        failures.map((error) => error.message).join(' '),
        failures,
      )
    }
    return {
      results: Object.fromEntries(values.map((item) => [item.method, item.value])),
      methodsUsed: ['text', 'screenshot'],
      fallbackUsed: false,
      warnings: [],
    }
  }

  const preferred = mode === 'prefer_screenshot' ? 'screenshot' : 'text'
  const fallback = preferred === 'screenshot' ? 'text' : 'screenshot'
  try {
    const result = await attempt(preferred, operations)
    return {
      results: { [preferred]: result.value },
      methodsUsed: [preferred],
      fallbackUsed: false,
      warnings: [],
    }
  } catch (preferredError) {
    try {
      const result = await attempt(fallback, operations)
      return {
        results: { [fallback]: result.value },
        methodsUsed: [fallback],
        fallbackUsed: true,
        warnings: [preferredError.message],
      }
    } catch (fallbackError) {
      throw new AcquisitionError(
        'preferred_and_fallback_failed',
        `${preferredError.message} ${fallbackError.message}`,
        [preferredError, fallbackError],
      )
    }
  }
}
