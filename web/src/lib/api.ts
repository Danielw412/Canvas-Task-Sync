import useSWR from 'swr'
import type { ApiErrorShape, OverviewResponse } from '../types'

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
  return response.json() as Promise<T>
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
  return response.json() as Promise<T>
}

async function responseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorShape
  try {
    payload = (await response.json()) as ApiErrorShape
  } catch {
    payload = {
      error: { code: 'request_failed', message: response.statusText || 'Request failed.', retryable: false },
    }
  }
  return new ApiError(response.status, payload)
}

export function useOverview(courseId?: string | null) {
  const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : ''
  return useSWR<OverviewResponse>(`/api/v1/overview${suffix}`, fetchJson, {
    revalidateOnFocus: true,
    dedupingInterval: 2_000,
  })
}

export function formatDateTime(value?: string | null, options?: Intl.DateTimeFormatOptions) {
  if (!value) return '—'
  return new Intl.DateTimeFormat(undefined, options ?? {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(new Date(value))
}

export function formatDuration(start?: string | null, finish?: string | null) {
  if (!start) return '—'
  const end = finish ? new Date(finish).getTime() : Date.now()
  const seconds = Math.max(0, (end - new Date(start).getTime()) / 1000)
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
}

export function humanize(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}
