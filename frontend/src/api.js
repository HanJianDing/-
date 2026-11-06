let appMode = 'normal'
export function setAppMode(mode) { appMode = mode }
export function isAcceptance() { return appMode === 'acceptance' }
let csrf = ''
export function setSession(user) { csrf = user?.csrf || '' }
export async function api(path, options = {}) {
  const response = await fetch(path, { ...options, credentials: 'same-origin', headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...options.headers,
  } })
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/auth/login') window.dispatchEvent(new Event('session-expired'))
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}），请检查输入`)
  }
  return response.json()
}
