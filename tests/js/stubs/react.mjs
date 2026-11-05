// Hooks that run once, inline: enough to call a component body directly.
export function useState(init) {
  return [typeof init === 'function' ? init() : init, () => {}]
}
export function useEffect() {}
export function useRef(init) { return { current: init } }
export function useCallback(fn) { return fn }
export function useMemo(fn) { return fn() }
