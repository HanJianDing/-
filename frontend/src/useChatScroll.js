import {useLayoutEffect, useRef} from 'react'
import {isNearLatest} from './interaction'

export function useChatScroll(id, conversation) {
  const messagesRef = useRef(null), contentRef = useRef(null)
  const follow = useRef(true), ready = useRef(false)
  useLayoutEffect(() => { follow.current = true; ready.current = false }, [id])
  useLayoutEffect(() => {
    ready.current = !!conversation && conversation.id === id
    const element = messagesRef.current
    if (ready.current && follow.current && element) element.scrollTop = element.scrollHeight
  }, [id, conversation])
  useLayoutEffect(() => {
    const observer = new ResizeObserver(() => {
      const element = messagesRef.current
      if (ready.current && follow.current && element) element.scrollTop = element.scrollHeight
    })
    if (contentRef.current) observer.observe(contentRef.current)
    return () => observer.disconnect()
  }, [])
  return {messagesRef, contentRef, onScroll: event => { follow.current = isNearLatest(event.currentTarget) }}
}
