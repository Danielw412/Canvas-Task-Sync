import { afterEach, describe, expect, it, vi } from 'vitest'
import { agendaWeekOptions, ApiError, fetchJson } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchJson', () => {
  it('turns an HTML response from an older server into an actionable error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<!doctype html>', {
      status: 200,
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
    })))

    const request = fetchJson('/api/v1/settings/extension')

    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      code: 'api_version_mismatch',
      status: 502,
      message: 'The local app is running an older API. Restart Canvas Task Sync, then refresh this page.',
    } satisfies Partial<ApiError>)
  })

  it('returns valid JSON payloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ connected: true })))

    await expect(fetchJson<{ connected: boolean }>('/api/v1/example')).resolves.toEqual({ connected: true })
  })
})

describe('agendaWeekOptions', () => {
  it('shows the exact Monday through Friday date range for every choice', () => {
    const options = agendaWeekOptions(
      'America/New_York',
      new Date('2026-08-17T16:00:00Z'),
    )

    expect(options).toEqual([
      { value: 'previous_week', label: 'Previous Week · Aug 10-14, 2026' },
      { value: 'this_week', label: 'This Week · Aug 17-21, 2026' },
      { value: 'next_week', label: 'Next Week · Aug 24-28, 2026' },
    ])
  })
})
