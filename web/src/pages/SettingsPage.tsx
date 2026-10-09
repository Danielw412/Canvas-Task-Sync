import {
  ArrowClockwiseIcon,
  ArrowSquareOutIcon,
  CheckCircleIcon,
  CheckIcon,
  CopyIcon,
  DatabaseIcon,
  GoogleLogoIcon,
  KeyIcon,
  LockSimpleIcon,
  PlugsConnectedIcon,
  PuzzlePieceIcon,
  PulseIcon,
  ShieldCheckIcon,
  SparkleIcon,
  TrashIcon,
  UploadSimpleIcon,
  XCircleIcon,
  type Icon,
} from '@phosphor-icons/react'
import { useId, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, Disclosure, EmptyState, Modal, Notice, PageHeader, StatusIcon } from '../components/ui'
import { fetchJson, formatRelative, mutateJson } from '../lib/api'
import type {
  AgentEffort,
  AgentProvider,
  ConnectionStatus,
  ExtractionAgentSettings,
  ExtractionAgentView,
  GoogleAuthorizationStart,
  GoogleAuthorizationStatus,
  HealthCheck,
  HealthState,
} from '../types'

interface SettingsResponse {
  connections: ConnectionStatus
  general: { history_retention_days: number }
  paths: { control_database: string; state_database: string; config: string }
}

interface ExtensionSetup {
  server_url: string
  pairing_token: string
  capture_ttl_seconds: number
  supported_sources: string[]
  load_unpacked_path: string
  load_unpacked_relative_path?: string
  captures: { source_type: string; captured_at: string; item_count: number; screenshot_count: number }[]
}

type Section = 'ai' | 'google' | 'extension' | 'data'

const SECTIONS: { id: Section; label: string; icon: Icon }[] = [
  { id: 'ai', label: 'AI extraction', icon: SparkleIcon },
  { id: 'google', label: 'Google account', icon: GoogleLogoIcon },
  { id: 'extension', label: 'Chrome extension', icon: PuzzlePieceIcon },
  { id: 'data', label: 'Data & privacy', icon: DatabaseIcon },
]

const AUTHORIZATION_POLL_MS = 1_500

async function pollAuthorization(state: string, expiresAt: number): Promise<GoogleAuthorizationStatus> {
  let latest: GoogleAuthorizationStatus | null = null
  while (Date.now() < expiresAt) {
    await new Promise((resolve) => setTimeout(resolve, AUTHORIZATION_POLL_MS))
    latest = await fetchJson<GoogleAuthorizationStatus>(`/api/v1/settings/google/authorize/${encodeURIComponent(state)}`)
    if (latest.status !== 'pending') return latest
  }
  return latest?.status === 'pending' || latest === null
    ? { status: 'failed', message: 'Google authorization timed out. Start it again.', connections: latest?.connections ?? ({} as never) }
    : latest
}

function revalidateOverviews() {
  return globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))
}

export default function SettingsPage() {
  const { toast } = useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const section: Section = SECTIONS.find((item) => item.id === searchParams.get('section'))?.id ?? 'ai'
  const { data, error, mutate } = useSWR<SettingsResponse>('/api/v1/settings/connections', fetchJson)
  const { data: extension, error: extensionError, mutate: mutateExtension } = useSWR<ExtensionSetup>('/api/v1/settings/extension', fetchJson)
  const [keyModal, setKeyModal] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [authorizing, setAuthorizing] = useState(false)
  const [consentUrl, setConsentUrl] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<'disconnect' | 'history' | null>(null)

  function go(id: Section) {
    setSearchParams({ section: id }, { replace: true })
  }

  async function action(work: () => Promise<unknown>, success: string) {
    setBusy(true)
    try {
      await work()
      await Promise.all([mutate(), mutateExtension(), revalidateOverviews()])
      toast(success, 'success')
      return true
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'The action failed.', 'error')
      return false
    } finally { setBusy(false) }
  }

  async function saveKey() {
    await action(() => mutateJson('/api/v1/settings/gemini-key', { body: { api_key: apiKey } }), 'Gemini API key saved on the server.')
    setApiKey('')
    setKeyModal(false)
  }

  // Consent happens in this browser even when the backend is headless on another machine:
  // the backend mints the URL, this tab opens it, and Google redirects to the dashboard
  // origin, which hands the code back over the same loopback API.
  async function authorizeGoogle() {
    setAuthorizing(true)
    setConsentUrl(null)
    try {
      const started = await mutateJson<GoogleAuthorizationStart>('/api/v1/settings/google/authorize')
      setConsentUrl(started.authorization_url)
      window.open(started.authorization_url, '_blank', 'noopener,noreferrer')
      toast('Finish Google consent in the tab that opened.', 'info')
      const outcome = await pollAuthorization(started.state, new Date(started.expires_at).getTime())
      if (outcome.status === 'completed') {
        await Promise.all([mutate(), mutateExtension(), revalidateOverviews()])
        setConsentUrl(null)
        toast('Google is connected.', 'success')
      } else {
        toast(outcome.message ?? 'Google authorization did not complete.', 'error')
      }
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'Google authorization failed to start.', 'error')
    } finally {
      setAuthorizing(false)
    }
  }

  async function uploadClient(file?: File) {
    if (!file) return
    const formData = new FormData()
    formData.append('file', file)
    await action(() => mutateJson('/api/v1/settings/oauth-client', { formData }), 'OAuth client saved on the server.')
  }

  if (error) return <div className="surface"><EmptyState icon={XCircleIcon} title="Settings could not load" body={error.message} /></div>

  const connections = data?.connections
  const capture = extension?.captures?.[0]
  const sectionState: Record<Section, HealthState | null> = {
    ai: connections ? connections.extraction_ready ? 'healthy' : 'warning' : null,
    google: connections ? connections.google_authorized ? 'healthy' : 'warning' : null,
    extension: capture ? 'healthy' : null,
    data: null,
  }
  const setupSteps = connections ? [
    { done: connections.google_client_configured, title: 'Upload your Google OAuth client', target: 'google' as Section },
    { done: connections.google_authorized, title: 'Connect Google Tasks', target: 'google' as Section },
    { done: connections.extraction_ready, title: 'Choose an AI agent and check its sign-in', target: 'ai' as Section },
  ] : []
  const setupDone = setupSteps.filter((step) => step.done).length

  return <div className="page--settings">
    <PageHeader title="Settings" description="Connections, the AI agent, and the data kept on this server." />

    {setupSteps.length && setupDone < setupSteps.length ? <section className="surface surface--padded setup-card" aria-labelledby="setup-heading">
      <div className="section-head"><h2 id="setup-heading">Finish setting up</h2><p>{setupDone} of {setupSteps.length} done</p></div>
      <div className="setup-steps">
        {setupSteps.map((step, index) => <div className={`setup-step${step.done ? ' is-done' : ''}`} key={step.title}>
          <span className="setup-step__num" aria-hidden>{step.done ? <CheckIcon size={13} weight="bold" /> : index + 1}</span>
          <span className="setup-step__title">{step.done ? <span className="sr-only">Done: </span> : null}{step.title}</span>
          {step.done ? <span className="subtle">Done</span> : <Button variant="secondary" size="sm" onClick={() => go(step.target)}>Set up</Button>}
        </div>)}
      </div>
    </section> : null}

    <div className="settings-layout">
      <nav className="subnav" aria-label="Settings sections">
        {SECTIONS.map(({ id, label, icon: SectionIcon }) => <button
          type="button"
          key={id}
          className={`subnav__item${section === id ? ' is-active' : ''}`}
          aria-current={section === id ? 'page' : undefined}
          onClick={() => go(id)}
        >
          <SectionIcon size={18} aria-hidden />{label}
          {sectionState[id] ? <StatusIcon state={sectionState[id]!} size={15} /> : null}
        </button>)}
        <div className="subnav__sep" />
        <Link className="subnav__item" to="/diagnostics"><PulseIcon size={18} aria-hidden />Diagnostics</Link>
      </nav>

      <div className="settings-pane">
        {section === 'ai' ? <AiSection
          connections={connections}
          busy={busy}
          onSaved={() => Promise.all([mutate(), revalidateOverviews()])}
          onKey={() => setKeyModal(true)}
          onTestKey={() => void action(() => mutateJson('/api/v1/settings/gemini/test'), 'Gemini API key works.')}
        /> : null}
        {section === 'google' ? <GoogleSection
          connections={connections}
          busy={busy}
          authorizing={authorizing}
          consentUrl={consentUrl}
          onUpload={(file) => void uploadClient(file)}
          onAuthorize={() => void authorizeGoogle()}
          onDisconnect={() => setConfirm('disconnect')}
        /> : null}
        {section === 'extension' ? <ExtensionSection
          data={extension}
          error={extensionError}
          busy={busy}
          rotate={() => void action(() => mutateJson('/api/v1/settings/extension/rotate'), 'Pairing token rotated. Paste the new one into the extension.')}
          clear={() => void action(() => mutateJson('/api/v1/settings/extension/captures', { method: 'DELETE' }), 'Browser captures cleared from memory.')}
        /> : null}
        {section === 'data' ? <DataSection
          data={data}
          busy={busy}
          updateRetention={(days) => void action(() => mutateJson('/api/v1/settings/general', { method: 'PUT', body: { history_retention_days: days } }), 'History retention updated.')}
          clear={() => setConfirm('history')}
        /> : null}
      </div>
    </div>

    {keyModal ? <Modal
      title={connections?.gemini_configured ? 'Replace Gemini API key' : 'Add Gemini API key'}
      onClose={() => setKeyModal(false)}
      footer={<><Button variant="secondary" onClick={() => setKeyModal(false)}>Cancel</Button><Button icon={KeyIcon} disabled={busy || apiKey.length < 8} onClick={() => void saveKey()}>Save key</Button></>}
    >
      <label className="field">
        <span className="field__label">API key</span>
        <input className="control control--mono" aria-label="API key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
        <small className="field__help">Written to the server's .env file. It is never shown or logged again.</small>
      </label>
    </Modal> : null}
    {confirm === 'disconnect' ? <Modal
      title="Disconnect Google?"
      onClose={() => setConfirm(null)}
      footer={<><Button variant="secondary" onClick={() => setConfirm(null)}>Cancel</Button><Button variant="danger-solid" loading={busy} onClick={() => void action(() => mutateJson('/api/v1/settings/google/disconnect'), 'Google access disconnected.').then(() => setConfirm(null))}>Disconnect</Button></>}
    ><p>Syncing stops until you authorize again. Your OAuth client file stays, but token.json is taken out of use.</p></Modal> : null}
    {confirm === 'history' ? <Modal
      title="Clear run history?"
      onClose={() => setConfirm(null)}
      footer={<><Button variant="secondary" onClick={() => setConfirm(null)}>Cancel</Button><Button variant="danger-solid" icon={TrashIcon} loading={busy} onClick={() => void action(() => mutateJson('/api/v1/history', { method: 'DELETE' }), 'Run history cleared.').then(() => setConfirm(null))}>Clear history</Button></>}
    ><p>Every run and its sanitized log is deleted. Sync mappings and the extraction cache are kept, so nothing is duplicated later.</p></Modal> : null}
  </div>
}

const EFFORT_LABELS: Record<AgentEffort, string> = {
  low: 'Low, fastest',
  medium: 'Medium (recommended)',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max, most thorough',
}

const PROVIDER_BLURBS: Record<AgentProvider, string> = {
  gemini: 'Google Gemini with an API key.',
  claude: "Claude, using this server's Claude Code sign-in.",
  codex: "Codex, using this server's ChatGPT sign-in.",
}

const USAGE_NOTES: Record<AgentProvider, string> = {
  gemini: 'Uses the Gemini API key stored on the server.',
  claude: "Draws from the plan of this server's Claude Code sign-in.",
  codex: "Draws from the plan of this server's Codex (ChatGPT) sign-in.",
}

function allowedEffort(effort: AgentEffort, efforts: AgentEffort[]): AgentEffort {
  return efforts.length === 0 || efforts.includes(effort) ? effort : 'medium'
}

function readinessLabel(ready: boolean | null | undefined) {
  if (ready) return 'Ready'
  return ready === false ? 'Not signed in' : 'Not checked'
}

// One agent and model for every course's extraction. Each change saves immediately, so
// switching agents is a single click; the next run uses it.
function AiSection({ connections, busy: parentBusy, onSaved, onKey, onTestKey }: {
  connections?: ConnectionStatus
  busy: boolean
  onSaved: () => Promise<unknown>
  onKey: () => void
  onTestKey: () => void
}) {
  const { toast } = useApp()
  const { data, error, mutate } = useSWR<ExtractionAgentView>('/api/v1/settings/extraction-agent', fetchJson)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [check, setCheck] = useState<(HealthCheck & { provider: AgentProvider }) | null>(null)
  const statusPrefix = useId()

  async function save(next: ExtractionAgentSettings) {
    setSaving(true)
    try {
      const saved = await mutateJson<ExtractionAgentView>('/api/v1/settings/extraction-agent', { method: 'PUT', body: next })
      await mutate(saved, { revalidate: false })
      await onSaved()
      toast(`Extraction agent: ${saved.label}.`, 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'The extraction agent could not be saved.', 'error')
    } finally {
      setSaving(false)
    }
  }

  async function testSignIn(provider: AgentProvider) {
    setTesting(true)
    try {
      const result = await mutateJson<{ check: HealthCheck | null }>('/api/v1/settings/extraction-agent/test', { body: { provider } })
      setCheck(result.check ? { ...result.check, provider } : null)
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'The sign-in check failed.', 'error')
    } finally {
      setTesting(false)
    }
  }

  const settings = data?.settings
  const provider = data?.providers?.find((item) => item.id === settings?.provider)
  const model = provider?.models.find((item) => item.id === settings?.model) ?? null
  const perCourse = settings?.provider === 'gemini' && !settings.model
  const efforts = model?.efforts ?? []
  const busy = saving || !data || !settings
  const result = check && check.provider === settings?.provider ? check : null
  const isGemini = settings?.provider === 'gemini'

  function chooseProvider(id: AgentProvider) {
    if (!data || !settings || id === settings.provider) return
    const target = data.providers?.find((item) => item.id === id)
    const first = id === 'gemini' ? null : target?.models[0] ?? null
    void save({ provider: id, model: first?.id ?? null, effort: allowedEffort(settings.effort, first?.efforts ?? []) })
  }

  function chooseModel(id: string) {
    if (!settings || !provider) return
    const option = provider.models.find((item) => item.id === id)
    void save({ ...settings, model: option?.id ?? null, effort: allowedEffort(settings.effort, option?.efforts ?? []) })
  }

  const keyRow = <div className="setting">
    <div className="setting__text">
      <span className="setting__title">Gemini API key <span className={`badge ${connections?.gemini_configured ? 'badge--success' : 'badge--warning'}`}>{connections?.gemini_configured ? 'Saved' : 'Not set'}</span></span>
      <span className="setting__desc">Kept in the server's .env file and never shown again after saving.</span>
    </div>
    <div className="setting__control">
      {!isGemini ? <Button variant="ghost" size="sm" disabled={parentBusy || !connections?.gemini_configured} onClick={onTestKey}>Test key</Button> : null}
      <Button variant="secondary" icon={KeyIcon} onClick={onKey}>{connections?.gemini_configured ? 'Replace key' : 'Add key'}</Button>
    </div>
  </div>

  return <>
    <div className="settings-pane__head">
      <h2>AI extraction</h2>
      <p>One agent reads every course's agenda and decides which tasks it supports. Due dates and task identity always follow fixed rules. Changes apply from the next run.</p>
    </div>
    {error ? <Notice tone="danger">{error.message}</Notice> : null}

    <div className="choice-grid" role="radiogroup" aria-label="Extraction agent">
      {(data?.providers ?? []).map((item) => {
        const selected = item.id === settings?.provider
        return <button
          type="button"
          role="radio"
          aria-checked={selected}
          aria-label={item.label}
          aria-describedby={`${statusPrefix}-${item.id}`}
          className={`choice${selected ? ' is-selected' : ''}`}
          key={item.id}
          disabled={busy}
          onClick={() => chooseProvider(item.id)}
        >
          <span className="choice__title">{item.label}{selected ? <CheckCircleIcon className="tone-info" size={18} weight="fill" aria-hidden /> : null}</span>
          <span className="choice__desc">{PROVIDER_BLURBS[item.id]}</span>
          <span className="choice__status" id={`${statusPrefix}-${item.id}`}>
            <StatusIcon state={item.status.ready ? 'healthy' : item.status.ready === false ? 'warning' : 'missing'} size={14} />{readinessLabel(item.status.ready)}
          </span>
        </button>
      })}
    </div>

    <div className="surface">
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Model</span>
          <span className="setting__desc">{perCourse ? 'Each course keeps its own Gemini models, set under Courses, Advanced.' : 'Used for every course.'}</span>
        </div>
        <div className="setting__control">
          <select className="control" aria-label="Model" value={settings?.model ?? ''} disabled={busy} onChange={(event) => chooseModel(event.target.value)}>
            {isGemini ? <option value="">Per course</option> : null}
            {(provider?.models ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </div>
      </div>
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">{isGemini ? 'Reasoning' : 'Effort'}</span>
          <span className="setting__desc">Higher effort reads hard layouts better but takes longer{isGemini ? '' : ' and uses more of the plan'}.</span>
        </div>
        <div className="setting__control">
          {perCourse || efforts.length === 0
            ? <span className="muted">{perCourse ? 'Set per course' : `${model?.label ?? 'This model'} has no effort setting`}</span>
            : <select className="control" aria-label="Effort" value={settings?.effort} disabled={busy} onChange={(event) => settings && void save({ ...settings, effort: event.target.value as AgentEffort })}>
              {efforts.map((effort) => <option key={effort} value={effort}>{EFFORT_LABELS[effort]}</option>)}
            </select>}
        </div>
      </div>
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">{isGemini ? 'Connection' : 'Sign-in'}</span>
          <span className="setting__desc">{settings ? USAGE_NOTES[settings.provider] : ''}</span>
          {provider ? <span className="choice__status"><StatusIcon state={result?.state ?? (provider.status.ready ? 'healthy' : 'missing')} size={14} />{result?.summary ?? provider.status.detail}</span> : null}
        </div>
        <div className="setting__control">
          <Button variant="secondary" icon={PlugsConnectedIcon} loading={testing} disabled={busy} onClick={() => settings && void testSignIn(settings.provider)}>{isGemini ? 'Test connection' : 'Test sign-in'}</Button>
        </div>
      </div>
      {isGemini ? keyRow : null}
    </div>

    {!isGemini && settings ? <Disclosure title="Gemini API key" hint="Only used if you switch to Gemini">
      <div className="surface">{keyRow}</div>
    </Disclosure> : null}

    <p className="fine-print"><SparkleIcon size={14} aria-hidden />Claude and Codex run on this server, up to {data?.parallel_turns ?? 3} at a time. Switching the agent or model reads each agenda again once.</p>
  </>
}

function GoogleSection({ connections, busy, authorizing, consentUrl, onUpload, onAuthorize, onDisconnect }: {
  connections?: ConnectionStatus
  busy: boolean
  authorizing: boolean
  consentUrl: string | null
  onUpload: (file?: File) => void
  onAuthorize: () => void
  onDisconnect: () => void
}) {
  const fileInput = useRef<HTMLInputElement>(null)
  return <>
    <div className="settings-pane__head">
      <h2>Google account</h2>
      <p>Synced tasks are written to Google Tasks with your account. The credentials stay on the server.</p>
    </div>
    <div className="surface">
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">OAuth client <span className={`badge ${connections?.google_client_configured ? 'badge--success' : 'badge--warning'}`}>{connections?.google_client_configured ? 'Uploaded' : 'Missing'}</span></span>
          <span className="setting__desc">The credentials.json file for a Google Cloud desktop client.</span>
        </div>
        <div className="setting__control">
          <input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={(event) => { onUpload(event.target.files?.[0]); event.target.value = '' }} />
          <Button variant="secondary" icon={UploadSimpleIcon} disabled={busy} onClick={() => fileInput.current?.click()}>{connections?.google_client_configured ? 'Replace file' : 'Upload file'}</Button>
        </div>
      </div>
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Authorization <span className={`badge ${connections?.google_authorized ? 'badge--success' : 'badge--warning'}`}>{connections?.google_authorized ? 'Connected' : 'Not connected'}</span></span>
          <span className="setting__desc">Allows reading and writing Google Tasks, and reading the Slides pages you choose. Consent opens in a new tab.</span>
        </div>
        <div className="setting__control">
          <Button
            variant={connections?.google_authorized ? 'secondary' : 'primary'}
            icon={GoogleLogoIcon}
            loading={authorizing}
            disabled={busy || !connections?.google_client_configured}
            onClick={onAuthorize}
          >{authorizing ? 'Waiting for consent…' : connections?.google_authorized ? 'Reauthorize' : 'Authorize'}</Button>
        </div>
        {consentUrl ? <p className="fine-print setting__full">The consent tab didn't open? <a className="text-link" href={consentUrl} target="_blank" rel="noreferrer">Open the Google consent page<ArrowSquareOutIcon size={13} aria-hidden /></a></p> : null}
        {!connections?.google_client_configured ? <p className="fine-print setting__full">Upload the OAuth client first.</p> : null}
      </div>
      {connections?.google_authorized ? <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Disconnect</span>
          <span className="setting__desc">Stops writing to Google Tasks until you authorize again.</span>
        </div>
        <div className="setting__control"><Button variant="danger" disabled={busy} onClick={onDisconnect}>Disconnect</Button></div>
      </div> : null}
    </div>
  </>
}

function ExtensionSection({ data, error, busy, rotate, clear }: {
  data?: ExtensionSetup
  error?: Error
  busy: boolean
  rotate: () => void
  clear: () => void
}) {
  const { toast } = useApp()
  const capture = data?.captures?.[0]

  async function copyToken() {
    if (!data?.pairing_token) return
    try {
      await navigator.clipboard.writeText(data.pairing_token)
      toast('Pairing token copied.', 'success')
    } catch {
      toast('Copy failed. Select the token and copy it manually.', 'error')
    }
  }

  return <>
    <div className="settings-pane__head">
      <h2>Chrome extension</h2>
      <p>Optional. Only courses that use Chrome capture as their fallback need it.</p>
    </div>
    {error ? <Notice tone="danger">{error.message}</Notice> : <>
      <ol className="surface steps">
        <li className="step">
          <span className="step__num">1</span>
          <div className="step__body">
            <h3>Load the extension</h3>
            <p>Open <code>chrome://extensions</code>, turn on Developer mode, choose Load unpacked, and pick this folder in your Canvas Task Sync checkout on this computer:</p>
            <code>{data?.load_unpacked_relative_path ?? 'extension/dist'}</code>
          </div>
        </li>
        <li className="step">
          <span className="step__num">2</span>
          <div className="step__body">
            <h3>Pair it with this server</h3>
            <p>In the extension's options, enter <code>{data?.server_url ?? 'http://127.0.0.1:8890'}</code> and this token. The token only works for this loopback bridge.</p>
            <div className="token-field">
              <input className="control control--mono" aria-label="Extension pairing token" readOnly value={data?.pairing_token ?? ''} onFocus={(event) => event.currentTarget.select()} />
              <Button variant="secondary" icon={CopyIcon} disabled={!data?.pairing_token} onClick={() => void copyToken()}>Copy</Button>
              <Button variant="ghost" icon={ArrowClockwiseIcon} disabled={busy} onClick={rotate}>Rotate</Button>
            </div>
          </div>
        </li>
        <li className="step">
          <span className="step__num">3</span>
          <div className="step__body">
            <h3>Capture a file</h3>
            <p>Open the Google Slides, Docs, or Sheets file in Chrome, click the extension, choose what to send, and capture it.</p>
            <span className="choice__status">
              <StatusIcon state={capture ? 'healthy' : 'missing'} size={14} />
              {capture ? `Latest capture: ${capture.source_type.replace('google_', 'Google ')}, ${capture.item_count} items and ${capture.screenshot_count} screenshots, ${formatRelative(capture.captured_at)}` : 'No capture received yet.'}
            </span>
          </div>
        </li>
      </ol>
      <div className="setting setting--inline">
        <p className="fine-print"><LockSimpleIcon size={14} aria-hidden />Captures stay in memory for {Math.round((data?.capture_ttl_seconds ?? 900) / 60)} minutes, are never written to disk, and never include login credentials.</p>
        {capture ? <div className="setting__control"><Button variant="ghost" size="sm" icon={TrashIcon} disabled={busy} onClick={clear}>Clear captures</Button></div> : null}
      </div>
    </>}
  </>
}

function DataSection({ data, busy, updateRetention, clear }: {
  data?: SettingsResponse
  busy: boolean
  updateRetention: (days: number) => void
  clear: () => void
}) {
  return <>
    <div className="settings-pane__head">
      <h2>Data & privacy</h2>
      <p>Everything stays on this server. Nothing is sent to network storage.</p>
    </div>
    <div className="surface">
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Keep run history for</span>
          <span className="setting__desc">Older runs and their logs are deleted automatically.</span>
        </div>
        <div className="setting__control">
          <select className="control" aria-label="Run history retention" value={data?.general.history_retention_days ?? 90} onChange={(event) => updateRetention(Number(event.target.value))} disabled={busy}>
            <option value={30}>30 days</option><option value={90}>90 days</option><option value={365}>1 year</option><option value={3650}>10 years</option>
          </select>
        </div>
      </div>
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Clear run history</span>
          <span className="setting__desc">Deletes every run and its log now. Sync mappings and the extraction cache are kept.</span>
        </div>
        <div className="setting__control"><Button variant="danger" icon={TrashIcon} disabled={busy} onClick={clear}>Clear history</Button></div>
      </div>
      <div className="setting">
        <div className="setting__text">
          <span className="setting__title">Server address</span>
          <span className="setting__desc">Bound to loopback, so only this machine or an SSH tunnel can reach it.</span>
        </div>
        <div className="setting__control"><code>{data?.connections.local_server ?? '127.0.0.1:8890'}</code></div>
      </div>
    </div>
    <Disclosure title="File locations" hint="Where the server keeps its data">
      <div className="paths">
        <span>Run history and settings: <code>{data?.paths.control_database ?? '-'}</code></span>
        <span>Sync identity and cache: <code>{data?.paths.state_database ?? '-'}</code></span>
        <span>Course configuration: <code>{data?.paths.config ?? '-'}</code></span>
      </div>
    </Disclosure>
    <p className="fine-print"><ShieldCheckIcon size={14} aria-hidden />Secrets are never shown after saving, logs and debug data are sanitized, and source images aren't kept.</p>
  </>
}
