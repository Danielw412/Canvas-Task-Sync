import {
  Bot,
  Check,
  Copy,
  Database,
  ExternalLink,
  FileJson,
  HardDrive,
  KeyRound,
  Laptop,
  LockKeyhole,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  Trash2,
  Upload,
} from 'lucide-react'
import { useRef, useState } from 'react'
import useSWR, { mutate as globalMutate } from 'swr'
import { useApp } from '../components/AppContext'
import { Button, EmptyState, Modal, StatusIcon } from '../components/ui'
import { fetchJson, mutateJson } from '../lib/api'
import type {
  AgentEffort,
  AgentProvider,
  ConnectionStatus,
  ExtractionAgentSettings,
  ExtractionAgentView,
  GoogleAuthorizationStart,
  GoogleAuthorizationStatus,
  HealthCheck,
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

export default function SettingsPage() {
  const { toast } = useApp()
  const { data, error, mutate } = useSWR<SettingsResponse>('/api/v1/settings/connections', fetchJson)
  const { data: extension, error: extensionError, mutate: mutateExtension } = useSWR<ExtensionSetup>('/api/v1/settings/extension', fetchJson)
  const [tab, setTab] = useState<'connections' | 'general' | 'privacy'>('connections')
  const [keyModal, setKeyModal] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [authorizing, setAuthorizing] = useState(false)
  const [consentUrl, setConsentUrl] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  async function action(work: () => Promise<unknown>, success: string) {
    setBusy(true)
    try {
      await work()
      await Promise.all([mutate(), mutateExtension(), globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))])
      toast(success, 'success')
    } catch (requestError) {
      toast(requestError instanceof Error ? requestError.message : 'The action failed.', 'error')
    } finally { setBusy(false) }
  }

  async function saveKey() {
    await action(() => mutateJson('/api/v1/settings/gemini-key', { body: { api_key: apiKey } }), 'Gemini API key saved locally.')
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
      toast('Complete Google consent in the tab that opened.', 'info')
      const outcome = await pollAuthorization(started.state, new Date(started.expires_at).getTime())
      if (outcome.status === 'completed') {
        await Promise.all([mutate(), mutateExtension(), globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))])
        setConsentUrl(null)
        toast('Google authorization completed.', 'success')
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
    await action(() => mutateJson('/api/v1/settings/oauth-client', { formData }), 'OAuth desktop client saved locally.')
    if (fileInput.current) fileInput.current.value = ''
  }

  async function updateRetention(days: number) {
    await action(() => mutateJson('/api/v1/settings/general', { method: 'PUT', body: { history_retention_days: days } }), 'History retention updated.')
  }

  async function copyPairingToken() {
    if (!extension?.pairing_token) return
    try {
      await navigator.clipboard.writeText(extension.pairing_token)
      toast('Extension pairing token copied.', 'success')
    } catch {
      toast('Copy failed. Select the token and copy it manually.', 'error')
    }
  }

  if (error) return <EmptyState title="Settings could not load" body={error.message} />
  const connections = data?.connections
  return <div className="settings-page">
    <header className="page-heading page-heading--actions"><div><h1>Settings</h1><p>Connections, storage, and local data.</p></div><Button icon={RefreshCw} disabled={busy} onClick={() => void action(() => mutateJson('/api/v1/health-runs'), 'Health check queued.')}>Run all checks</Button></header>
    <div className="tab-bar settings-tabs"><button className={tab === 'connections' ? 'is-active' : ''} onClick={() => setTab('connections')}>Connections</button><button className={tab === 'general' ? 'is-active' : ''} onClick={() => setTab('general')}>General</button><button className={tab === 'privacy' ? 'is-active' : ''} onClick={() => setTab('privacy')}>Data &amp; privacy</button></div>
    <div className="settings-layout">
      <section className="settings-main">
        {tab === 'connections' ? <>
          <ExtractionAgentSection onSaved={() => Promise.all([mutate(), globalMutate((key) => typeof key === 'string' && key.includes('/api/v1/overview'))])} />
          <section className="settings-section panel"><header><h2>Google connection</h2><span className={connections?.google_authorized ? 'tone-success' : 'tone-warning'}><StatusIcon state={connections?.google_authorized ? 'healthy' : 'missing'} size={17} />{connections?.google_authorized ? 'Authorized' : 'Setup needed'}</span></header><div className="setup-row"><span className="step-number">1</span><div><strong>OAuth client file</strong><small>credentials.json</small></div><div className="setup-result"><StatusIcon state={connections?.google_client_configured ? 'healthy' : 'missing'} size={17} /><span>{connections?.google_client_configured ? 'Valid desktop client' : 'Not configured'}</span></div><input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={(event) => void uploadClient(event.target.files?.[0])} /><Button variant="secondary" icon={Upload} disabled={busy} onClick={() => fileInput.current?.click()}>{connections?.google_client_configured ? 'Replace file' : 'Upload file'}</Button></div><div className="setup-row"><span className="step-number">2</span><div><strong>Google authorization</strong><small>Tasks and Slides access</small></div><div className="scope-list"><span><Check size={14} />Google Tasks · Read and write</span><span><Check size={14} />Google Slides · Read selected presentation pages</span></div><Button variant="secondary" disabled={busy || authorizing || !connections?.google_client_configured} onClick={() => void authorizeGoogle()}>{authorizing ? 'Waiting for consent…' : connections?.google_authorized ? 'Reauthorize' : 'Authorize'}</Button></div>{consentUrl ? <p className="setup-hint">Consent did not open? <a href={consentUrl} target="_blank" rel="noreferrer">Open the Google authorization page<ExternalLink size={13} /></a></p> : null}{connections?.google_authorized ? <button className="settings-danger-row" disabled={busy} onClick={() => { if (window.confirm('Disconnect Google access? Your OAuth client file remains, but token.json is removed from active use.')) void action(() => mutateJson('/api/v1/settings/google/disconnect'), 'Google access disconnected.') }}><span>Disconnect</span><small>Disconnect Google access for Tasks and Slides.</small></button> : null}</section>
          <section className="settings-section panel"><header><h2>Gemini API</h2><span className={connections?.gemini_configured ? 'tone-success' : 'tone-warning'}><StatusIcon state={connections?.gemini_configured ? 'healthy' : 'missing'} size={17} />{connections?.gemini_configured ? 'Configured' : 'Setup needed'}</span></header><div className="setup-row"><span className="step-number">1</span><div><strong>API key</strong><small>Stored locally in .env and never returned by the API</small></div><div className="masked-key">••••••••••••••••••••••••</div><div className="button-cluster"><Button variant="secondary" icon={KeyRound} onClick={() => setKeyModal(true)}>{connections?.gemini_configured ? 'Replace key' : 'Add key'}</Button><Button variant="secondary" disabled={busy || !connections?.gemini_configured} onClick={() => void action(() => mutateJson('/api/v1/settings/gemini/test'), 'Gemini connection passed.')}>Test connection</Button></div></div><div className="setup-row"><span className="step-number">2</span><div><strong>Models and reasoning</strong><small>Per course on the Courses page, or one Gemini model for every course under Extraction agent</small></div></div></section>
          <ChromeConnectorSection data={extension} error={extensionError} busy={busy} copyToken={copyPairingToken} rotate={() => action(() => mutateJson('/api/v1/settings/extension/rotate'), 'Extension pairing token rotated. Paste the new token into the extension.')} clear={() => action(() => mutateJson('/api/v1/settings/extension/captures', { method: 'DELETE' }), 'In-memory browser captures cleared.')} />
          <LocalServerSection address={connections?.local_server ?? '127.0.0.1:8890'} />
        </> : null}
        {tab === 'general' ? <><LocalServerSection address={connections?.local_server ?? '127.0.0.1:8890'} /><section className="settings-section panel"><header><h2>App behavior</h2></header><div className="setting-row"><div><strong>Default course</strong><small>Use the course selected in the top bar.</small></div><span>Follow current selection</span></div><div className="setting-row"><div><strong>Browser launch</strong><small>The CLI opens this control center by default.</small></div><span>Use <code>--no-open</code> to disable</span></div></section></> : null}
        {tab === 'privacy' ? <DataPrivacy data={data} busy={busy} updateRetention={updateRetention} clear={() => { if (window.confirm('Clear all run history and sanitized debug events? Sync mappings and extraction cache are kept.')) void action(() => mutateJson('/api/v1/history', { method: 'DELETE' }), 'Run history cleared.') }} /> : null}
      </section>
      <aside className="settings-rail inspector-rail"><section className="rail-section"><h2>Connection checks</h2><div className="connection-check-list">{connections?.checks.map((check) => <div key={check.key}><span className="connection-icon">{check.key.includes('oauth') ? <FileJson size={19} /> : check.key.includes('extraction') ? <Bot size={19} /> : check.key.includes('gemini') ? <Sparkles size={19} /> : <Database size={19} />}</span><div><strong>{check.label}</strong><small>{check.summary}</small></div><StatusIcon state={check.state} /><span>{check.state === 'healthy' ? 'OK' : 'Check'}</span></div>)}</div><a href="/diagnostics" className="inline-link">View diagnostics <ExternalLink size={15} /></a></section><section className="security-list"><h2>Security</h2><div><LockKeyhole size={19} /><span>Secrets are never shown after saving</span></div><div><ShieldCheck size={19} /><span>Logs and debug metadata are sanitized</span></div><div><HardDrive size={19} /><span>Source images are not retained</span></div><div><Laptop size={19} /><span>Credentials stay on this computer</span></div></section></aside>
    </div>
    {keyModal ? <Modal title={connections?.gemini_configured ? 'Replace Gemini API key' : 'Add Gemini API key'} onClose={() => setKeyModal(false)} footer={<><Button variant="secondary" onClick={() => setKeyModal(false)}>Cancel</Button><Button icon={KeyRound} disabled={busy || apiKey.length < 8} onClick={() => void saveKey()}>Save key</Button></>}><label className="form-field"><span>API key</span><input aria-label="API key" type="password" autoFocus autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /><small>The key is written to your local .env file. It will not be returned or logged.</small></label></Modal> : null}
  </div>
}

const EFFORT_LABELS: Record<AgentEffort, string> = {
  low: 'Low · fastest',
  medium: 'Medium · recommended',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max · most thorough',
}

const USAGE_NOTES: Record<AgentProvider, string> = {
  gemini: 'Gemini API key in .env (see Gemini API below)',
  claude: "This server's Claude Code sign-in · draws from plan usage",
  codex: "This server's Codex (ChatGPT) sign-in · draws from plan usage",
}

function allowedEffort(effort: AgentEffort, efforts: AgentEffort[]): AgentEffort {
  return efforts.length === 0 || efforts.includes(effort) ? effort : 'medium'
}

// One agent and model for every course's extraction. Each change saves immediately, so
// switching agents is a single click; the next run uses it.
function ExtractionAgentSection({ onSaved }: { onSaved: () => Promise<unknown> }) {
  const { toast } = useApp()
  const { data, error, mutate } = useSWR<ExtractionAgentView>('/api/v1/settings/extraction-agent', fetchJson)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [check, setCheck] = useState<(HealthCheck & { provider: AgentProvider }) | null>(null)

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

  if (error) return <section className="settings-section panel"><header><h2>Extraction agent</h2></header><p className="local-note tone-danger">{error.message}</p></section>
  const settings = data?.settings
  const provider = data?.providers?.find((item) => item.id === settings?.provider)
  const model = provider?.models.find((item) => item.id === settings?.model) ?? null
  const perCourse = settings?.provider === 'gemini' && !settings.model
  const efforts = model?.efforts ?? []
  const busy = saving || !data || !settings
  const result = check && check.provider === settings?.provider ? check : null
  const ready = result ? result.state === 'healthy' : provider?.status.ready

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

  return <section className="settings-section panel extraction-agent">
    <header><h2>Extraction agent</h2><span className={ready ? 'tone-success' : 'tone-warning'}><StatusIcon state={ready ? 'healthy' : 'missing'} size={17} />{!data ? 'Loading…' : ready ? 'Ready' : 'Setup needed'}</span></header>
    <div className="setup-row"><span className="step-number">1</span><div><strong>Agent</strong><small>Reads every course's agenda; applies to the next run</small></div><div className="filter-tabs agent-switch" role="radiogroup" aria-label="Extraction agent">{(data?.providers ?? []).map((item) => <button type="button" role="radio" aria-checked={item.id === settings?.provider} className={item.id === settings?.provider ? 'is-active' : ''} key={item.id} disabled={busy} onClick={() => chooseProvider(item.id)}>{item.label}</button>)}</div></div>
    <div className="setup-row"><span className="step-number">2</span><div><strong>Model</strong><small>{perCourse ? 'Each course keeps its own Gemini models' : 'Used for every course'}</small></div><label className="select-control"><span className="sr-only">Model</span><select aria-label="Model" value={settings?.model ?? ''} disabled={busy} onChange={(event) => chooseModel(event.target.value)}>{settings?.provider === 'gemini' ? <option value="">Per course (Courses page)</option> : null}{(provider?.models ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label></div>
    <div className="setup-row"><span className="step-number">3</span><div><strong>{settings?.provider === 'gemini' ? 'Reasoning' : 'Effort'}</strong><small>Higher effort reads hard layouts better but takes longer{settings?.provider === 'gemini' ? '' : ' and uses more plan usage'}</small></div>{perCourse || efforts.length === 0 ? <span className="setup-result">{perCourse ? 'Set per course' : `${model?.label ?? 'This model'} has no effort setting`}</span> : <label className="select-control"><span className="sr-only">Effort</span><select aria-label="Effort" value={settings?.effort} disabled={busy} onChange={(event) => settings && void save({ ...settings, effort: event.target.value as AgentEffort })}>{efforts.map((effort) => <option key={effort} value={effort}>{EFFORT_LABELS[effort]}</option>)}</select></label>}</div>
    <div className="setup-row"><span className="step-number">4</span><div><strong>Sign-in and usage</strong><small>{settings ? USAGE_NOTES[settings.provider] : ''}</small></div><div className="setup-result"><StatusIcon state={result?.state ?? (provider?.status.ready ? 'healthy' : 'missing')} size={17} /><span>{result?.summary ?? provider?.status.detail ?? ''}</span></div><Button variant="secondary" disabled={busy || testing} onClick={() => settings && void testSignIn(settings.provider)}>{testing ? 'Checking…' : settings?.provider === 'gemini' ? 'Test connection' : 'Test sign-in'}</Button></div>
    <p className="local-note"><Bot size={15} />Claude and Codex run on this server, up to {data?.parallel_turns ?? 3} at once; further runs wait for a free slot. A new agent or model extracts each agenda again once.</p>
  </section>
}

function ChromeConnectorSection({ data, error, busy, copyToken, rotate, clear }: { data?: ExtensionSetup; error?: Error; busy: boolean; copyToken: () => Promise<void>; rotate: () => Promise<void>; clear: () => Promise<void> }) {
  const capture = data?.captures?.[0]
  return <section className="settings-section panel"><header><h2>Chrome source connector</h2><span className={capture ? 'tone-success' : 'tone-warning'}><StatusIcon state={capture ? 'healthy' : 'warning'} size={17} />{capture ? 'Capture ready' : 'Waiting for capture'}</span></header>{error ? <p className="tone-danger">{error.message}</p> : <><div className="setup-row"><span className="step-number">1</span><div><strong>Load the unpacked extension</strong><small>Open <code>chrome://extensions</code>, enable Developer mode, choose Load unpacked, and select this folder inside your Canvas Task Sync checkout <em>on this computer</em>:</small><small><code>{data?.load_unpacked_relative_path ?? 'extension/dist'}</code></small></div></div><div className="setup-row"><span className="step-number">2</span><div><strong>Pair with this local app</strong><small>Use server <code>{data?.server_url ?? 'http://127.0.0.1:8890'}</code>. The token authorizes only this loopback bridge.</small></div><input className="extension-token" aria-label="Extension pairing token" readOnly value={data?.pairing_token ?? ''} onFocus={(event) => event.currentTarget.select()} /><div className="button-cluster"><Button variant="secondary" icon={Copy} disabled={!data?.pairing_token} onClick={() => void copyToken()}>Copy token</Button><Button variant="secondary" icon={RotateCcw} disabled={busy} onClick={() => void rotate()}>Rotate</Button></div></div><div className="setup-row"><span className="step-number">3</span><div><strong>Capture the open file</strong><small>Open Slides, Docs, or Sheets in Chrome, click the extension, choose portions and a mode, then send the capture.</small></div>{capture ? <div className="setup-result"><StatusIcon state="healthy" size={17} /><span>{capture.source_type.replace('google_', '')} · {capture.item_count} items · {capture.screenshot_count} screenshots</span></div> : null}</div><p className="local-note"><LockKeyhole size={15} />Captures stay in memory for {Math.round((data?.capture_ttl_seconds ?? 900) / 60)} minutes, are never written to disk, and contain no exported login credentials.</p>{capture ? <button className="settings-danger-row" disabled={busy} onClick={() => void clear()}><span>Clear browser captures</span><small>Immediately removes all pending in-memory source content.</small></button> : null}</>}</section>
}

function LocalServerSection({ address }: { address: string }) {
  return <section className="settings-section panel"><header><h2>Local server</h2><span className="tone-success"><StatusIcon state="healthy" size={17} />Connected</span></header><div className="setting-row"><span className="step-number">1</span><div><strong>Address</strong><small>{address}</small></div></div><div className="setting-row"><span className="step-number">2</span><div><strong>Binding</strong><small>Loopback only · Only this computer can open the control center.</small></div></div></section>
}

function DataPrivacy({ data, busy, updateRetention, clear }: { data?: SettingsResponse; busy: boolean; updateRetention: (days: number) => Promise<void>; clear: () => void }) {
  return <section className="settings-section panel data-privacy"><header><h2>Data &amp; privacy</h2></header><div className="setting-row"><span className="step-number">1</span><div><strong>Run history retention</strong><small>Automatically delete operational history older than this.</small></div><select value={data?.general.history_retention_days ?? 90} onChange={(event) => void updateRetention(Number(event.target.value))} disabled={busy}><option value={30}>30 days</option><option value={90}>90 days</option><option value={365}>1 year</option><option value={3650}>Keep for 10 years</option></select></div><div className="setting-row"><span className="step-number">2</span><div><strong>Clear run history…</strong><small>Delete all runs and sanitized logs from the operational database.</small></div><Button variant="danger" icon={Trash2} disabled={busy} onClick={clear}>Clear history</Button></div><div className="setting-row setting-row--paths"><span className="step-number">3</span><div><strong>File locations</strong><small><code>{data?.paths.control_database}</code> · Operational history and settings</small><small><code>{data?.paths.state_database}</code> · Sync identity and cache data</small></div></div><p className="local-note"><LockKeyhole size={15} />All data stays on this computer. No network storage is used.</p></section>
}
