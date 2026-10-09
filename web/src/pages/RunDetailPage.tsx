import {
  ArrowLeftIcon,
  ArrowSquareOutIcon,
  CaretRightIcon,
  CheckIcon,
  CircleNotchIcon,
  ShieldCheckIcon,
  StopIcon,
  XCircleIcon,
  XIcon,
} from '@phosphor-icons/react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import useSWR from 'swr'
import { useApp } from '../components/AppContext'
import { ElapsedTime } from '../components/ElapsedTime'
import {
  ActionBadge,
  Button,
  Disclosure,
  EmptyState,
  Modal,
  Notice,
  PageLoader,
  RunStatusBadge,
  Segmented,
  Sheet,
  SkeletonRows,
  StatusIcon,
} from '../components/ui'
import {
  attentionTotal,
  changeSummary,
  fetchJson,
  formatDateTime,
  formatDueDate,
  humanize,
  isActiveRun,
  isAttentionKind,
  mutateJson,
  revalidateOverview,
  runKindLabel,
  stageLabels,
} from '../lib/api'
import type { HealthState, RunDetail, RunEvent, RunStage, SyncAction } from '../types'

const previewStages: { key: RunStage; label: string }[] = [
  { key: 'validate_configuration', label: 'Check settings' },
  { key: 'authenticate_services', label: 'Connect' },
  { key: 'capture_source', label: 'Read agenda' },
  { key: 'extract_assignments', label: 'Find tasks' },
  { key: 'calculate_deadlines', label: 'Set due dates' },
  { key: 'compare_google_tasks', label: 'Compare tasks' },
  { key: 'build_review_plan', label: 'Build plan' },
]

const terminal = new Set(['awaiting_approval', 'succeeded', 'review_needed', 'stale', 'cancelled', 'failed', 'failed_partial'])
const eventNames = ['run_queued', 'apply_queued', 'stage_completed', 'action_applied', 'run_completed', 'run_failed', 'run_cancelled', 'preview_stale', 'health_check', 'cancellation_requested']
const writableKinds = new Set(['create', 'update', 'notes_cleanup'])
const weekNames = { previous_week: 'last week', this_week: 'this week', next_week: 'next week' }

type PlanFilter = 'all' | 'changes' | 'attention' | 'unchanged'
type EventFilter = 'events' | 'evidence' | 'state' | 'errors'

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

function dueLabel(action: SyncAction) {
  if (action.due_uncertain) return 'Due date uncertain'
  return action.due_date ? formatDueDate(action.due_date) : 'No due date'
}

export default function RunDetailPage() {
  const { runId } = useParams()
  const { data: run, error, mutate } = useRun(runId)
  const { toast } = useApp()
  const navigate = useNavigate()
  const [planFilter, setPlanFilter] = useState<PlanFilter>('all')
  const [eventFilter, setEventFilter] = useState<EventFilter>('events')
  const [selectedAction, setSelectedAction] = useState<SyncAction | null>(null)
  const [confirmApply, setConfirmApply] = useState(false)
  const [showStages, setShowStages] = useState(false)
  const [busy, setBusy] = useState(false)
  const actions = useMemo(() => run?.plan?.actions ?? [], [run?.plan?.actions])
  const changeCount = actions.filter((action) => writableKinds.has(action.kind)).length
  const attentionCount = actions.filter((action) => isAttentionKind(action.kind)).length
  const unchangedCount = actions.filter((action) => action.kind === 'unchanged').length
  const filtered = useMemo(() => actions.filter((action) => {
    if (planFilter === 'changes') return writableKinds.has(action.kind)
    if (planFilter === 'attention') return isAttentionKind(action.kind)
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
      await Promise.all([mutate(), revalidateOverview()])
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Plan could not be applied.', 'error')
    } finally { setBusy(false) }
  }

  if (error) return <div className="surface"><EmptyState icon={XCircleIcon} title="Run could not load" body={error.message} action={<Button variant="secondary" onClick={() => navigate('/runs')}>Back to runs</Button>} /></div>
  if (!run) return <PageLoader />

  const isActive = isActiveRun(run.status)
  const isHealth = run.requested_mode === 'health'
  const courseName = run.course_name ?? run.course_id
  const latestEvent = run.events.at(-1)
  const completedEvents = run.events.filter((event) => event.event_type === 'stage_completed')
  const completedStages = new Set(completedEvents.map((event) => event.stage))
  const completedPreviewStages = previewStages.filter((stage) => completedStages.has(stage.key)).length
  const stageStartedAt = completedEvents.at(-1)?.created_at ?? run.started_at
  const failedIndex = ['failed', 'failed_partial'].includes(run.status) ? previewStages.findIndex((stage) => !completedStages.has(stage.key)) : -1
  const eventGroups: Record<EventFilter, RunEvent[]> = {
    events: run.events,
    evidence: run.events.filter((event) => ['capture_source', 'extract_assignments', 'build_review_plan'].includes(event.stage)),
    state: run.events.filter((event) => ['compare_google_tasks', 'persist_state', 'revalidate_preview'].includes(event.stage)),
    errors: run.events.filter((event) => event.level === 'error' || event.level === 'warning'),
  }
  const healthChecks = run.events.filter((event) => event.event_type === 'health_check')
  const weekName = run.week_selection ? weekNames[run.week_selection] : null

  return <div className="page--run">
    <Link className="back-link" to="/runs"><ArrowLeftIcon size={14} aria-hidden />All runs</Link>
    <header className="run-head">
      <div className="run-head__text">
        <div className="eyebrow-line">{runKindLabel(run)}<span aria-hidden>·</span>Run #{run.id}</div>
        <h1>{courseName}</h1>
        <p className="run-head__meta">
          Started {formatDateTime(run.started_at ?? run.created_at)}, {run.trigger === 'schedule' ? 'by a schedule' : 'manually'}{weekName && !isHealth ? `, for ${weekName}` : ''}.
        </p>
      </div>
      <div className="run-head__status">
        <RunStatusBadge status={run.status} />
        {isActive && run.status !== 'applying' ? <Button variant="secondary" size="sm" icon={StopIcon} disabled={busy} onClick={() => void cancel()}>Cancel run</Button> : null}
      </div>
    </header>

    {run.error_summary ? <div className="overview-notices"><Notice tone="danger" title={run.status === 'failed_partial' ? 'Some changes were written before this run stopped' : 'This run stopped early'}>{run.error_summary}</Notice></div> : null}
    {run.status === 'stale' ? <div className="overview-notices"><Notice tone="warning" title="This preview is out of date">The agenda, settings, or Google Tasks changed after it was built, so it can no longer be applied. Start a new preview from Overview.</Notice></div> : null}
    {run.status === 'review_needed' && !isHealth ? <div className="overview-notices"><Notice tone="warning" title={`${attentionTotal(run.counts) || 'Some'} ${attentionTotal(run.counts) === 1 ? 'item needs' : 'items need'} your attention`}>Safe changes were applied. The items below were left untouched until you check them.</Notice></div> : null}

    {isHealth ? <section className="section" aria-labelledby="checks-heading">
      <div className="section-head"><h2 id="checks-heading">Checks</h2>{latestEvent && !isActive ? <p>{latestEvent.message}</p> : null}</div>
      {healthChecks.length ? <div className="surface">
        {healthChecks.map((event) => <div className="row check-row" key={event.id}>
          <StatusIcon state={(event.metadata.state as HealthState | undefined) ?? (event.level === 'error' ? 'error' : event.level === 'warning' ? 'warning' : 'healthy')} />
          <span className="row-title">{event.message.split(':')[0]}</span>
          <span className="check-row__summary">{event.message.split(':').slice(1).join(':').trim() || event.message}</span>
          <span className="subtle num">{event.duration_ms != null ? `${(event.duration_ms / 1000).toFixed(1)}s` : ''}</span>
        </div>)}
      </div> : isActive ? <SkeletonRows rows={3} label="Running checks" /> : <div className="surface"><EmptyState title="No checks were recorded" body="The health check finished without reporting any results." /></div>}
    </section> : <>
      {!isActive && failedIndex === -1 && !showStages ? <section className="surface progress-line" aria-label="Run progress">
        <span className="progress-line__text">
          <StatusIcon state={completedPreviewStages === previewStages.length ? 'healthy' : run.status} size={18} />
          <span><strong>{completedPreviewStages} of 7 stages</strong> finished in <span className="num"><ElapsedTime start={run.started_at} finish={run.finished_at} active={false} /></span></span>
        </span>
        <Button variant="ghost" size="sm" onClick={() => setShowStages(true)}>Show stages</Button>
      </section> : <section className="surface progress" aria-label="Run progress">
        <ol className="progress__steps">
          {previewStages.map((stage, index) => {
            const complete = completedStages.has(stage.key)
            const current = !complete && isActive && run.stage === stage.key
            const failed = index === failedIndex
            const event = [...completedEvents].reverse().find((item) => item.stage === stage.key)
            const state = failed ? 'is-failed' : complete ? 'is-done' : current ? 'is-current' : ''
            return <li className={`progress__step ${state}`} key={stage.key}>
              <span className="progress__dot">{failed ? <XIcon size={12} weight="bold" aria-hidden /> : complete ? <CheckIcon size={12} weight="bold" aria-hidden /> : current ? <CircleNotchIcon className="spin" size={13} weight="bold" aria-hidden /> : null}</span>
              <span className="progress__label">{stage.label}</span>
              <span className="progress__time">{complete ? `${((event?.duration_ms ?? 0) / 1000).toFixed(1)}s` : current ? <ElapsedTime start={stageStartedAt} active /> : null}</span>
            </li>
          })}
        </ol>
        <div className="progress__summary">
          <span><strong>{completedPreviewStages} of 7 stages</strong>{isActive ? <span className="muted">{stageLabels[run.stage]}…</span> : null}</span>
          <span className="progress__summary-end">
            <span className="num"><ElapsedTime start={run.started_at} finish={run.finished_at} active={isActive} /> total</span>
            {showStages && !isActive ? <Button variant="ghost" size="sm" onClick={() => setShowStages(false)}>Hide stages</Button> : null}
          </span>
        </div>
      </section>}

      <div className="plan-head">
        <h2>Plan</h2>
        {actions.length ? <Segmented
          mode="buttons"
          label="Plan filter"
          value={planFilter}
          onChange={setPlanFilter}
          options={[
            { value: 'all', label: 'All', count: actions.length },
            { value: 'changes', label: 'Changes', count: changeCount },
            { value: 'attention', label: 'Needs attention', count: attentionCount },
            { value: 'unchanged', label: 'Unchanged', count: unchangedCount },
          ]}
        /> : null}
      </div>
      {filtered.length ? <div className="surface">
        {filtered.map((action, index) => <button type="button" className="row row--interactive plan-row" key={`${action.logical_id ?? action.title}-${index}`} onClick={() => setSelectedAction(action)}>
          <span><ActionBadge kind={action.kind} /></span>
          <span className="row-title truncate">{action.title}</span>
          <span className={`plan-row__due${action.due_uncertain ? ' is-uncertain' : ''}`}>{dueLabel(action)}</span>
          <span className="plan-row__list truncate">{action.task_list ?? '-'}</span>
          <CaretRightIcon className="row-chevron" size={15} aria-hidden />
        </button>)}
      </div> : isActive ? <SkeletonRows rows={4} label="Building the plan" /> : <div className="surface">
        <EmptyState
          title={actions.length ? 'Nothing in this view' : 'No plan was built'}
          body={actions.length ? 'Choose another filter to see the rest of the plan.' : run.error_summary ? 'The run stopped before it could compare tasks.' : 'This run found nothing to sync.'}
        />
      </div>}

      {run.requested_mode === 'preview' ? <ApplyBar
        run={run}
        changeCount={changeCount}
        busy={busy}
        onDiscard={() => navigate('/runs')}
        onApply={() => setConfirmApply(true)}
      /> : null}
    </>}

    <Disclosure className="tech" title="Technical details" hint="Run data and the sanitized event log">
      <div className="tech-grid">
        <dl className="kv">
          <dt>Run ID</dt><dd className="num">{run.id}</dd>
          <dt>Trigger</dt><dd>{run.trigger === 'schedule' ? 'Schedule' : 'Manual'}</dd>
          <dt>Mode</dt><dd>{runKindLabel(run)}</dd>
          <dt>Extraction</dt><dd>{run.extraction_mode ? humanize(run.extraction_mode) : '-'}</dd>
          <dt>Cache</dt><dd>{run.events.some((event) => event.metadata.cache === 'hit') ? 'Hit' : 'Miss'}</dd>
          <dt>Remote tasks</dt><dd className="num">{String(run.events.find((event) => event.stage === 'compare_google_tasks')?.metadata.remote_task_count ?? '-')}</dd>
          {run.include_past ? <><dt>Past-due</dt><dd>Included</dd></> : null}
          {run.test_rebase_week ? <><dt>Rebased to</dt><dd className="num">{run.test_rebase_week}</dd></> : null}
          <dt>Current step</dt><dd>{latestEvent?.message ?? 'Waiting for the first event.'}</dd>
        </dl>
        <div>
          <div className="tech-head">
            <Segmented
              mode="buttons"
              label="Event filter"
              value={eventFilter}
              onChange={setEventFilter}
              options={[
                { value: 'events', label: 'All' },
                { value: 'evidence', label: 'Evidence' },
                { value: 'state', label: 'State' },
                { value: 'errors', label: 'Problems', count: eventGroups.errors.length },
              ]}
            />
            <span className="fine-print"><ShieldCheckIcon size={14} aria-hidden />Secrets are always redacted.</span>
          </div>
          <div className="event-list">
            {eventGroups[eventFilter].map((event) => <EventRow event={event} key={event.id} />)}
            {!eventGroups[eventFilter].length ? <p className="subtle">Nothing recorded here yet.</p> : null}
          </div>
        </div>
      </div>
    </Disclosure>

    {selectedAction ? <ActionSheet action={selectedAction} onClose={() => setSelectedAction(null)} /> : null}
    {confirmApply ? <Modal
      title={`Apply ${changeCount} ${changeCount === 1 ? 'change' : 'changes'}?`}
      onClose={() => setConfirmApply(false)}
      footer={<><Button variant="secondary" onClick={() => setConfirmApply(false)}>Keep reviewing</Button><Button icon={CheckIcon} loading={busy} onClick={() => void apply()}>Apply changes</Button></>}
    >
      <p>This writes the Create, Update, and note cleanup actions from this exact preview. Items that need attention stay untouched, and no Google Task is deleted.</p>
    </Modal> : null}
  </div>
}

function ApplyBar({ run, changeCount, busy, onDiscard, onApply }: {
  run: RunDetail
  changeCount: number
  busy: boolean
  onDiscard: () => void
  onApply: () => void
}) {
  const label = `Apply ${changeCount} ${changeCount === 1 ? 'change' : 'changes'}`
  if (run.status === 'applying') {
    return <div className="apply-bar" role="status">
      <div className="apply-bar__text"><CircleNotchIcon className="spin tone-info" size={20} aria-hidden /><p><strong>Applying changes</strong><span>Writing approved changes to Google Tasks.</span></p></div>
    </div>
  }
  if (run.status === 'succeeded' || run.status === 'failed_partial') {
    return <div className="apply-bar">
      <div className="apply-bar__text"><StatusIcon state={run.status} size={20} /><p><strong>{run.status === 'succeeded' ? 'Changes applied' : 'Some changes were applied'}</strong><span>{changeSummary(run) || 'Google Tasks is up to date with this preview.'}</span></p></div>
    </div>
  }
  if (['cancelled', 'failed', 'stale'].includes(run.status)) return null
  const ready = run.status === 'awaiting_approval'
  const rebased = Boolean(run.test_rebase_week)
  return <div className="apply-bar">
    <div className="apply-bar__text">
      <ShieldCheckIcon className="tone-info" size={22} aria-hidden />
      <p>
        <strong>{!ready ? 'Preview in progress' : changeCount ? `${changeCount} ${changeCount === 1 ? 'change' : 'changes'} ready to apply` : 'Nothing to apply'}</strong>
        <span>{rebased ? 'This preview was rebased to a test week, so it cannot be applied.' : !ready ? 'You can apply once the plan is ready.' : 'Items that need attention stay untouched. Nothing is ever deleted.'}</span>
      </p>
    </div>
    <div className="apply-bar__actions">
      <Button variant="ghost" onClick={onDiscard}>Discard</Button>
      <Button icon={CheckIcon} disabled={busy || !ready || changeCount === 0 || rebased} onClick={onApply}>{label}</Button>
    </div>
  </div>
}

function EventRow({ event }: { event: RunEvent }) {
  const [open, setOpen] = useState(false)
  return <div className="event-row">
    <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <time>{new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(event.created_at))}</time>
      <span className={`level level--${event.level}`}>{event.level === 'warning' ? 'WARN' : event.level.toUpperCase()}</span>
      <span className="truncate">{event.message}</span>
      {event.duration_ms != null ? <small>{(event.duration_ms / 1000).toFixed(1)}s</small> : <small />}
      <CaretRightIcon className={open ? 'is-open' : ''} size={13} aria-hidden />
    </button>
    {open ? <pre>{JSON.stringify(event.metadata, null, 2)}</pre> : null}
  </div>
}

function ActionSheet({ action, onClose }: { action: SyncAction; onClose: () => void }) {
  const desired = action.desired
  return <Sheet label="Plan item details" badge={<ActionBadge kind={action.kind} />} title={action.title} onClose={onClose}>
    <section className="detail-section"><h3>Decision</h3><p>{action.reason}</p></section>
    {action.due_uncertain_reason ? <Notice tone="warning" title="Due date uncertain">{action.due_uncertain_reason}</Notice> : null}
    <section className="detail-section"><h3>AI-generated description</h3><p>{desired?.details || 'No additional detail was available from the source.'}</p></section>
    <section className="detail-section"><h3>Exact source evidence</h3><blockquote className="quote">{action.evidence || desired?.source_text || 'No source evidence was retained for this item.'}</blockquote></section>
    <section className="detail-section">
      <h3>Task</h3>
      <dl className="kv">
        <dt>Destination</dt><dd>{action.task_list ?? '-'}</dd>
        <dt>Type</dt><dd>{desired?.task_type ? humanize(desired.task_type) : '-'}</dd>
        <dt>Due date</dt><dd>{action.due_uncertain ? 'Uncertain' : action.due_date ? formatDueDate(action.due_date) : 'None'}</dd>
        <dt>Due basis</dt><dd>{desired?.due_basis ?? '-'}</dd>
        <dt>Classification</dt><dd>{desired?.classification ? humanize(desired.classification) : '-'}</dd>
        <dt>Action</dt><dd>{desired?.action_kind ? humanize(desired.action_kind) : '-'}</dd>
        <dt>Due verified</dt><dd>{action.due_verified ? 'Yes' : 'Pending apply'}</dd>
        <dt>Google task</dt><dd className="mono">{action.remote_task_id ?? 'Not created yet'}</dd>
      </dl>
    </section>
    {desired?.assignment_url || desired?.source_url ? <div className="link-list">
      {desired?.assignment_url ? <a className="text-link" href={desired.assignment_url} target="_blank" rel="noreferrer">Open assignment<ArrowSquareOutIcon size={14} aria-hidden /></a> : null}
      {desired?.source_url ? <a className="text-link" href={desired.source_url} target="_blank" rel="noreferrer">Open source page<ArrowSquareOutIcon size={14} aria-hidden /></a> : null}
    </div> : null}
  </Sheet>
}
