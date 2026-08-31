import useSWR from 'swr'
import type { ApiErrorShape, OverviewResponse, WeekSelection } from '../types'

const WEEK_OFFSETS: Record<WeekSelection, number> = {
  previous_week: -7,
  this_week: 0,
  next_week: 7,
}

const WEEK_NAMES: Record<WeekSelection, string> = {
  previous_week: 'Previous Week',
  this_week: 'This Week',
  next_week: 'Next Week',
}

export class ApiError extends Error {
  code: string
  status: number
  fieldErrors?: Record<string, string[]> | null

  constructor(status: number, payload: ApiErrorShape) {
    super(payload.error.message)
    this.name = 'ApiError'
    this.code = payload.error.code
    this.status = status
    this.fieldErrors = payload.error.field_errors
  }
}

let csrfToken: string | null = null
let bootstrapPromise: Promise<string> | null = null

export async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!response.ok) {
    throw await responseError(response)
  }
  return parseJsonResponse<T>(response)
}

async function csrf(): Promise<string> {
  if (csrfToken) return csrfToken
  bootstrapPromise ??= fetchJson<{ csrf_token: string }>('/api/v1/bootstrap').then((value) => {
    csrfToken = value.csrf_token
    return value.csrf_token
  })
  return bootstrapPromise
}

export async function mutateJson<T>(
  url: string,
  options: { method?: string; body?: unknown; formData?: FormData } = {},
): Promise<T> {
  const token = await csrf()
  const headers: Record<string, string> = { 'X-CSRF-Token': token, Accept: 'application/json' }
  let body: BodyInit | undefined
  if (options.formData) {
    body = options.formData
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }
  const response = await fetch(url, { method: options.method ?? 'POST', headers, body })
  if (!response.ok) throw await responseError(response)
  if (response.status === 204) return undefined as T
  return parseJsonResponse<T>(response)
}

async function responseError(response: Response): Promise<ApiError> {
  const body = await response.text()
  let payload: ApiErrorShape | null = null
  try {
    payload = JSON.parse(body) as ApiErrorShape
  } catch {
    // The response may be an HTML error document from a proxy or an older local server.
  }
  if (!payload?.error?.message) {
    payload = requestFailedPayload(response)
  }
  return new ApiError(response.status, payload)
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const body = await response.text()
  try {
    return JSON.parse(body) as T
  } catch {
    throw new ApiError(502, requestFailedPayload(response))
  }
}

function requestFailedPayload(response: Response): ApiErrorShape {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
  const isHtml = contentType.includes('text/html')
  return {
    error: {
      code: isHtml ? 'api_version_mismatch' : 'invalid_api_response',
      message: isHtml
        ? 'The local app is running an older API. Restart Canvas Task Sync, then refresh this page.'
        : response.statusText || 'The local app returned an invalid response. Restart it and try again.',
      retryable: true,
    },
  }
}

export function useOverview(courseId?: string | null) {
  const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : ''
  return useSWR<OverviewResponse>(`/api/v1/overview${suffix}`, fetchJson, {
    revalidateOnFocus: true,
    dedupingInterval: 2_000,
  })
}

export function formatDateTime(value?: string | null, options?: Intl.DateTimeFormatOptions) {
  if (!value) return '-'
  return new Intl.DateTimeFormat(undefined, options ?? {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

export function formatDuration(start?: string | null, finish?: string | null) {
  if (!start) return '-'
  const end = finish ? new Date(finish).getTime() : Date.now()
  const seconds = Math.max(0, (end - new Date(start).getTime()) / 1000)
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
}

export function humanize(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function agendaWeekOptions(
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone,
  now = new Date(),
) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }).formatToParts(now)
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  const today = new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)))
  const daysSinceMonday = (today.getUTCDay() + 6) % 7
  const currentMonday = new Date(today)
  currentMonday.setUTCDate(today.getUTCDate() - daysSinceMonday)

  return (Object.keys(WEEK_NAMES) as WeekSelection[]).map((selection) => {
    const start = new Date(currentMonday)
    start.setUTCDate(currentMonday.getUTCDate() + WEEK_OFFSETS[selection])
    const end = new Date(start)
    end.setUTCDate(start.getUTCDate() + 4)
    return {
      value: selection,
      label: `${WEEK_NAMES[selection]} · ${formatAgendaWeekRange(start, end)}`,
    }
  })
}

function formatAgendaWeekRange(start: Date, end: Date) {
  const month = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' })
  const startMonth = month.format(start)
  const endMonth = month.format(end)
  const year = end.getUTCFullYear()
  if (start.getUTCFullYear() === year && startMonth === endMonth) {
    return `${startMonth} ${start.getUTCDate()}-${end.getUTCDate()}, ${year}`
  }
  if (start.getUTCFullYear() !== year) {
    return `${startMonth} ${start.getUTCDate()}, ${start.getUTCFullYear()}-${endMonth} ${end.getUTCDate()}, ${year}`
  }
  return `${startMonth} ${start.getUTCDate()}-${endMonth} ${end.getUTCDate()}, ${year}`
}
