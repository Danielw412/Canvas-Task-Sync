import {
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
import type { ConnectionStatus } from '../types'

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
  captures: { source_type: string; captured_at: string; item_count: number; screenshot_count: number }[]
}

export default function SettingsPage() {
  const { toast } = useApp()
  const { data, error, mutate } = useSWR<SettingsResponse>('/api/v1/settings/connections', fetchJson)
  const { data: extension, error: extensionError, mutate: mutateExtension } = useSWR<ExtensionSetup>('/api/v1/settings/extension', fetchJson)
  const [tab, setTab] = useState<'connections' | 'general' | 'privacy'>('connections')
  const [keyModal, setKeyModal] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
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
          <section className="settings-section panel"><header><h2>Google connection</h2><span className={connections?.google_authorized ? 'tone-success' : 'tone-warning'}><StatusIcon state={connections?.google_authorized ? 'healthy' : 'missing'} size={17} />{connections?.google_authorized ? 'Authorized' : 'Setup needed'}</span></header><div className="setup-row"><span className="step-number">1</span><div><strong>OAuth client file</strong><small>credentials.json</small></div><div className="setup-result"><StatusIcon state={connections?.google_client_configured ? 'healthy' : 'missing'} size={17} /><span>{connections?.google_client_configured ? 'Valid desktop client' : 'Not configured'}</span></div><input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={(event) => void uploadClient(event.target.files?.[0])} /><Button variant="secondary" icon={Upload} disabled={busy} onClick={() => fileInput.current?.click()}>{connections?.google_client_configured ? 'Replace file' : 'Upload file'}</Button></div><div className="setup-row"><span className="step-number">2</span><div><strong>Google authorization</strong><small>Tasks and Slides access</small></div><div className="scope-list"><span><Check size={14} />Google Tasks · Read and write</span><span><Check size={14} />Google Slides · Read selected presentation pages</span></div><Button variant="secondary" disabled={busy || !connections?.google_client_configured} onClick={() => void action(() => mutateJson('/api/v1/settings/google/authorize'), 'Google authorization completed.')}>{connections?.google_authorized ? 'Reauthorize' : 'Authorize'}</Button></div>{connections?.google_authorized ? <button className="settings-danger-row" disabled={busy} onClick={() => { if (window.confirm('Disconnect Google access? Your OAuth client file remains, but token.json is removed from active use.')) void action(() => mutateJson('/api/v1/settings/google/disconnect'), 'Google access disconnected.') }}><span>Disconnect</span><small>Disconnect Google access for Tasks and Slides.</small></button> : null}</section>
          <section className="settings-section panel"><header><h2>Gemini API</h2><span className={connections?.gemini_configured ? 'tone-success' : 'tone-warning'}><StatusIcon state={connections?.gemini_configured ? 'healthy' : 'missing'} size={17} />{connections?.gemini_configured ? 'Configured' : 'Setup needed'}</span></header><div className="setup-row"><span className="step-number">1</span><div><strong>API key</strong><small>Stored locally in .env and never returned by the API</small></div><div className="masked-key">••••••••••••••••••••••••</div><div className="button-cluster"><Button variant="secondary" icon={KeyRound} onClick={() => setKeyModal(true)}>{connections?.gemini_configured ? 'Replace key' : 'Add key'}</Button><Button variant="secondary" disabled={busy || !connections?.gemini_configured} onClick={() => void action(() => mutateJson('/api/v1/settings/gemini/test'), 'Gemini connection passed.')}>Test connection</Button></div></div><div className="setup-row"><span className="step-number">2</span><div><strong>Models and reasoning</strong><small>Configured separately for each class on the Courses page</small></div></div></section>
          <ChromeConnectorSection data={extension} error={extensionError} busy={busy} copyToken={copyPairingToken} rotate={() => action(() => mutateJson('/api/v1/settings/extension/rotate'), 'Extension pairing token rotated. Paste the new token into the extension.')} clear={() => action(() => mutateJson('/api/v1/settings/extension/captures', { method: 'DELETE' }), 'In-memory browser captures cleared.')} />
          <LocalServerSection address={connections?.local_server ?? '127.0.0.1:8790'} />
        </> : null}
        {tab === 'general' ? <><LocalServerSection address={connections?.local_server ?? '127.0.0.1:8790'} /><section className="settings-section panel"><header><h2>App behavior</h2></header><div className="setting-row"><div><strong>Default course</strong><small>Use the course selected in the top bar.</small></div><span>Follow current selection</span></div><div className="setting-row"><div><strong>Browser launch</strong><small>The CLI opens this control center by default.</small></div><span>Use <code>--no-open</code> to disable</span></div></section></> : null}
        {tab === 'privacy' ? <DataPrivacy data={data} busy={busy} updateRetention={updateRetention} clear={() => { if (window.confirm('Clear all run history and sanitized debug events? Sync mappings and extraction cache are kept.')) void action(() => mutateJson('/api/v1/history', { method: 'DELETE' }), 'Run history cleared.') }} /> : null}
      </section>
      <aside className="settings-rail inspector-rail"><section className="rail-section"><h2>Connection checks</h2><div className="connection-check-list">{connections?.checks.map((check) => <div key={check.key}><span className="connection-icon">{check.key.includes('oauth') ? <FileJson size={19} /> : check.key.includes('gemini') ? <Sparkles size={19} /> : <Database size={19} />}</span><div><strong>{check.label}</strong><small>{check.summary}</small></div><StatusIcon state={check.state} /><span>{check.state === 'healthy' ? 'OK' : 'Check'}</span></div>)}</div><a href="/diagnostics" className="inline-link">View diagnostics <ExternalLink size={15} /></a></section><section className="security-list"><h2>Security</h2><div><LockKeyhole size={19} /><span>Secrets are never shown after saving</span></div><div><ShieldCheck size={19} /><span>Logs and debug metadata are sanitized</span></div><div><HardDrive size={19} /><span>Source images are not retained</span></div><div><Laptop size={19} /><span>Credentials stay on this computer</span></div></section></aside>
    </div>
    {keyModal ? <Modal title={connections?.gemini_configured ? 'Replace Gemini API key' : 'Add Gemini API key'} onClose={() => setKeyModal(false)} footer={<><Button variant="secondary" onClick={() => setKeyModal(false)}>Cancel</Button><Button icon={KeyRound} disabled={busy || apiKey.length < 8} onClick={() => void saveKey()}>Save key</Button></>}><label className="form-field"><span>API key</span><input aria-label="API key" type="password" autoFocus autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /><small>The key is written to your local .env file. It will not be returned or logged.</small></label></Modal> : null}
  </div>
}

function ChromeConnectorSection({ data, error, busy, copyToken, rotate, clear }: { data?: ExtensionSetup; error?: Error; busy: boolean; copyToken: () => Promise<void>; rotate: () => Promise<void>; clear: () => Promise<void> }) {
  const capture = data?.captures?.[0]
  return <section className="settings-section panel"><header><h2>Chrome source connector</h2><span className={capture ? 'tone-success' : 'tone-warning'}><StatusIcon state={capture ? 'healthy' : 'warning'} size={17} />{capture ? 'Capture ready' : 'Waiting for capture'}</span></header>{error ? <p className="tone-danger">{error.message}</p> : <><div className="setup-row"><span className="step-number">1</span><div><strong>Load the unpacked extension</strong><small>Open <code>chrome://extensions</code>, enable Developer mode, choose Load unpacked, and select:</small><small><code>{data?.load_unpacked_path ?? 'extension/dist'}</code></small></div></div><div className="setup-row"><span className="step-number">2</span><div><strong>Pair with this local app</strong><small>Use server <code>{data?.server_url ?? 'http://127.0.0.1:8790'}</code>. The token authorizes only this loopback bridge.</small></div><input className="extension-token" aria-label="Extension pairing token" readOnly value={data?.pairing_token ?? ''} onFocus={(event) => event.currentTarget.select()} /><div className="button-cluster"><Button variant="secondary" icon={Copy} disabled={!data?.pairing_token} onClick={() => void copyToken()}>Copy token</Button><Button variant="secondary" icon={RotateCcw} disabled={busy} onClick={() => void rotate()}>Rotate</Button></div></div><div className="setup-row"><span className="step-number">3</span><div><strong>Capture the open file</strong><small>Open Slides, Docs, or Sheets in Chrome, click the extension, choose portions and a mode, then send the capture.</small></div>{capture ? <div className="setup-result"><StatusIcon state="healthy" size={17} /><span>{capture.source_type.replace('google_', '')} · {capture.item_count} items · {capture.screenshot_count} screenshots</span></div> : null}</div><p className="local-note"><LockKeyhole size={15} />Captures stay in memory for {Math.round((data?.capture_ttl_seconds ?? 900) / 60)} minutes, are never written to disk, and contain no exported login credentials.</p>{capture ? <button className="settings-danger-row" disabled={busy} onClick={() => void clear()}><span>Clear browser captures</span><small>Immediately removes all pending in-memory source content.</small></button> : null}</>}</section>
}

function LocalServerSection({ address }: { address: string }) {
  return <section className="settings-section panel"><header><h2>Local server</h2><span className="tone-success"><StatusIcon state="healthy" size={17} />Connected</span></header><div className="setting-row"><span className="step-number">1</span><div><strong>Address</strong><small>{address}</small></div></div><div className="setting-row"><span className="step-number">2</span><div><strong>Binding</strong><small>Loopback only · Only this computer can open the control center.</small></div></div></section>
}

function DataPrivacy({ data, busy, updateRetention, clear }: { data?: SettingsResponse; busy: boolean; updateRetention: (days: number) => Promise<void>; clear: () => void }) {
  return <section className="settings-section panel data-privacy"><header><h2>Data &amp; privacy</h2></header><div className="setting-row"><span className="step-number">1</span><div><strong>Run history retention</strong><small>Automatically delete operational history older than this.</small></div><select value={data?.general.history_retention_days ?? 90} onChange={(event) => void updateRetention(Number(event.target.value))} disabled={busy}><option value={30}>30 days</option><option value={90}>90 days</option><option value={365}>1 year</option><option value={3650}>Keep for 10 years</option></select></div><div className="setting-row"><span className="step-number">2</span><div><strong>Clear run history…</strong><small>Delete all runs and sanitized logs from the operational database.</small></div><Button variant="danger" icon={Trash2} disabled={busy} onClick={clear}>Clear history</Button></div><div className="setting-row setting-row--paths"><span className="step-number">3</span><div><strong>File locations</strong><small><code>{data?.paths.control_database}</code> · Operational history and settings</small><small><code>{data?.paths.state_database}</code> · Sync identity and cache data</small></div></div><p className="local-note"><LockKeyhole size={15} />All data stays on this computer. No network storage is used.</p></section>
}
