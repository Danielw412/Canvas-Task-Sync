import {
  ArrowLeft,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock3,
  LoaderCircle,
  LockKeyhole,
  ShieldCheck,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { ActionMark, Button, EmptyState, Modal, StatusLabel } from '../components/ui'
import { fetchJson, formatDateTime, formatDuration, humanize, mutateJson } from '../lib/api'
import type { RunDetail, RunEvent, RunStage, SyncAction } from '../types'

const previewStages: { key: RunStage; label: string }[] = [
  { key: 'validate_configuration', label: 'Validate configuration' },
  { key: 'authenticate_services', label: 'Authenticate services' },
  { key: 'capture_source', label: 'Capture source' },
  { key: 'extract_assignments', label: 'Extract assignments' },
  { key: 'calculate_deadlines', label: 'Calculate deadlines' },
  { key: 'compare_google_tasks', label: 'Compare Google Tasks' },
  { key: 'build_review_plan', label: 'Build review plan' },
]

const terminal = new Set(['awaiting_approval', 'succeeded', 'review_needed', 'stale', 'cancelled', 'failed', 'failed_partial'])
const eventNames = ['run_queued', 'apply_queued', 'stage_completed', 'action_applied', 'run_completed', 'run_failed', 'run_cancelled', 'preview_stale', 'health_check', 'cancellation_requested']
const writableKinds = new Set(['create', 'update', 'notes_cleanup'])

function useRun(runId?: string) {
  const response = useSWR<RunDetail>(runId ? `/api/v1/runs/${runId}` : null, fetchJson, {
    refreshInterval: (data) => data && !terminal.has(data.status) ? 1_000 : 0,
  })
  const revalidate = response.mutate
  useEffect(() => {
    if (!runId) return
    const source = new EventSource(`/api/v1/runs/${runId}/events`)
    const update = () => { void revalidate() }
    eventNames.forEach((event) => source.addEventListener(event, update))
    source.onerror = update
    return () => source.close()
  }, [runId, revalidate])
  return response
}

function attention(kind: string) {
  return ['uncertain', 'source_missing', 'remote_missing', 'historical_blocked'].includes(kind)
}

export default function RunDetailPage() {
  const { runId } = useParams()
  const { data: run, error, mutate } = useRun(runId)
  const { toast } = useApp()
  const navigate = useNavigate()
  const [planFilter, setPlanFilter] = useState<'all' | 'changes' | 'attention' | 'unchanged'>('all')
  const [debugTab, setDebugTab] = useState<'events' | 'evidence' | 'state' | 'errors'>('events')
  const [selectedAction, setSelectedAction] = useState<SyncAction | null>(null)
  const [confirmApply, setConfirmApply] = useState(false)
  const [busy, setBusy] = useState(false)
  const actions = useMemo(() => run?.plan?.actions ?? [], [run?.plan?.actions])
  const changeCount = actions.filter((action) => writableKinds.has(action.kind)).length
  const attentionItems = actions.filter((action) => attention(action.kind))
  const filtered = useMemo(() => actions.filter((action) => {
    if (planFilter === 'changes') return writableKinds.has(action.kind)
    if (planFilter === 'attention') return attention(action.kind)
    if (planFilter === 'unchanged') return action.kind === 'unchanged'
    return true
  }), [actions, planFilter])

  async function cancel() {
    if (!run) return
    setBusy(true)
    try {
      await mutateJson(`/api/v1/runs/${run.id}/cancel`)
      toast('Cancellation requested.', 'warning')
      await mutate()
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Run could not be cancelled.', 'error')
    } finally { setBusy(false) }
  }

  async function apply() {
    if (!run?.plan_hash) return
    setBusy(true)
    try {
      await mutateJson(`/api/v1/runs/${run.id}/apply`, { body: { plan_hash: run.plan_hash } })
      setConfirmApply(false)
      toast('Approved changes were added to the write queue.', 'success')
      await Promise.all([mutate(), globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))])
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Plan could not be applied.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <EmptyState title="Run could not load" body={error.message} action={<Button variant="secondary" onClick={() => navigate('/runs')}>Back to runs</Button>} />
  if (!run) return <div className="page-loader"><LoaderCircle className="spin" /><span>Loading run…</span></div>

  const completedStages = new Set(run.events.filter((event) => event.event_type === 'stage_completed').map((event) => event.stage))
  const isActive = ['queued', 'running', 'applying'].includes(run.status)
  const latestEvent = run.events.at(-1)
  const eventGroups = {
    events: run.events,
    evidence: run.events.filter((event) => ['capture_source', 'extract_assignments', 'build_review_plan'].includes(event.stage)),
    state: run.events.filter((event) => ['compare_google_tasks', 'persist_state', 'revalidate_preview'].includes(event.stage)),
    errors: run.events.filter((event) => event.level === 'error' || event.level === 'warning'),
  }

  return <div className="run-detail-layout">
    <section className="run-workspace">
      <header className="run-heading">
        <div><div className="breadcrumbs"><Link to="/runs">Runs</Link><span>/</span><span>Run #{run.id}</span></div><h1>{run.requested_mode === 'health' ? `Checking ${run.course_name ?? run.course_id}` : run.requested_mode === 'auto_apply' ? `Syncing ${run.course_name ?? run.course_id}` : run.status === 'applying' ? `Applying ${run.course_name ?? run.course_id}` : `Previewing ${run.course_name ?? run.course_id}`}</h1><p>Started {formatDateTime(run.started_at ?? run.created_at)} · {run.trigger === 'schedule' ? 'Scheduled trigger' : 'Manual trigger'}</p></div>
        <div className="run-heading__status"><StatusLabel status={run.status} />{isActive && run.status !== 'applying' ? <Button variant="secondary" icon={X} disabled={busy} onClick={cancel}>Cancel run</Button> : null}</div>
      </header>

      {run.requested_mode === 'health' ? <section className="panel health-run-summary"><div className="health-run-summary__icon"><CheckCircle2 /></div><div><h2>Connection and source checks</h2><p>{latestEvent?.message ?? 'Checking configured services…'}</p></div></section> : <section className="stage-panel panel">
        <div className="stage-timeline">{previewStages.map((stage, index) => {
          const complete = completedStages.has(stage.key)
          const active = run.stage === stage.key && isActive
          const event = run.events.slice().reverse().find(
            (item) => item.stage === stage.key && item.event_type === 'stage_completed',
          )
          return <div className={`stage-item ${complete ? 'stage-item--complete' : active ? 'stage-item--active' : ''}`} key={stage.key}>
            <div className="stage-item__track">{index > 0 ? <span /> : null}<i>{complete ? <Check size={17} /> : active ? <LoaderCircle className="spin" size={17} /> : <Circle size={16} />}</i>{index < previewStages.length - 1 ? <span /> : null}</div>
            <strong>{stage.label}</strong><small>{complete ? `${((event?.duration_ms ?? 0) / 1000).toFixed(1)}s` : active ? 'In progress' : 'Waiting'}</small>
          </div>
        })}</div>
        <div className="stage-summary"><span>{completedStages.size} of 7 stages</span><span>·</span><span>{formatDuration(run.started_at, run.finished_at)} elapsed</span></div>
      </section>}

      <section className="plan-section">
        <h2>Plan preview</h2>
        <div className="filter-tabs" role="tablist">
          <button className={planFilter === 'all' ? 'is-active' : ''} onClick={() => setPlanFilter('all')}>All <span>{actions.length}</span></button>
          <button className={planFilter === 'changes' ? 'is-active' : ''} onClick={() => setPlanFilter('changes')}>Changes <span>{changeCount}</span></button>
          <button className={planFilter === 'attention' ? 'is-active' : ''} onClick={() => setPlanFilter('attention')}>Needs attention <span>{attentionItems.length}</span></button>
          <button className={planFilter === 'unchanged' ? 'is-active' : ''} onClick={() => setPlanFilter('unchanged')}>Unchanged <span>{actions.filter((item) => item.kind === 'unchanged').length}</span></button>
        </div>
        {filtered.length ? <div className="table-frame plan-table"><div className="data-table data-table--plan data-table__header"><span>Action</span><span>Task</span><span>Due</span><span>Destination</span><span>Reason</span><span /></div>{filtered.map((action, index) => <button className="data-table data-table--plan data-row" key={`${action.logical_id ?? action.title}-${index}`} onClick={() => setSelectedAction(action)}><ActionMark kind={action.kind} /><strong>{action.title}</strong><span>{action.due_uncertain ? 'Due date uncertain' : action.due_date ? new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(`${action.due_date}T12:00:00`)) : 'No due date'}</span><span>{action.task_list ?? '—'}</span><span>{action.reason}</span><ChevronRight size={16} /></button>)}<div className="table-hint"><Clock3 size={16} /> Click a row to view details, evidence, and field-level comparisons.</div></div> : <EmptyState title={isActive ? 'Building the review plan' : 'No items in this view'} body={isActive ? 'Actions will appear when source comparison completes.' : 'Choose another plan filter.'} />}
      </section>

      {run.requested_mode === 'preview' ? <section className="apply-band panel"><div><ShieldCheck size={23} /><p><strong>Create, Update, and Clean notes actions will write to Google Tasks.</strong><span>Nothing is ever deleted automatically.</span></p></div><div><Button variant="secondary" onClick={() => navigate('/runs')}>Discard preview</Button><Button icon={LockKeyhole} disabled={run.status !== 'awaiting_approval' || changeCount === 0 || Boolean(run.test_rebase_week)} onClick={() => setConfirmApply(true)}>Apply {changeCount} {changeCount === 1 ? 'change' : 'changes'}</Button>{run.status !== 'awaiting_approval' ? <small>Available when preview completes</small> : null}</div></section> : null}
    </section>

    <aside className="run-inspector inspector-rail">
      <section className="rail-section run-summary"><h2>Run details</h2><div className="summary-grid"><span>Run ID</span><strong>{run.id}</strong><span>Trigger</span><strong>{run.trigger === 'schedule' ? 'Schedule' : 'Manual'}</strong><span>Extraction mode</span><strong>{run.extraction_mode ? humanize(run.extraction_mode) : '—'}</strong><span>Cache</span><strong>{run.events.some((event) => event.metadata.cache === 'hit') ? 'Hit' : 'Miss'}</strong><span>Page hash</span><strong>{run.page_hash ? `${run.page_hash.slice(0, 5)}…${run.page_hash.slice(-4)}` : '—'}</strong><span>Source blocks</span><strong>{String(run.events.find((event) => event.stage === 'capture_source')?.metadata.block_count ?? '—')}</strong><span>Remote tasks</span><strong>{String(run.events.find((event) => event.stage === 'compare_google_tasks')?.metadata.remote_task_count ?? '—')}</strong></div><div className="current-step"><span>Current step</span><p>{isActive ? <LoaderCircle className="spin" size={17} /> : <CheckCircle2 className="tone-success" size={17} />}{latestEvent?.message ?? 'Waiting for the first event.'}</p></div></section>
      <section className="rail-section debug-section"><div className="debug-heading"><h2>Debug details</h2><small>Secrets are always redacted</small><ChevronDown size={17} /></div><div className="debug-tabs">{([['events', 'Events'], ['evidence', 'Evidence'], ['state', 'State decisions'], ['errors', 'Errors']] as const).map(([key, label]) => <button className={debugTab === key ? 'is-active' : ''} key={key} onClick={() => setDebugTab(key)}>{label}</button>)}</div><div className="event-list">{eventGroups[debugTab].map((event) => <EventRow event={event} key={event.id} />)}{!eventGroups[debugTab].length ? <p className="muted-copy">No {debugTab === 'state' ? 'state decisions' : debugTab} recorded yet.</p> : null}</div></section>
    </aside>

    {selectedAction ? <ActionDrawer action={selectedAction} onClose={() => setSelectedAction(null)} /> : null}
    {confirmApply ? <Modal title={`Apply ${changeCount} ${changeCount === 1 ? 'change' : 'changes'}?`} onClose={() => setConfirmApply(false)} footer={<><Button variant="secondary" onClick={() => setConfirmApply(false)}>Keep reviewing</Button><Button icon={LockKeyhole} disabled={busy} onClick={apply}>Confirm and apply</Button></>}><div className="confirmation-copy"><ShieldCheck size={28} /><p>This applies Create, Update, and legacy-note cleanup actions from the immutable preview. Items needing attention stay untouched, and no Google Task is deleted.</p></div></Modal> : null}
  </div>
}

function EventRow({ event }: { event: RunEvent }) {
  const [open, setOpen] = useState(false)
  return <div className={`event-row event-row--${event.level}`}><button onClick={() => setOpen((value) => !value)}><time>{new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(event.created_at))}</time><span className={`level level--${event.level}`}>{event.level === 'warning' ? 'WARN' : event.level.toUpperCase()}</span><span>{event.message}</span>{event.duration_ms != null ? <small>{(event.duration_ms / 1000).toFixed(1)}s</small> : null}<ChevronRight className={open ? 'rotate' : ''} size={15} /></button>{open ? <pre>{JSON.stringify(event.metadata, null, 2)}</pre> : null}</div>
}

function ActionDrawer({ action, onClose }: { action: SyncAction; onClose: () => void }) {
  return <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose() }}><aside className="detail-drawer" role="dialog" aria-modal="true" aria-label="Plan item details"><header><div><ActionMark kind={action.kind} /><h2>{action.title}</h2></div><button className="icon-button" onClick={onClose} aria-label="Close details"><X size={20} /></button></header><div className="detail-drawer__body"><section><h3>Decision</h3><p>{action.reason}</p></section><section><h3>AI-generated description</h3><p>{action.desired?.details || 'No additional detail was available from the source.'}</p>{action.desired?.assignment_url ? <a className="inline-link" href={action.desired.assignment_url} target="_blank" rel="noreferrer">Open assignment <ArrowLeft className="rotate-180" size={16} /></a> : null}</section><section><h3>Exact source evidence</h3><blockquote>{action.evidence || action.desired?.source_text || 'No source evidence was retained for this item.'}</blockquote></section><section><h3>Desired task</h3><dl><dt>Destination</dt><dd>{action.task_list ?? '—'}</dd><dt>Task type</dt><dd>{action.desired?.task_type ?? '—'}</dd><dt>Due date</dt><dd>{action.due_uncertain ? 'Due date uncertain' : action.due_date ?? 'None'}</dd>{action.due_uncertain_reason ? <><dt>Uncertainty reason</dt><dd>{action.due_uncertain_reason}</dd></> : null}<dt>Due basis</dt><dd>{action.desired?.due_basis ?? '—'}</dd><dt>Classification</dt><dd>{action.desired?.classification ?? '—'}</dd><dt>Action kind</dt><dd>{action.desired?.action_kind ?? '—'}</dd><dt>Due verified</dt><dd>{action.due_verified ? 'Yes' : 'Pending apply'}</dd><dt>Remote task ID</dt><dd>{action.remote_task_id ?? 'Not assigned'}</dd></dl></section>{action.desired?.source_url ? <a className="inline-link" href={action.desired.source_url} target="_blank" rel="noreferrer">Open source page <ArrowLeft className="rotate-180" size={16} /></a> : null}</div></aside></div>
}
