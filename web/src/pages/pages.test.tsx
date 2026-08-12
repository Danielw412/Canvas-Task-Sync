import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SWRConfig } from 'swr'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OverviewResponse, RunDetail } from '../types'
import { AppShell } from '../components/AppShell'
import OverviewPage from './OverviewPage'
import RunDetailPage from './RunDetailPage'
import SettingsPage from './SettingsPage'

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('../components/AppContext', () => ({
  useApp: () => ({ selectedCourseId: 'spanish', setSelectedCourseId: vi.fn(), toast }),
}))

const overview: OverviewResponse = {
  selected_course_id: 'spanish',
  courses: [{
    id: 'spanish',
    readiness: 'healthy',
    readiness_message: 'Ready',
    settings: {
      enabled: true,
      name: 'Repository Course',
      prefix: 'COURSE',
      task_list: 'School',
      timezone: 'America/New_York',
      meeting_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
      source: {
        type: 'google_slides',
        url: 'https://docs.google.com/presentation/d/fixture/edit',
        page_id: 'slide-1',
        extraction: {
          mode: 'hybrid',
          thumbnail_size: 'large',
          assignments_default_due: 'next_class',
          same_day_action_kinds: ['bring', 'present', 'submit'],
        },
      },
    },
  }],
  connections: {
    google_client_configured: true,
    google_authorized: true,
    gemini_configured: true,
    local_server: '127.0.0.1:8787',
    checks: [],
  },
  latest_run: null,
  recent_runs: [],
  next_schedule: null,
}

const run: RunDetail = {
  id: 7,
  course_id: 'spanish',
  course_name: 'Repository Course',
  trigger: 'manual',
  requested_mode: 'preview',
  status: 'awaiting_approval',
  stage: 'complete',
  created_at: '2026-08-11T20:00:00Z',
  started_at: '2026-08-11T20:00:00Z',
  finished_at: '2026-08-11T20:00:05Z',
  extraction_mode: 'hybrid',
  counts: { create: 1, uncertain: 1, unchanged: 1 },
  applied_counts: {},
  plan_hash: 'a'.repeat(64),
  include_past: false,
  cancel_requested: false,
  plan: {
    course_id: 'spanish',
    task_list: 'School',
    dry_run: true,
    extraction_mode: 'hybrid',
    fallback_reasons: [],
    actions: [
      { kind: 'create', title: 'Create me', reason: 'Not found remotely', due_date: '2026-08-14' },
      { kind: 'uncertain', title: 'Review me', reason: 'Evidence is ambiguous' },
      { kind: 'unchanged', title: 'Leave me', reason: 'No changes needed' },
    ],
  },
  events: [
    { id: 1, run_id: 7, sequence: 1, created_at: '2026-08-11T20:00:01Z', stage: 'validate_configuration', event_type: 'stage_completed', level: 'info', message: 'Validated', metadata: {} },
    { id: 2, run_id: 7, sequence: 2, created_at: '2026-08-11T20:00:02Z', stage: 'authenticate_services', event_type: 'stage_completed', level: 'info', message: 'Authenticated', metadata: {} },
  ],
}

class EventSourceStub {
  onerror: (() => void) | null = null
  addEventListener() {}
  close() {}
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(value), {
    status,
    headers: status === 204 ? undefined : { 'Content-Type': 'application/json' },
  })
}

function renderPage(node: ReactNode, path = '/') {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>
    </SWRConfig>,
  )
}

beforeEach(() => {
  toast.mockReset()
  vi.stubGlobal('EventSource', EventSourceStub)
})

describe('operational pages', () => {
  it('renders desktop and mobile navigation from one route model', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(overview)))
    renderPage(<AppShell />)
    expect(await screen.findByLabelText('Primary navigation')).toBeVisible()
    expect(within(screen.getByLabelText('Primary navigation')).getAllByRole('link')).toHaveLength(6)
    expect(within(screen.getByLabelText('Mobile navigation')).getAllByRole('link')).toHaveLength(5)
  })

  it('submits advanced preview policy and validates Monday rebasing', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      if (url === '/api/v1/runs') return jsonResponse({ run_id: 44, status: 'queued' }, 202)
      return jsonResponse(overview)
    }))
    renderPage(<OverviewPage />)
    expect(await screen.findByRole('heading', { name: 'Everything is ready to sync' })).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Advanced preview' }))
    const dialog = screen.getByRole('dialog', { name: 'Advanced preview' })
    expect(dialog).toContainElement(document.activeElement as HTMLElement)
    fireEvent.change(within(dialog).getByLabelText('Rebase fixture week (optional)'), {
      target: { value: '2026-08-11' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start diagnostic preview' }))
    expect(toast).toHaveBeenCalledWith('The test week must begin on a Monday.', 'warning')

    fireEvent.change(within(dialog).getByLabelText('Rebase fixture week (optional)'), {
      target: { value: '2026-08-10' },
    })
    fireEvent.click(within(dialog).getByLabelText('Include past-due changes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Start diagnostic preview' }))
    await waitFor(() => expect(requests.some((request) => request.url === '/api/v1/runs')).toBe(true))
    const mutation = requests.find((request) => request.url === '/api/v1/runs')!
    expect(JSON.parse(String(mutation.init?.body))).toMatchObject({
      course_id: 'spanish',
      include_past: true,
      test_rebase_week: '2026-08-10',
    })
  })

  it('filters an immutable plan and requires explicit apply confirmation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(run)))
    renderPage(
      <Routes><Route path="/runs/:runId" element={<RunDetailPage />} /></Routes>,
      '/runs/7',
    )
    expect(await screen.findByText('2 of 7 stages')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /Changes 1/ }))
    expect(screen.getByText('Create me')).toBeVisible()
    expect(screen.queryByText('Review me')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 change' }))
    expect(screen.getByRole('dialog', { name: 'Apply 1 change?' })).toBeVisible()
    expect(screen.getByText(/no Google Task is deleted/i)).toBeVisible()
  })

  it('keeps API keys write-only and restores focus after the settings modal', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    const settings = {
      connections: { ...overview.connections, checks: [] },
      general: { history_retention_days: 90 },
      paths: { control_database: 'control.sqlite3', state_database: 'state.sqlite3', config: 'courses.yaml' },
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      if (url.includes('/gemini-key')) return jsonResponse(null, 204)
      return jsonResponse(settings)
    }))
    renderPage(<SettingsPage />)
    const open = await screen.findByRole('button', { name: 'Replace key' })
    open.focus()
    fireEvent.click(open)
    const input = screen.getByLabelText('API key')
    fireEvent.change(input, { target: { value: 'super-private-test-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(document.activeElement).toBe(open)
    expect(screen.queryByDisplayValue('super-private-test-key')).not.toBeInTheDocument()
    const mutation = requests.find((request) => request.url.includes('/gemini-key'))!
    expect(JSON.parse(String(mutation.init?.body))).toEqual({ api_key: 'super-private-test-key' })
  })
})
