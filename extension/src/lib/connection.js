import { normalizeServerUrl } from './config.js'

export class ConnectionError extends Error {
  constructor(code, message, status = 0) {
    super(message)
    this.name = 'ConnectionError'
    this.code = code
    this.status = status
  }
}

async function request(config, path, options = {}, fetchImpl = fetch) {
  const serverUrl = normalizeServerUrl(config.serverUrl)
  if (!config.pairingToken) {
    throw new ConnectionError('pairing_token_missing', 'Paste the pairing token from the local app settings.')
  }
  let response
  try {
    response = await fetchImpl(`${serverUrl}${path}`, {
      ...options,
      headers: {
        Accept: 'application/json',
        'X-Extension-Token': config.pairingToken,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    })
  } catch (error) {
    throw new ConnectionError(
      'local_app_unreachable',
      `Could not reach ${serverUrl}. Start “canvas-task-sync web” and try again.`,
    )
  }
  if (!response.ok) {
    let payload
    try {
      payload = await response.json()
    } catch {
      payload = null
    }
    throw new ConnectionError(
      payload?.error?.code || 'local_app_rejected_request',
      payload?.error?.message || `The local app rejected the request (${response.status}).`,
      response.status,
    )
  }
  return response.status === 204 ? null : response.json()
}

export function testConnection(config, fetchImpl) {
  return request(config, '/api/v1/extension/status', {}, fetchImpl)
}

export function postCapture(config, capture, fetchImpl) {
  return request(
    config,
    '/api/v1/extension/captures',
    { method: 'POST', body: JSON.stringify(capture) },
    fetchImpl,
  )
}

export function discardCaptures(config, fetchImpl) {
  return request(config, '/api/v1/extension/captures', { method: 'DELETE' }, fetchImpl)
}

export function claimCaptureRequest(config, fetchImpl, waitSeconds = 0) {
  const boundedWait = Math.max(0, Math.min(Number(waitSeconds) || 0, 25))
  return request(
    config,
    `/api/v1/extension/capture-requests/next?wait_seconds=${boundedWait}`,
    {},
    fetchImpl,
  )
}

export function reportCaptureFailure(config, requestId, error, fetchImpl) {
  return request(
    config,
    `/api/v1/extension/capture-requests/${encodeURIComponent(requestId)}/failed`,
    {
      method: 'POST',
      body: JSON.stringify({
        code: error?.code || 'automatic_capture_failed',
        message: error instanceof Error ? error.message : String(error),
      }),
    },
    fetchImpl,
  )
}
