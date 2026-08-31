import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Info,
  LoaderCircle,
  Minus,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { type ButtonHTMLAttributes, type KeyboardEvent, type ReactNode, useEffect, useRef } from 'react'
import type { HealthState, RunStatus, SyncActionKind } from '../types'

export function Button({
  children,
  variant = 'primary',
  icon: Icon,
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  icon?: LucideIcon
}) {
  return <button className={`button button--${variant} ${className}`} {...props}>{Icon ? <Icon size={17} /> : null}{children}</button>
}

export function IconButton({ label, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return <button className="icon-button" aria-label={label} title={label} {...props}>{children}</button>
}

export function StatusIcon({ state, size = 18 }: { state: HealthState | RunStatus; size?: number }) {
  if (['healthy', 'succeeded'].includes(state)) return <CheckCircle2 className="tone-success" size={size} />
  if (['warning', 'review_needed', 'stale'].includes(state)) return <AlertTriangle className="tone-warning" size={size} />
  if (['error', 'failed', 'failed_partial'].includes(state)) return <XCircle className="tone-danger" size={size} />
  if (['queued', 'awaiting_approval', 'missing'].includes(state)) return <Circle className="tone-muted" size={size} />
  return <LoaderCircle className="tone-primary spin" size={size} />
}

export function StatusLabel({ status }: { status: RunStatus }) {
  const labels: Record<RunStatus, string> = {
    queued: 'Queued', running: 'In progress', awaiting_approval: 'Ready to review', applying: 'Applying',
    succeeded: 'Success', review_needed: 'Review needed', stale: 'Preview stale', cancelled: 'Cancelled',
    failed: 'Failed', failed_partial: 'Partially applied',
  }
  return <span className={`status-label status-label--${status}`}><StatusIcon state={status} size={16} />{labels[status]}</span>
}

export function ActionMark({ kind }: { kind: SyncActionKind }) {
  if (kind === 'create') return <span className="action-mark action-mark--create"><span>+</span>Create</span>
  if (kind === 'update') return <span className="action-mark action-mark--update"><span>↗</span>Update</span>
  if (kind === 'notes_cleanup') return <span className="action-mark action-mark--update"><span>↗</span>Clean notes</span>
  if (['uncertain', 'remote_missing', 'source_missing', 'historical_blocked'].includes(kind)) {
    return <span className="action-mark action-mark--attention"><AlertTriangle size={15} />Needs attention</span>
  }
  if (kind === 'ignored') return <span className="action-mark action-mark--muted"><Info size={15} />Ignored</span>
  return <span className="action-mark action-mark--muted"><Minus size={15} />Unchanged</span>
}

export function SelectChevron() { return <ChevronDown aria-hidden size={17} /> }
export function RowChevron() { return <ChevronRight aria-hidden size={17} /> }

export function EmptyState({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return <div className="empty-state"><Info size={22} /><div><strong>{title}</strong><p>{body}</p></div>{action}</div>
}

export function PageLoader() {
  return <div className="page-loader" role="status" aria-label="Loading control center"><span /><span /><span /></div>
}

export function Modal({ title, children, footer, onClose }: { title: string; children: ReactNode; footer: ReactNode; onClose: () => void }) {
  const modalRef = useRef<HTMLElement>(null)
  const restoreFocusRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)
  useEffect(() => {
    const restoreTarget = restoreFocusRef.current
    const first = modalRef.current?.querySelector<HTMLElement>('.modal__body input, .modal__body select, .modal__body textarea, .modal__body button, .modal__body [href], button')
    first?.focus()
    return () => restoreTarget?.focus()
  }, [])

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab' || !modalRef.current) return
    const focusable = [...modalRef.current.querySelectorAll<HTMLElement>('input:not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled), [href]')]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable.at(-1)!
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={modalRef} className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" onKeyDown={handleKeyDown}>
      <header><h2 id="modal-title">{title}</h2><IconButton label="Close" onClick={onClose}><XCircle size={20} /></IconButton></header>
      <div className="modal__body">{children}</div>
      <footer>{footer}</footer>
    </section>
  </div>
}

export function CheckRow({ children }: { children: ReactNode }) {
  return <div className="check-row"><span className="check-row__icon"><Check size={14} /></span><span>{children}</span></div>
}
