// Smoke test for desktop/plugin.js: loads it under stubbed SDK/React, checks the
// contributions it registers, renders the pane, chip and ::routine card bodies,
// and feeds the run events through the gateway listeners.
import { register } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

register('./stub-loader.mjs', import.meta.url)

const here = dirname(fileURLToPath(import.meta.url))
const pluginUrl = pathToFileURL(join(here, '..', '..', 'desktop', 'plugin.js')).href
const sdk = await import('./stubs/plugin-sdk.mjs')

function assert(cond, msg) {
  if (!cond) {
    console.error('FAIL:', msg)
    process.exit(1)
  }
}

// Strings anywhere in an element tree (props and children), without invoking components.
function strings(node, out = []) {
  if (node == null || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') return out.push(String(node)), out
  if (Array.isArray(node)) { node.forEach((n) => strings(n, out)); return out }
  if (typeof node === 'object') {
    for (const [k, v] of Object.entries(node.props || {})) {
      if (k !== 'ctx' && typeof v !== 'function') strings(v, out)
    }
  }
  return out
}
const textOf = (el) => strings(el.type(el.props)).join(' | ')

globalThis.document = { visibilityState: 'visible', addEventListener() {} }

const listeners = {}
const registered = []
const ctx = {
  rest: async () => { throw new Error('unused in smoke test') },
  onEvent(type, fn) { listeners[type] = fn; return () => {} },
  addEventListener() { return () => {} },
  setTimeout() { return () => {} },
  registerMany(items) { registered.push(...items); return () => {} },
  os: { revealPath() {}, writeClipboard() {} },
}

const mod = await import(pluginUrl)
const plugin = mod.default
assert(plugin.id === 'phone-autopilot', 'default export id')
assert(typeof plugin.register === 'function', 'register() exists')
plugin.register(ctx)

const pane = registered.find((c) => c.id === 'phone')
const chip = registered.find((c) => c.id === 'chip')
const directive = registered.find((c) => c.area === 'transcript.directives')
assert(pane && pane.area === 'panes' && pane.data.placement === 'right', 'pane registered on the right')
assert(chip && chip.area === 'statusBar.right' && typeof chip.render === 'function', 'status chip registered')
assert(directive && directive.data.name === 'routine' && typeof directive.data.render === 'function', 'routine directive registered')
for (const name of ['started', 'event', 'finished']) {
  assert(typeof listeners[`plugin.phone-autopilot.run.${name}`] === 'function', `listener for run.${name}`)
}

const paneText = textOf(pane.render())
assert(paneText.includes('Plug in your phone with USB debugging on'), 'pane empty state when no status')
assert(paneText.includes('Routines'), 'pane routines section')

assert(textOf(chip.render()).includes('📱 0 routines'), 'status chip label')

const card = directive.data.render({ attrs: { name: 'daily-claim' }, source: '', streaming: false, sessionId: null })
assert(card && textOf(card).includes('daily-claim'), 'directive card renders for a valid name')
assert(directive.data.render({ attrs: { name: 'Bad Name!' }, source: '', streaming: false, sessionId: null }) === null,
  'directive renders nothing for an invalid name')

const runId = '20260101-120000-abcd'
const wrap = (payload) => ({ type: 'x', payload })
listeners['plugin.phone-autopilot.run.started'](wrap({ run_id: runId, routine: 'daily-claim', version: 3 }))
listeners['plugin.phone-autopilot.run.event'](wrap({ run_id: runId, routine: 'daily-claim', event: 'checkpoint', name: 'home', status: 'passed' }))
listeners['plugin.phone-autopilot.run.event'](wrap({
  run_id: runId, routine: 'daily-claim', event: 'checkpoint', name: 'reward', status: 'failed',
  expected: 'Claim', seen_text: ['Store'], screenshot: '/tmp/x.png',
}))
listeners['plugin.phone-autopilot.run.finished'](wrap({
  run_id: runId, routine: 'daily-claim', version: 3, ok: false,
  failed_checkpoint: { name: 'reward', expected: 'Claim', seen_text: ['Store'], screenshot: '/tmp/x.png' },
}))
const afterRun = textOf(pane.render())
assert(afterRun.includes('checkpoint home'), 'passed checkpoint shows in run log')
assert(afterRun.includes('checkpoint reward missed'), 'failed checkpoint shows in run log')
assert(afterRun.includes('Expected') && afterRun.includes('Claim'), 'failed checkpoint shows expected text')
assert(afterRun.includes('/tmp/x.png'), 'failed checkpoint shows screenshot path')
assert(afterRun.includes('daily-claim v3'), 'run header shows routine and version')

for (const name of ['requested', 'changed']) {
  assert(typeof listeners[`plugin.phone-autopilot.apps.${name}`] === 'function', `listener for apps.${name}`)
}
globalThis.__queryData = {
  'phone-autopilot/apps': {
    ok: true,
    allowed: [],
    requests: [
      { app: 'com.android.settings', kind: 'app', source: 'routine', routine: 'deep-work', at: 2 },
      { app: 'com.google.android.permissioncontroller', kind: 'permission', source: 'phone_exec', at: 1 },
    ],
  },
}
const withRequests = textOf(pane.render())
assert(withRequests.includes('Routine deep-work tried to open an app'), 'app request card names the routine')
assert(withRequests.includes('com.android.settings') && withRequests.includes('Allow'), 'app request card offers Allow')
assert(withRequests.includes('Android is asking for a permission'), 'permission dialog card')
assert(withRequests.includes('Answer the dialog on your phone'), 'permission dialog is answered on the phone')
globalThis.__queryData = {}
assert(!textOf(pane.render()).includes('tried to open an app'), 'no card without requests')

const src = await import('node:fs').then((fs) => fs.readFileSync(join(here, '..', '..', 'desktop', 'plugin.js'), 'utf8'))
assert(!/\beval\s*\(|new Function|import\s*\(/.test(src), 'no eval, new Function or dynamic import')

console.log('PLUGIN_JS_SMOKE_OK')
