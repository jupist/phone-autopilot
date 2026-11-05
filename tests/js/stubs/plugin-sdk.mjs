// Minimal @hermes/plugin-sdk stand-in. Only the names plugin.js imports.
export const PANES_AREA = 'panes'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TRANSCRIPT_DIRECTIVE_AREA = 'transcript.directives'

export const notifications = []
export const host = {
  notify(msg) { notifications.push(msg) },
  navigate() {},
}

export function atom(initial) {
  let value = initial
  const listeners = new Set()
  return {
    get: () => value,
    set(next) { value = next; listeners.forEach((fn) => fn(value)) },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn) },
  }
}
export function useValue(store) { return store.get() }

export const queryClient = { invalidateQueries() {} }
export function useQuery() { return { data: undefined, isLoading: false } }

export function relativeTime(ms) { return `t${Math.round(ms)}` }

const comp = (name) => function Stub(props) { return { type: name, props } }
export const Button = comp('Button')
export const Tip = comp('Tip')
export const EmptyState = comp('EmptyState')
export const StatusDot = comp('StatusDot')
export const DisclosureCaret = comp('DisclosureCaret')
export const icons = new Proxy({}, { get: (_, key) => comp(String(key)) })
