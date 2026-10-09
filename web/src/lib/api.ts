import useSWR, { mutate } from 'swr'
import type { ApiErrorShape, OverviewResponse, RunStage, RunStatus, RunSummary, WeekSelection } from '../types'

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

const WEEK_SHORT_NAMES: Record<WeekSelection, string> = {
  previous_week: 'Last week',
  this_week: 'This week',
  next_week: 'Next week',
}

// The same weeks as agendaWeekOptions, split into a short name and its Monday-Friday range.
export function agendaWeeks(timeZone?: string, now = new Date()) {
  return agendaWeekOptions(timeZone, now).map((option) => ({
    value: option.value,
    name: WEEK_SHORT_NAMES[option.value],
    range: option.label.split(' · ')[1] ?? '',
  }))
}

export const stageLabels: Record<RunStage, string> = {
  queued: 'Waiting to start',
  validate_configuration: 'Checking settings',
  authenticate_services: 'Connecting to services',
  capture_source: 'Reading the agenda',
  extract_assignments: 'Finding tasks',
  calculate_deadlines: 'Setting due dates',
  compare_google_tasks: 'Comparing with Google Tasks',
  build_review_plan: 'Building the plan',
  revalidate_preview: 'Rechecking the preview',
  apply_changes: 'Writing to Google Tasks',
  persist_state: 'Saving sync state',
  health_check: 'Running checks',
  complete: 'Complete',
}

const ACTIVE_STATUSES = new Set<RunStatus>(['queued', 'running', 'applying'])
const ATTENTION_KINDS = ['uncertain', 'source_missing', 'remote_missing', 'historical_blocked']

export function isActiveRun(status: RunStatus) {
  return ACTIVE_STATUSES.has(status)
}

export function isAttentionKind(kind: string) {
  return ATTENTION_KINDS.includes(kind)
}

export function attentionTotal(counts: Record<string, number>) {
  return ATTENTION_KINDS.reduce((sum, key) => sum + (counts[key] ?? 0), 0)
}

export function runKindLabel(run: Pick<RunSummary, 'requested_mode'>) {
  if (run.requested_mode === 'health') return 'Health check'
  return run.requested_mode === 'auto_apply' ? 'Sync' : 'Preview'
}

// Applied counts describe what was written; a preview or failed run only has planned counts.
export function changeSummary(run: Pick<RunSummary, 'counts' | 'applied_counts' | 'requested_mode' | 'status'>) {
  if (run.requested_mode === 'health') return ''
  const counts = Object.keys(run.applied_counts ?? {}).length ? run.applied_counts : run.counts
  const created = counts.create ?? 0
  const updated = (counts.update ?? 0) + (counts.notes_cleanup ?? 0)
  const parts = [created ? `${created} created` : '', updated ? `${updated} updated` : ''].filter(Boolean)
  if (parts.length) return parts.join(', ')
  return Object.keys(run.counts ?? {}).length ? 'No changes' : ''
}

export function formatTime(value?: string | null) {
  if (!value) return '-'
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(value))
}

function startOfDay(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

export function dayDifference(value: string | Date, now = new Date()) {
  return Math.round((startOfDay(new Date(value)) - startOfDay(now)) / 86_400_000)
}

export function formatDayHeading(value: string, now = new Date()) {
  const difference = dayDifference(value, now)
  if (difference === 0) return 'Today'
  if (difference === -1) return 'Yesterday'
  return new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'short', day: 'numeric' }).format(new Date(value))
}

export function formatRelative(value?: string | null, now = new Date()) {
  if (!value) return 'never'
  const seconds = Math.round((now.getTime() - new Date(value).getTime()) / 1000)
  if (seconds < 0) {
    const ahead = -seconds
    if (ahead < 3_600) return `in ${Math.max(1, Math.round(ahead / 60))} min`
    if (dayDifference(value, now) === 0) return `today at ${formatTime(value)}`
    if (dayDifference(value, now) === 1) return `tomorrow at ${formatTime(value)}`
    return formatDateTime(value, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
  }
  if (seconds < 45) return 'just now'
  if (seconds < 3_600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 6 * 3_600) return `${Math.round(seconds / 3_600)} hr ago`
  if (dayDifference(value, now) === 0) return `today at ${formatTime(value)}`
  if (dayDifference(value, now) === -1) return `yesterday at ${formatTime(value)}`
  return formatDateTime(value, { month: 'short', day: 'numeric' })
}

// A task's due date is a calendar date, so it is compared and shown without a timezone shift.
export function formatDueDate(value: string, now = new Date()) {
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(year, month - 1, day)
  const difference = dayDifference(date, now)
  if (difference === 0) return 'Today'
  if (difference === 1) return 'Tomorrow'
  if (difference === -1) return 'Yesterday'
  const sameYear = date.getFullYear() === now.getFullYear()
  return new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) }).format(date)
}

export function dueDayDifference(value: string, now = new Date()) {
  const [year, month, day] = value.split('-').map(Number)
  return dayDifference(new Date(year, month - 1, day), now)
}

export function revalidateOverview() {
  return mutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
}

export function wakeExtensionCaptureQueue() {
  window.postMessage(
    { source: 'canvas-task-sync-web', type: 'capture-requested' },
    window.location.origin,
  )
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
