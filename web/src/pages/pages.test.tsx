import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SWRConfig } from 'swr'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OverviewResponse, RunDetail } from '../types'
import { AppShell } from '../components/AppShell'
import CoursesPage from './CoursesPage'
import OverviewPage from './OverviewPage'
import RunDetailPage from './RunDetailPage'
import RunsPage from './RunsPage'
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
      assessment_task_list: 'Tests',
      ai_instructions: '',
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
    local_server: '127.0.0.1:8790',
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
    task_lists: ['School', 'Tests'],
    dry_run: true,
    extraction_mode: 'hybrid',
    fallback_reasons: [],
    actions: [
      { kind: 'create', title: 'Create me', reason: 'Not found remotely', due_date: '2026-08-14', task_list: 'School', desired: { details: 'Complete questions 1–5.', source_url: 'https://canvas.example/agenda', assignment_url: 'https://canvas.example/courses/7/assignments/10', source_text: 'Questions 1–5', due_basis: 'next class', action_kind: 'complete', classification: 'homework', task_type: 'assignment', destination_task_list: 'School' } },
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
  it('allows a Canvas course to have no fallback acquisition', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(overview.courses)))
    renderPage(<CoursesPage />)

    fireEvent.click(await screen.findByRole('button', { name: 'Source & extraction' }))
    const fallback = await screen.findByLabelText(/Fallback acquisition/)
    fireEvent.change(fallback, { target: { value: 'none' } })

    expect(fallback).toHaveValue('none')
    expect(screen.getByText(/Canvas API content is the only agenda source/i)).toBeVisible()
    expect(screen.queryByLabelText(/Presentation URL/)).not.toBeInTheDocument()
  })

  it('saves course-specific AI instructions with the selected course only', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      if (url === '/api/v1/courses/spanish' && init?.method === 'PUT') {
        return jsonResponse(overview.courses)
      }
      return jsonResponse(overview.courses)
    }))
    renderPage(<CoursesPage />)

    const instructions = await screen.findByLabelText(/Course notes \/ AI instructions/)
    fireEvent.change(instructions, {
      target: { value: 'Do not create homework tasks for reading assignments.' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save changes' })[0])

    await waitFor(() => {
      const request = requests.find(
        (item) => item.url === '/api/v1/courses/spanish' && item.init?.method === 'PUT',
      )
      expect(request).toBeDefined()
      expect(JSON.parse(String(request?.init?.body))).toMatchObject({
        id: 'spanish',
        settings: {
          ai_instructions: 'Do not create homework tasks for reading assignments.',
        },
      })
    })
  })

  it('saves a unique per-course Gemini primary and fallback order', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      return jsonResponse(overview.courses)
    }))
    renderPage(<CoursesPage />)

    fireEvent.change(await screen.findByLabelText('Primary model'), {
      target: { value: 'gemini-3.5-flash' },
    })
    fireEvent.change(screen.getByLabelText('Fallback 1'), {
      target: { value: 'gemini-3.5-flash-lite' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save changes' })[0])

    await waitFor(() => {
      const request = requests.find(
        (item) => item.url === '/api/v1/courses/spanish' && item.init?.method === 'PUT',
      )
      expect(JSON.parse(String(request?.init?.body))).toMatchObject({
        settings: {
          gemini_model: 'gemini-3.5-flash',
          gemini_fallback_models: [
            'gemini-3.5-flash-lite',
            'gemini-3.7-flash',
            'gemini-3.6-flash',
          ],
        },
      })
    })
  })

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

    expect(screen.getByLabelText('Agenda week')).toHaveValue('this_week')
    fireEvent.change(screen.getByLabelText('Agenda week'), { target: { value: 'next_week' } })

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
      mode: 'preview',
      week_selection: 'next_week',
      acquisition_strategy: 'auto',
      include_past: true,
      test_rebase_week: '2026-08-10',
    })
  })

  it('makes sync all the primary automatic action', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      if (url === '/api/v1/runs/all') {
        return jsonResponse({ run_ids: [41, 42], capture_request_ids: ['capture-1'], status: 'queued' }, 202)
      }
      return jsonResponse(overview)
    }))
    const postMessage = vi.spyOn(window, 'postMessage')
    renderPage(<OverviewPage />)
    expect(await screen.findByRole('heading', { name: 'Everything is ready to sync' })).toBeVisible()

    const buttons = screen.getAllByRole('button')
    expect(buttons.findIndex((button) => button.textContent === 'Sync all courses')).toBeLessThan(
      buttons.findIndex((button) => button.textContent === 'Sync selected course'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Sync all courses' }))

    await waitFor(() => expect(requests.some((request) => request.url === '/api/v1/runs/all')).toBe(true))
    expect(postMessage).toHaveBeenCalledWith(
      { source: 'canvas-task-sync-web', type: 'capture-requested' },
      window.location.origin,
    )
    expect(JSON.parse(String(requests.find((request) => request.url === '/api/v1/runs/all')?.init?.body))).toMatchObject({ mode: 'auto_apply' })
    expect(toast).toHaveBeenCalledWith('Started syncing 2 courses in parallel.', 'success')
  })

  it('shows runs from every course by default and supports course filtering', async () => {
    const allCoursesOverview = {
      ...overview,
      courses: [
        ...overview.courses,
        { ...overview.courses[0], id: 'literature', settings: { ...overview.courses[0].settings, name: 'AP Literature' } },
      ],
    }
    const requests: string[] = []
    const runs = [
      { ...run, id: 8, course_id: 'literature', course_name: 'AP Literature', status: 'failed' },
      { ...run, id: 7, course_id: 'spanish', course_name: 'Repository Course', status: 'succeeded' },
    ]
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requests.push(url)
      if (url.startsWith('/api/v1/runs')) return jsonResponse(runs)
      return jsonResponse(allCoursesOverview)
    }))

    renderPage(<RunsPage />)

    expect(await screen.findByRole('link', { name: /AP Literature/ })).toBeVisible()
    expect(screen.getByRole('link', { name: /Repository Course/ })).toBeVisible()
    expect(screen.getByLabelText('Course filter')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('Course filter'), { target: { value: 'literature' } })
    await waitFor(() => expect(requests).toContain('/api/v1/runs?course_id=literature'))
  })

  it('deletes a course only after explicit confirmation', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, init })
      if (url.includes('/bootstrap')) return jsonResponse({ csrf_token: 'csrf' })
      if (url === '/api/v1/courses/spanish' && init?.method === 'DELETE') return jsonResponse([])
      return jsonResponse(overview.courses)
    }))
    renderPage(<CoursesPage />)
    expect(await screen.findByRole('heading', { name: 'Repository Course' })).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Delete course' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete Repository Course?' })
    expect(within(dialog).getByText(/deletes every schedule assigned to it/i)).toBeVisible()
    expect(within(dialog).getByText(/existing Google Tasks are kept/i)).toBeVisible()
    expect(requests.some((request) => request.init?.method === 'DELETE')).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete course' }))

    expect(await screen.findByText('Add your first course')).toBeVisible()
    expect(requests).toContainEqual(expect.objectContaining({
      url: '/api/v1/courses/spanish',
      init: expect.objectContaining({ method: 'DELETE' }),
    }))
    expect(toast).toHaveBeenCalledWith(
      'Course deleted. Existing Google Tasks and run history were kept.',
      'success',
    )
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
    expect(screen.getByText('School')).toBeVisible()
    expect(screen.queryByText('Review me')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 change' }))
    expect(screen.getByRole('dialog', { name: 'Apply 1 change?' })).toBeVisible()
    expect(screen.getByText(/no Google Task is deleted/i)).toBeVisible()
  })

  it('shows the generated description and exact assignment link in plan details', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(run)))
    renderPage(
      <Routes><Route path="/runs/:runId" element={<RunDetailPage />} /></Routes>,
      '/runs/7',
    )

    fireEvent.click(await screen.findByRole('button', { name: /Create me/ }))

    expect(screen.getByRole('heading', { name: 'AI-generated description' })).toBeVisible()
    expect(screen.getByText('Complete questions 1–5.')).toBeVisible()
    expect(screen.getByRole('link', { name: /Open assignment/ })).toHaveAttribute(
      'href',
      'https://canvas.example/courses/7/assignments/10',
    )
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
      if (url.includes('/settings/extension')) return jsonResponse({
        server_url: 'http://127.0.0.1:8790',
        pairing_token: 'pairing-token-fixture',
        capture_ttl_seconds: 900,
        supported_sources: ['google_slides', 'google_docs', 'google_sheets'],
        load_unpacked_path: 'C:\\project\\extension\\dist',
        captures: [],
      })
      return jsonResponse(settings)
    }))
    renderPage(<SettingsPage />)
    const open = await screen.findByRole('button', { name: 'Replace key' })
    expect(screen.getByRole('heading', { name: 'Chrome source connector' })).toBeVisible()
    expect(screen.getByLabelText('Extension pairing token')).toHaveValue('pairing-token-fixture')
    open.focus()
    fireEvent.click(open)
    const input = screen.getByLabelText('API key')
    fireEvent.change(input, { target: { value: 'super-private-test-key' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save key' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(document.activeElement).toBe(open))
    expect(screen.queryByDisplayValue('super-private-test-key')).not.toBeInTheDocument()
    const mutation = requests.find((request) => request.url.includes('/gemini-key'))!
    expect(JSON.parse(String(mutation.init?.body))).toEqual({ api_key: 'super-private-test-key' })
  })
})
