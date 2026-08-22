import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SimpleApp } from './SimpleApp'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  static CLOSED = 2
  readyState = 1
  listeners = new Map<string, EventListener[]>()
  onerror: (() => void) | null = null

  constructor(public url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, listener: EventListener) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
  }

  emit(name: string, data: object = {}) {
    const event = name === 'log'
      ? new MessageEvent(name, { data: JSON.stringify(data) })
      : new MessageEvent(name, { data: JSON.stringify(data) })
    this.listeners.get(name)?.forEach((listener) => listener(event))
  }

  close() { this.readyState = FakeEventSource.CLOSED }
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const course = {
  id: 'spanish',
  readiness: 'healthy',
  readiness_message: 'Ready',
  settings: { enabled: true, name: 'Spanish IV' },
}

const run = {
  id: 1,
  operation_id: 'op-all',
  course_id: 'spanish',
  course_name: 'Spanish IV',
  trigger: 'manual',
  requested_mode: 'auto_apply',
  status: 'running',
  stage: 'extract_assignments',
  created_at: '2026-08-18T12:00:00Z',
  counts: {},
  applied_counts: {},
}

describe('SimpleApp', () => {
  beforeEach(() => { FakeEventSource.instances = [] })

  afterEach(() => vi.unstubAllGlobals())

  it('starts automatic sync-all, streams JSON, and clears the console on the next run', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url === '/runtime-config.json') return response({ api_base: 'http://127.0.0.1:8790' })
      if (url.endsWith('/api/v1/courses')) return response([course])
      if (url.includes('/api/v1/operations?')) return response([])
      if (url.includes('/api/v1/runs?limit=50')) return response([run])
      if (url.endsWith('/api/v1/bootstrap')) return response({ csrf_token: 'csrf' })
      if (url.endsWith('/api/v1/runs/all')) return response({ operation_id: 'op-all', run_ids: [1] }, 202)
      if (url.endsWith('/api/v1/runs')) return response({ operation_id: 'op-one', run_id: 2 }, 202)
      return response({})
    }))
    render(<SimpleApp eventSourceFactory={(url) => new FakeEventSource(url) as unknown as EventSource} />)

    expect(await screen.findByText('Run #1')).toBeVisible()
    expect(screen.getByText('In progress · Extract Assignments')).toBeVisible()

    fireEvent.click(await screen.findByRole('button', { name: 'Sync all courses' }))
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    expect(JSON.parse(String(requests.find((item) => item.url.endsWith('/runs/all'))?.init?.body))).toMatchObject({ mode: 'auto_apply' })

    act(() => FakeEventSource.instances[0].emit('log', {
      id: 1,
      run_id: 1,
      sequence: 1,
      operation_id: 'op-all',
      course_id: 'spanish',
      course_name: 'Spanish IV',
      created_at: '2026-08-18T12:00:00Z',
      stage: 'apply_changes',
      event_type: 'action_applied',
      level: 'info',
      message: "Create completed for 'Essay'.",
      metadata: { action: 'create', title: 'Essay' },
    }))
    await waitFor(() => expect(screen.getByLabelText('JSON log output')).toHaveTextContent('Create completed for'))
    expect(screen.getByText('Applying · Apply Changes')).toBeVisible()
    act(() => FakeEventSource.instances[0].emit('log', {
      id: 2,
      run_id: 1,
      sequence: 2,
      operation_id: 'op-all',
      course_id: 'spanish',
      course_name: 'Spanish IV',
      created_at: '2026-08-18T12:00:01Z',
      stage: 'complete',
      event_type: 'run_failed',
      level: 'error',
      message: 'Gemini service unavailable.',
      metadata: { partial: false },
    }))
    expect(await screen.findByText('Failed · Complete')).toBeVisible()
    expect(screen.getAllByText('Gemini service unavailable.')).toHaveLength(2)
    act(() => FakeEventSource.instances[0].emit('complete'))

    fireEvent.click(screen.getByRole('button', { name: 'Sync selected course' }))
    await waitFor(() => expect(requests.some((item) => item.url.endsWith('/api/v1/runs'))).toBe(true))
    expect(screen.getByLabelText('JSON log output')).not.toHaveTextContent('Create completed for')
    expect(screen.getByLabelText('JSON log output')).toHaveTextContent('No sync log selected')
  })
})
