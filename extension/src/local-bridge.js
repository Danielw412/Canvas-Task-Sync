(() => {
  const isLoopback =
    location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(location.hostname)
  if (!isLoopback) return

  function wake() {
    chrome.runtime.sendMessage({ type: 'CTS_AUTO_WAKE' }).catch(() => {})
  }

  window.addEventListener('message', (event) => {
    if (
      event.source === window
      && event.origin === location.origin
      && event.data?.source === 'canvas-task-sync-web'
      && event.data?.type === 'capture-requested'
    ) {
      wake()
    }
  })

  wake()
})()
