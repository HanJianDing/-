// Shared interaction rules; these do not bypass API permissions or execution guards.
export function resetAccountNavigation({location, history, setRoute, setQuestion}) {
  history.replaceState(null, '', location.pathname + location.search + '#dashboard')
  setQuestion('')
  setRoute('dashboard')
}

export function handleComposerKeyDown(event, {busy, text}) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey ||
      event.isComposing || event.nativeEvent?.isComposing || event.keyCode === 229 || event.nativeEvent?.keyCode === 229) return
  event.preventDefault()
  if (!busy && !event.repeat && text.trim()) event.currentTarget.form?.requestSubmit()
}

export function isNearLatest(element) {
  return element.scrollHeight - element.clientHeight - element.scrollTop <= 24
}
