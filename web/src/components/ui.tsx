import {
  CaretDownIcon,
  CheckCircleIcon,
  CircleDashedIcon,
  CircleNotchIcon,
  InfoIcon,
  MinusCircleIcon,
  PlusCircleIcon,
  ArrowsClockwiseIcon,
  WarningCircleIcon,
  WarningIcon,
  XCircleIcon,
  XIcon,
  type Icon,
} from '@phosphor-icons/react'
import {
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import { isAttentionKind } from '../lib/api'
import type { HealthState, RunStatus, SyncActionKind } from '../types'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-solid'

export function Button({
  children,
  variant = 'primary',
  size = 'md',
  icon: IconComponent,
  loading = false,
  className = '',
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: 'sm' | 'md'
  icon?: Icon
  loading?: boolean
}) {
  const iconSize = size === 'sm' ? 15 : 16
  return <button
    type="button"
    className={`btn btn--${variant}${size === 'sm' ? ' btn--sm' : ''}${children ? '' : ' btn--icon'} ${className}`.trim()}
    disabled={disabled || loading}
    {...props}
  >
    {loading ? <CircleNotchIcon className="spin" size={iconSize} aria-hidden /> : IconComponent ? <IconComponent size={iconSize} aria-hidden /> : null}
    {children}
  </button>
}

export function IconButton({ label, icon: IconComponent, size = 'md', variant = 'ghost', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string
  icon: Icon
  size?: 'sm' | 'md'
  variant?: ButtonVariant
}) {
  return <button
    type="button"
    className={`btn btn--${variant} btn--icon${size === 'sm' ? ' btn--sm' : ''}`}
    aria-label={label}
    title={label}
    {...props}
  >
    <IconComponent size={size === 'sm' ? 16 : 18} aria-hidden />
  </button>
}

const healthy = new Set(['healthy', 'succeeded'])
const cautious = new Set(['warning', 'review_needed', 'stale', 'failed_partial'])
const failing = new Set(['error', 'failed'])
const waiting = new Set(['queued', 'awaiting_approval', 'missing', 'cancelled'])

export function StatusIcon({ state, size = 18 }: { state: HealthState | RunStatus; size?: number }) {
  if (healthy.has(state)) return <CheckCircleIcon className="status-icon tone-success" size={size} weight="fill" aria-hidden />
  if (cautious.has(state)) return <WarningCircleIcon className="status-icon tone-warning" size={size} weight="fill" aria-hidden />
  if (failing.has(state)) return <XCircleIcon className="status-icon tone-danger" size={size} weight="fill" aria-hidden />
  if (waiting.has(state)) return <CircleDashedIcon className="status-icon tone-muted" size={size} aria-hidden />
  return <CircleNotchIcon className="status-icon tone-info spin" size={size} aria-hidden />
}

const runStatusLabels: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  awaiting_approval: 'Ready to review',
  applying: 'Applying',
  succeeded: 'Completed',
  review_needed: 'Needs review',
  stale: 'Out of date',
  cancelled: 'Cancelled',
  failed: 'Failed',
  failed_partial: 'Partly applied',
}

const runStatusTones: Record<RunStatus, string> = {
  queued: 'neutral',
  running: 'info',
  awaiting_approval: 'accent',
  applying: 'info',
  succeeded: 'success',
  review_needed: 'warning',
  stale: 'warning',
  cancelled: 'neutral',
  failed: 'danger',
  failed_partial: 'danger',
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const active = status === 'running' || status === 'applying'
  return <span className={`badge badge--${runStatusTones[status]}`}>
    {active ? <CircleNotchIcon className="spin" size={12} weight="bold" aria-hidden /> : null}
    {runStatusLabels[status]}
  </span>
}

export function ActionBadge({ kind }: { kind: SyncActionKind }) {
  if (kind === 'create') return <span className="badge badge--success"><PlusCircleIcon size={13} weight="bold" aria-hidden />Create</span>
  if (kind === 'update') return <span className="badge badge--info"><ArrowsClockwiseIcon size={13} weight="bold" aria-hidden />Update</span>
  if (kind === 'notes_cleanup') return <span className="badge badge--info"><ArrowsClockwiseIcon size={13} weight="bold" aria-hidden />Clean notes</span>
  if (isAttentionKind(kind)) return <span className="badge badge--warning"><WarningIcon size={13} weight="bold" aria-hidden />Needs attention</span>
  if (kind === 'ignored') return <span className="badge"><InfoIcon size={13} weight="bold" aria-hidden />Ignored</span>
  return <span className="badge"><MinusCircleIcon size={13} weight="bold" aria-hidden />Unchanged</span>
}

export function PageHeader({ title, description, actions, eyebrow }: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  eyebrow?: ReactNode
}) {
  return <header className="page-header">
    <div className="page-header__text">
      {eyebrow ? <div className="eyebrow-line">{eyebrow}</div> : null}
      <h1>{title}</h1>
      {description ? <p className="page-header__desc">{description}</p> : null}
    </div>
    {actions ? <div className="page-header__actions">{actions}</div> : null}
  </header>
}

export function EmptyState({ icon: IconComponent = InfoIcon, title, body, action }: {
  icon?: Icon
  title: string
  body: ReactNode
  action?: ReactNode
}) {
  return <div className="empty">
    <span className="empty__icon"><IconComponent size={22} aria-hidden /></span>
    <strong className="empty__title">{title}</strong>
    <p className="empty__body">{body}</p>
    {action ? <div className="empty__action">{action}</div> : null}
  </div>
}

export function PageLoader() {
  return <div className="page-loader" role="status" aria-label="Loading">
    <span className="skeleton" /><span className="skeleton" /><span className="skeleton" />
  </div>
}

export function SkeletonRows({ rows = 4, label = 'Loading' }: { rows?: number; label?: string }) {
  return <div className="surface" role="status" aria-label={label}>
    {Array.from({ length: rows }, (_, index) => <div className="skeleton-row" key={index}>
      <span className="skeleton" style={{ width: `${68 - index * 9}%` }} />
      <span className="skeleton" />
      <span className="skeleton" />
    </div>)}
  </div>
}

export function Notice({ tone = 'info', icon, title, children, actions }: {
  tone?: 'info' | 'warning' | 'danger' | 'success'
  icon?: Icon
  title?: ReactNode
  children?: ReactNode
  actions?: ReactNode
}) {
  const fallback = { info: InfoIcon, warning: WarningIcon, danger: XCircleIcon, success: CheckCircleIcon }[tone]
  const IconComponent = icon ?? fallback
  return <div className={`notice notice--${tone}`} role={tone === 'danger' ? 'alert' : undefined}>
    <IconComponent className="notice__icon" size={18} weight={tone === 'info' ? 'regular' : 'fill'} aria-hidden />
    <div className="notice__body">
      {title ? <strong>{title}</strong> : null}
      {children ? typeof children === 'string' ? <p>{children}</p> : children : null}
    </div>
    {actions ? <div className="notice__actions">{actions}</div> : null}
  </div>
}

export function Field({ label, help, error, children, className = '' }: {
  label: ReactNode
  help?: ReactNode
  error?: ReactNode
  children: ReactNode
  className?: string
}) {
  return <label className={`field ${className}`.trim()}>
    <span className="field__label">{label}</span>
    {children}
    {error ? <small className="field__error">{error}</small> : help ? <small className="field__help">{help}</small> : null}
  </label>
}

export function FieldGroup({ label, help, children, className = '' }: {
  label: ReactNode
  help?: ReactNode
  children: ReactNode
  className?: string
}) {
  const id = useId()
  return <div className={`field ${className}`.trim()} role="group" aria-labelledby={id}>
    <span className="field__label" id={id}>{label}</span>
    {children}
    {help ? <small className="field__help">{help}</small> : null}
  </div>
}

export function Switch({ checked, onChange, label, disabled }: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  disabled?: boolean
}) {
  return <button
    type="button"
    role="switch"
    className="switch"
    aria-checked={checked}
    aria-label={label}
    title={label}
    disabled={disabled}
    onClick={(event) => {
      event.stopPropagation()
      onChange(!checked)
    }}
  />
}

export interface SegmentOption<T extends string> {
  value: T
  label: ReactNode
  count?: number
  ariaLabel?: string
}

// "radio" picks one value (a filter or a setting); "buttons" toggles views and reads as pressed buttons.
export function Segmented<T extends string>({ value, options, onChange, label, mode = 'radio', className = '' }: {
  value: T
  options: SegmentOption<T>[]
  onChange: (value: T) => void
  label: string
  mode?: 'radio' | 'buttons'
  className?: string
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (mode !== 'radio') return
    const delta = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 0
    if (!delta) return
    event.preventDefault()
    const next = (index + delta + options.length) % options.length
    onChange(options[next].value)
    refs.current[next]?.focus()
  }

  return <div className={`segmented ${className}`.trim()} role={mode === 'radio' ? 'radiogroup' : 'group'} aria-label={label}>
    {options.map((option, index) => {
      const active = option.value === value
      return <button
        key={option.value}
        ref={(element) => { refs.current[index] = element }}
        type="button"
        className={`segmented__item${active ? ' is-active' : ''}`}
        role={mode === 'radio' ? 'radio' : undefined}
        aria-checked={mode === 'radio' ? active : undefined}
        aria-pressed={mode === 'buttons' ? active : undefined}
        aria-label={option.ariaLabel}
        tabIndex={mode === 'radio' && !active ? -1 : 0}
        onClick={() => onChange(option.value)}
        onKeyDown={(event) => handleKeyDown(event, index)}
      >
        {option.label}
        {option.count != null ? <>{' '}<span className="segmented__count">{option.count}</span></> : null}
      </button>
    })}
  </div>
}

export function Disclosure({ title, hint, defaultOpen = false, children, className = '' }: {
  title: ReactNode
  hint?: ReactNode
  defaultOpen?: boolean
  children: ReactNode
  className?: string
}) {
  // Open state is the person's after mount: a later defaultOpen never collapses what they opened.
  const [open, setOpen] = useState(defaultOpen)
  return <details className={`disclosure ${className}`.trim()} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{title}{hint ? <span className="disclosure__hint">{hint}</span> : null}<CaretDownIcon className="disclosure__chevron" size={16} aria-hidden /></summary>
    <div className="disclosure__body">{children}</div>
  </details>
}

const FOCUSABLE = 'input:not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'

// Shared dialog behavior: focus the first control, trap Tab, close on Escape, and return
// focus to whatever opened the dialog.
function useDialog(ref: RefObject<HTMLElement | null>, onClose: () => void, initialSelectors: string[]) {
  const restoreFocusRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)
  const selectors = initialSelectors.join('|')
  useEffect(() => {
    const restoreTarget = restoreFocusRef.current
    const root = ref.current
    // Skip controls inside a closed disclosure; where layout is unavailable, treat all as visible.
    const measurable = Boolean(root?.getClientRects().length)
    const visible = (element: HTMLElement) => !measurable || element.getClientRects().length > 0
    const first = selectors.split('|')
      .map((selector) => [...(root?.querySelectorAll<HTMLElement>(selector) ?? [])].find(visible))
      .find(Boolean) ?? root?.querySelector<HTMLElement>('button')
    first?.focus()
    return () => restoreTarget?.focus()
  }, [ref, selectors])

  return function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key !== 'Tab' || !ref.current) return
    const focusable = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)]
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
}

export function Modal({ title, children, footer, onClose }: { title: string; children: ReactNode; footer: ReactNode; onClose: () => void }) {
  const modalRef = useRef<HTMLElement>(null)
  const titleId = useId()
  const handleKeyDown = useDialog(modalRef, onClose, ['.modal__body input, .modal__body select, .modal__body textarea', '.modal__footer button'])
  return createPortal(<div className="backdrop modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={modalRef} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={handleKeyDown}>
      <header className="modal__header"><h2 id={titleId}>{title}</h2><IconButton label="Close" icon={XIcon} size="sm" onClick={onClose} /></header>
      <div className="modal__body">{children}</div>
      <footer className="modal__footer">{footer}</footer>
    </section>
  </div>, document.body)
}

export function Sheet({ title, description, badge, children, footer, onClose, label }: {
  title: ReactNode
  description?: ReactNode
  badge?: ReactNode
  children: ReactNode
  footer?: ReactNode
  onClose: () => void
  label?: string
}) {
  const sheetRef = useRef<HTMLElement>(null)
  const titleId = useId()
  const handleKeyDown = useDialog(sheetRef, onClose, ['.sheet__body input:not([type="radio"]), .sheet__body select, .sheet__body textarea', '.sheet__header button'])
  return createPortal(<div className="backdrop sheet-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <aside
      ref={sheetRef}
      className="sheet"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      aria-labelledby={label ? undefined : titleId}
      onKeyDown={handleKeyDown}
    >
      <header className="sheet__header">
        <div className="sheet__header-text">
          {badge ? <div>{badge}</div> : null}
          <h2 id={titleId}>{title}</h2>
          {description ? <p className="muted">{description}</p> : null}
        </div>
        <IconButton label="Close" icon={XIcon} size="sm" onClick={onClose} />
      </header>
      <div className="sheet__body">{children}</div>
      {footer ? <footer className="sheet__footer">{footer}</footer> : null}
    </aside>
  </div>, document.body)
}

export type MenuEntry =
  | { label: string; icon?: Icon; onSelect: () => void; disabled?: boolean }
  | 'separator'

export function Menu({ label, trigger, items, triggerClassName = 'btn btn--ghost btn--sm btn--icon', placement = 'down' }: {
  label: string
  trigger: ReactNode
  items: MenuEntry[]
  triggerClassName?: string
  placement?: 'down' | 'up'
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([])
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    function closeOnOutsidePointer(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOnOutsidePointer)
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer)
  }, [open])

  useEffect(() => {
    if (open) itemRefs.current.find(Boolean)?.focus()
  }, [open])

  function close(restore = true) {
    setOpen(false)
    if (restore) triggerRef.current?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const enabled = itemRefs.current.filter((item): item is HTMLButtonElement => Boolean(item && !item.disabled))
    const index = enabled.indexOf(document.activeElement as HTMLButtonElement)
    if (event.key === 'Escape') { event.preventDefault(); close() }
    else if (event.key === 'Tab') close(false)
    else if (event.key === 'ArrowDown') { event.preventDefault(); enabled[(index + 1) % enabled.length]?.focus() }
    else if (event.key === 'ArrowUp') { event.preventDefault(); enabled[(index - 1 + enabled.length) % enabled.length]?.focus() }
    else if (event.key === 'Home') { event.preventDefault(); enabled[0]?.focus() }
    else if (event.key === 'End') { event.preventDefault(); enabled.at(-1)?.focus() }
  }

  return <div className="menu-root" ref={rootRef}>
    <button
      ref={triggerRef}
      type="button"
      className={triggerClassName}
      aria-label={label}
      title={label}
      aria-haspopup="menu"
      aria-expanded={open}
      aria-controls={open ? menuId : undefined}
      onClick={() => setOpen((value) => !value)}
      onKeyDown={(event) => { if (event.key === 'ArrowDown' && !open) { event.preventDefault(); setOpen(true) } }}
    >{trigger}</button>
    {open ? <div className={`menu${placement === 'up' ? ' menu--up' : ''}`} role="menu" id={menuId} aria-label={label} onKeyDown={handleKeyDown}>
      {items.map((item, index) => item === 'separator'
        ? <div className="menu__sep" role="separator" key={`separator-${index}`} />
        : <button
          key={item.label}
          ref={(element) => { itemRefs.current[index] = element }}
          type="button"
          role="menuitem"
          className="menu__item"
          disabled={item.disabled}
          onClick={() => { close(false); item.onSelect() }}
        >{item.icon ? <item.icon size={16} aria-hidden /> : null}{item.label}</button>)}
    </div> : null}
  </div>
}
