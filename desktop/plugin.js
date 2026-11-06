/**
 * Pocket Autopilot, Desktop half: a live phone pane, the routine list and a
 * step-by-step run log, a status-bar chip, and the ::routine{name="..."}
 * transcript card.
 *
 * Plain ESM with no JSX (disk plugins load uncompiled), so markup goes through
 * h(). Imports only the three specifiers the desktop loader allows.
 */
import {
  Button,
  DisclosureCaret,
  EmptyState,
  PANES_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  TRANSCRIPT_DIRECTIVE_AREA,
  atom,
  host,
  icons,
  queryClient,
  relativeTime,
  useQuery,
  useValue,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'pocket-autopilot'
const PANE_ID = `${ID}:phone`
// The live view is the one fast poll. Everything else updates from plugin events; the slow polls
// only catch what no event reports (a phone plugged in, a config edit by hand).
const FRAME_MS = 700
const STATUS_MS = 5000
const ROUTINES_MS = 30000
const RIPPLE_MS = 480
const RUN_LIMIT = 6
const LINE_LIMIT = 120
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,47}$/
const RUN_EVENTS = {
  started: `plugin.${ID}.run.started`,
  event: `plugin.${ID}.run.event`,
  finished: `plugin.${ID}.run.finished`,
}

const APP_EVENTS = {
  requested: `plugin.${ID}.apps.requested`,
  changed: `plugin.${ID}.apps.changed`,
  settings: `plugin.${ID}.settings.changed`,
}
const ROUTINES_CHANGED = `plugin.${ID}.routines.changed`
const APPS_MS = 30000

const OK = 'var(--ui-green)'
const BAD = 'var(--ui-red)'
const MUTED_COLOR = 'var(--ui-text-tertiary)'
const WARN = 'var(--ui-orange)'
const MUTED = { color: MUTED_COLOR }
const FAINT = { color: 'var(--ui-text-quaternary)' }
const LINE = '1px solid var(--ui-stroke-tertiary)'
const SECTION = { display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 12, borderTop: LINE }
const LABEL = { ...MUTED, fontSize: 11, fontWeight: 600, letterSpacing: 0.4, textTransform: 'uppercase' }
const MONO = { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11 }
const ELLIPSIS = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }
const INLINE = { display: 'inline-flex' }

/** Element helper: several children go through jsxs (static list, no key noise). */
function h(type, props, ...kids) {
  const p = { ...(props || {}) }
  if (kids.length === 0) return jsx(type, p)
  if (kids.length === 1) {
    p.children = kids[0]
    return jsx(type, p)
  }
  p.children = kids
  return jsxs(type, p)
}

// ---- shared state ---------------------------------------------------------

/** Newest first. Each run: { runId, routine, version, status, lines, failed, seq, error }. */
const $runLog = atom([])

function blankRun(payload) {
  return {
    runId: String(payload.run_id),
    routine: String(payload.routine || ''),
    version: payload.version ?? null,
    status: 'running',
    lines: [],
    failed: null,
    seq: 0,
    error: null,
  }
}

function changeRun(payload, change) {
  const runId = String(payload?.run_id || '')
  if (!runId) return
  const runs = $runLog.get()
  const seed = runs.find((r) => r.runId === runId) || blankRun(payload)
  const next = change(seed)
  $runLog.set([next, ...runs.filter((r) => r.runId !== runId)].slice(0, RUN_LIMIT))
}

function addLine(run, line) {
  const seq = run.seq + 1
  return { ...run, seq, lines: [...run.lines, { ...line, key: `${seq}` }].slice(-LINE_LIMIT) }
}

function describeEvent(p) {
  if (p.event === 'step') return { mark: '▸', tone: MUTED_COLOR, text: String(p.name || 'step') }
  if (p.event === 'checkpoint' && p.status === 'passed') return { mark: '✓', tone: OK, text: `checkpoint ${p.name}` }
  if (p.event === 'checkpoint' && p.status === 'failed') return { mark: '✗', tone: BAD, text: `checkpoint ${p.name} missed` }
  if (p.event === 'done') return { mark: '•', tone: MUTED_COLOR, text: 'done' }
  return null
}

/** Where a finished run's time went, with any helper that ran over its budget flagged. */
function timeLines(payload) {
  const list = (value) => (Array.isArray(value) ? value.map(String) : [])
  return [
    ...list(payload.over_budget).map((text) => ({ mark: '!', tone: WARN, text })),
    ...list(payload.time_spent).map((text) => ({ mark: '⏱', tone: MUTED_COLOR, text })),
  ]
}

function onRunStarted(payload) {
  changeRun(payload, (run) => run)
}

function onRunEvent(payload) {
  changeRun(payload, (run) => {
    const line = describeEvent(payload)
    if (!line) return run
    const failed =
      payload.event === 'checkpoint' && payload.status === 'failed'
        ? {
            name: String(payload.name || ''),
            expected: payload.expected ?? null,
            seen: Array.isArray(payload.seen_text) ? payload.seen_text : [],
            screenshot: payload.screenshot ?? null,
          }
        : run.failed
    return { ...addLine({ ...run, failed }, line) }
  })
  queryClient.invalidateQueries({ queryKey: [ID, 'routines'] })
}

function onRunFinished(payload) {
  changeRun(payload, (run) => {
    const fc = payload.failed_checkpoint
    const failed = fc
      ? { name: String(fc.name || ''), expected: fc.expected ?? null, seen: Array.isArray(fc.seen_text) ? fc.seen_text : [], screenshot: fc.screenshot ?? null }
      : run.failed
    let next = { ...run, status: payload.ok ? 'passed' : 'failed', failed, error: payload.error || null }
    if (payload.error) next = addLine(next, { mark: '✗', tone: BAD, text: String(payload.error) })
    next = timeLines(payload).reduce(addLine, next)
    return next
  })
  queryClient.invalidateQueries({ queryKey: [ID] })
}

// ---- backend access -------------------------------------------------------

/** ctx.rest throws on HTTP 4xx/500 with the JSON {ok:false,error} body; turn that into data. */
async function call(ctx, path, opts) {
  try {
    return await ctx.rest(path, opts)
  } catch (error) {
    return { ok: false, error: error?.message || String(error) }
  }
}

function fail(res, fallback) {
  host.notify({ kind: 'error', message: res?.error || fallback })
}

function revealPane() {
  if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
}

/** Refetch interval that pauses while the window is hidden. */
function visiblePoll(ms) {
  return () => (document.visibilityState === 'visible' ? ms : false)
}

function useStatus(ctx) {
  return useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => call(ctx, '/status'),
    refetchInterval: visiblePoll(STATUS_MS),
  })
}

function useRoutines(ctx) {
  return useQuery({
    queryKey: [ID, 'routines'],
    queryFn: () => call(ctx, '/routines'),
    refetchInterval: visiblePoll(ROUTINES_MS),
  })
}

function useFrame(ctx, enabled) {
  return useQuery({
    queryKey: [ID, 'frame'],
    queryFn: () => call(ctx, '/frame?max_w=480&quality=60'),
    enabled,
    refetchInterval: visiblePoll(FRAME_MS),
  })
}

function useApps(ctx) {
  return useQuery({
    queryKey: [ID, 'apps'],
    queryFn: () => call(ctx, '/apps'),
    refetchInterval: visiblePoll(APPS_MS),
  })
}

function routinesOf(data) {
  return Array.isArray(data?.routines) ? data.routines : []
}

function lastRunOf(list) {
  let best = null
  for (const r of list) {
    const lr = r.last_run
    if (lr && (!best || lr.finished_at > best.finished_at)) best = { ...lr, routine: r.name }
  }
  return best
}

async function startRoutine(ctx, name) {
  const res = await call(ctx, `/routines/${name}/run`, { method: 'POST', body: {} })
  if (res?.ok) queryClient.invalidateQueries({ queryKey: [ID] })
  else fail(res, `Could not run ${name}`)
  return res
}

function runReason({ approved, connected, running }) {
  if (!approved) return 'Not approved yet. Ask the agent to run it once so you can approve it.'
  if (!connected) return 'Plug in your phone to run this routine.'
  if (running) return 'Already running.'
  return 'Run this routine on the phone'
}

function lastRunText(lr) {
  return `${lr.ok ? '✓' : '✗'} ${relativeTime(lr.finished_at * 1000)}`
}

// ---- small views ----------------------------------------------------------

/** Status text per backend `state`, with the USB-debugging hint as the default. */
function disconnectedCopy(st, loading) {
  if (loading && !st) return { title: 'Looking for your phone', description: 'Checking the connection…' }
  if (st && st.phone_harness === false) {
    return { title: 'phone-harness is not installed', description: 'Install the phone-harness plugin, then retry.' }
  }
  switch (st?.state) {
    case 'locked':
      return { title: 'Unlock your phone', description: 'The screen is locked. Unlock it, then retry.' }
    case 'offline':
      return { title: 'Plug in your phone with USB debugging on', description: 'The phone is not reachable right now.' }
    default:
      return {
        title: 'Plug in your phone with USB debugging on',
        description:
          st?.error ||
          'Android: enable USB debugging and allow this computer. iPhone: use iPhone Mirroring on macOS.',
      }
  }
}

function disconnected(st, loading) {
  const copy = disconnectedCopy(st, loading)
  return h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, padding: '18px 8px' } },
    h(EmptyState, { title: copy.title, description: copy.description }),
    h(
      Button,
      {
        variant: 'outline',
        size: 'xs',
        onClick: () => queryClient.invalidateQueries({ queryKey: [ID, 'status'] }),
      },
      h(icons.RefreshCw, { className: 'size-3.5' }),
      'Retry',
    ),
  )
}

function Ripple({ ctx, spot, onDone }) {
  const [grown, setGrown] = useState(false)
  useEffect(() => {
    const grow = ctx.setTimeout(() => setGrown(true), 16)
    const done = ctx.setTimeout(onDone, RIPPLE_MS)
    return () => {
      grow()
      done()
    }
  }, [spot.id])
  return h('span', {
    style: {
      position: 'absolute',
      left: `${spot.fx * 100}%`,
      top: `${spot.fy * 100}%`,
      width: 36,
      height: 36,
      marginLeft: -18,
      marginTop: -18,
      borderRadius: '50%',
      background: 'var(--ui-accent)',
      pointerEvents: 'none',
      transform: `scale(${grown ? 1.6 : 0.3})`,
      opacity: grown ? 0 : 0.55,
      transition: `transform ${RIPPLE_MS}ms ease-out, opacity ${RIPPLE_MS}ms ease-out`,
    },
  })
}

function FailureThumb({ ctx, run }) {
  const thumb = useQuery({
    queryKey: [ID, 'thumb', run.routine, run.runId],
    queryFn: () => call(ctx, `/runs/${encodeURIComponent(run.routine)}/${encodeURIComponent(run.runId)}/thumb`),
    staleTime: Infinity,
  })
  const src = thumb.data?.ok ? thumb.data.image : null
  if (!src) return null
  return h('img', {
    src,
    alt: 'Phone screen when the checkpoint failed',
    style: { width: 120, borderRadius: 8, border: LINE, alignSelf: 'flex-start' },
  })
}

function failureBlock(ctx, f, run) {
  return h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, paddingTop: 4, borderTop: LINE } },
    h('span', { style: MUTED }, 'Expected'),
    h('span', { style: { color: 'var(--ui-text-primary)' } }, f.expected || '(no text)'),
    f.seen.length > 0 ? h('span', { style: MUTED }, `Saw: ${f.seen.slice(0, 6).join(' · ')}`) : null,
    f.screenshot ? h(FailureThumb, { ctx, run }) : null,
    f.screenshot
      ? h(
          'div',
          { style: { display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 } },
          h('span', { style: { ...MONO, ...ELLIPSIS, ...MUTED, flex: 1 } }, f.screenshot),
          h(
            Button,
            {
              variant: 'ghost',
              size: 'xs',
              onClick: () => ctx.os.revealPath(f.screenshot),
            },
            'Reveal',
          ),
        )
      : null,
  )
}

function runCard(ctx, run) {
  const tone = run.status === 'passed' ? OK : run.status === 'failed' ? BAD : MUTED_COLOR
  const label = run.status === 'running' ? 'running' : run.status === 'passed' ? '✓ passed' : '✗ failed'
  return h(
    'div',
    {
      key: run.runId,
      style: { border: LINE, borderRadius: 10, padding: 8, display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 },
    },
    h(
      'div',
      { style: { display: 'flex', justifyContent: 'space-between', gap: 8, minWidth: 0 } },
      h(
        'span',
        { style: { ...MONO, ...ELLIPSIS, color: 'var(--ui-text-primary)' } },
        run.version ? `${run.routine} v${run.version}` : run.routine,
      ),
      h('span', { style: { color: tone, fontSize: 11, whiteSpace: 'nowrap' } }, label),
    ),
    run.lines.map((line) =>
      h(
        'div',
        { key: line.key, style: { display: 'flex', gap: 6, fontSize: 12, minWidth: 0 } },
        h('span', { style: { color: line.tone } }, line.mark),
        h('span', { style: { minWidth: 0, overflowWrap: 'anywhere' } }, line.text),
      ),
    ),
    run.failed ? failureBlock(ctx, run.failed, run) : null,
  )
}

function logSection(ctx, runs) {
  return h(
    'section',
    { style: SECTION },
    h('span', { style: LABEL }, 'Live run'),
    runs.length === 0
      ? h('span', { style: { ...MUTED, fontSize: 12 } }, 'Runs show up here step by step as they happen.')
      : runs.slice(0, 3).map((run) => runCard(ctx, run)),
  )
}

function routineRow(ctx, r, { connected, running }) {
  const approved = r.approved === true
  const reason = runReason({ approved, connected, running })
  const disabled = !approved || !connected || running
  const lr = r.last_run
  return h(
    'div',
    { key: r.name, style: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } },
    h(
      'div',
      { style: { display: 'flex', flexDirection: 'column', flex: 1, minWidth: 0 } },
      h('span', { style: { ...MONO, ...ELLIPSIS, color: 'var(--ui-text-primary)' } }, r.name),
      h(
        'span',
        { style: { ...MUTED, fontSize: 11, display: 'flex', gap: 6, alignItems: 'center', minWidth: 0 } },
        h('span', null, `v${r.version}`),
        approved ? h(icons.Check, { className: 'size-3', style: { color: OK } }) : h('span', { style: FAINT }, 'not approved'),
        lr ? h('span', { style: { color: lr.ok ? OK : BAD, whiteSpace: 'nowrap' } }, lastRunText(lr)) : null,
      ),
    ),
    h(
      Tip,
      { label: reason },
      h(
        'span',
        { style: INLINE },
        h(
          Button,
          { variant: 'chip', size: 'xs', disabled, 'aria-label': `Run ${r.name}`, onClick: () => startRoutine(ctx, r.name) },
          h(icons.Play, { className: 'size-3' }),
          'Run',
        ),
      ),
    ),
  )
}

function routinesSection(ctx, routines, { connected, runningNames }) {
  const list = routinesOf(routines.data)
  return h(
    'section',
    { style: SECTION },
    h(
      'div',
      { style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' } },
      h('span', { style: LABEL }, 'Routines'),
      h('span', { style: FAINT }, String(list.length)),
    ),
    list.length === 0
      ? h(
          'span',
          { style: { ...MUTED, fontSize: 12 } },
          'No routines yet. Ask the agent to learn a task on your phone and save it.',
        )
      : list.map((r) => routineRow(ctx, r, { connected, running: runningNames.has(r.name) })),
  )
}

// ---- app permissions -------------------------------------------------------
// Allow is a person's click: the agent has no tool that grants apps, and this route needs the
// dashboard session token only the UI holds.

async function dismissApp(ctx, app) {
  const res = await call(ctx, '/apps/dismiss', { method: 'POST', body: { app } })
  if (res?.ok) queryClient.invalidateQueries({ queryKey: [ID, 'apps'] })
  else fail(res, `Could not dismiss ${app}`)
}

/** Save the allowlist the way Settings → Plugins does: plugins.manage settings, the same writer as
 *  ctx.set_config. The request disappears once the app is allowed. */
async function allowApp(app, allowed) {
  try {
    const res = await host.request('plugins.manage', {
      action: 'settings', key: ID, values: { allowed_apps: [...new Set([...allowed, app])] },
    })
    if (res && res.ok === false) throw new Error(res.error || 'settings save failed')
  } catch (error) {
    fail({ error: error?.message || String(error) }, `Could not allow ${app}`)
  }
  queryClient.invalidateQueries({ queryKey: [ID, 'apps'] })
}

function whoTried(req) {
  if (req.source === 'routine' && req.routine) return `Routine ${req.routine}`
  return 'The agent'
}

function appRequestCard(ctx, req, allowed) {
  const permission = req.kind === 'permission'
  const title = permission ? 'Android is asking for a permission' : `${whoTried(req)} tried to open an app`
  const body = permission
    ? 'Answer the dialog on your phone. A routine never taps it for you.'
    : 'It is not on your allowed apps, so the phone went Home and the script stopped.'
  const buttons = permission
    ? [h(Button, { key: 'ok', variant: 'chip', size: 'xs', onClick: () => dismissApp(ctx, req.app) }, 'Done')]
    : [
        h(Button, { key: 'allow', size: 'xs', 'aria-label': `Allow ${req.app}`, onClick: () => allowApp(req.app, allowed) },
          h(icons.Check, { className: 'size-3' }), 'Allow'),
        h(Button, { key: 'no', variant: 'chip', size: 'xs', onClick: () => dismissApp(ctx, req.app) }, 'Not now'),
      ]
  return h(
    'div',
    {
      key: req.app,
      role: 'alert',
      style: {
        display: 'flex', flexDirection: 'column', gap: 6, padding: 10, borderRadius: 10,
        border: '1px solid var(--ui-stroke-secondary)', background: 'var(--ui-bg-elevated)',
      },
    },
    h('span', { style: { fontSize: 12, fontWeight: 600, color: 'var(--ui-text-primary)' } }, title),
    h('span', { style: { ...MONO, ...ELLIPSIS, color: 'var(--ui-text-secondary)' } }, req.app),
    h('span', { style: { ...MUTED, fontSize: 11 } }, body),
    h('div', { style: { display: 'flex', gap: 6 } }, ...buttons),
  )
}

function appRequestsSection(ctx, apps) {
  const requests = Array.isArray(apps.data?.requests) ? apps.data.requests : []
  const allowed = Array.isArray(apps.data?.allowed) ? apps.data.allowed : []
  if (requests.length === 0) return null
  return h('section', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
    requests.map((req) => appRequestCard(ctx, req, allowed)))
}

function navButton(ctx, label, Icon, key, onFailed) {
  return h(
    Button,
    {
      variant: 'chip',
      size: 'xs',
      'aria-label': label,
      onClick: async () => {
        const res = await call(ctx, '/key', { method: 'POST', body: { key } })
        if (!res?.ok) onFailed(key, res)
      },
    },
    h(Icon, { className: 'size-3.5' }),
    label,
  )
}

// ---- components -----------------------------------------------------------

function PhonePane({ ctx }) {
  const [paused, setPaused] = useState(false)
  const [spot, setSpot] = useState(null)
  const [unsupported, setUnsupported] = useState({})
  const status = useStatus(ctx)
  const routines = useRoutines(ctx)
  const apps = useApps(ctx)
  const runs = useValue($runLog)
  const st = status.data || null
  const connected = st?.connected === true
  const frame = useFrame(ctx, connected && !paused)
  const shot = frame.data?.ok && frame.data.image ? frame.data : null
  const aspect = shot
    ? [shot.w, shot.h]
    : st?.screen
      ? [st.screen.w, st.screen.h]
      : [9, 19.5]
  const runningNames = new Set(runs.filter((r) => r.status === 'running').map((r) => r.routine))

  const onTap = async (event) => {
    if (!shot) return
    const box = event.currentTarget.getBoundingClientRect()
    const fx = (event.clientX - box.left) / box.width
    const fy = (event.clientY - box.top) / box.height
    if (!(fx >= 0 && fx <= 1 && fy >= 0 && fy <= 1)) return
    setSpot({ id: Date.now(), fx, fy })
    const res = await call(ctx, '/tap', {
      method: 'POST',
      body: { x: Math.round(fx * shot.src_w), y: Math.round(fy * shot.src_h) },
    })
    if (!res?.ok) fail(res, 'Tap failed')
  }

  const popOut = async () => {
    const res = await call(ctx, '/scrcpy', { method: 'POST', body: {} })
    if (!res?.ok) fail(res, 'Could not open scrcpy')
  }

  const onKeyFailed = (key, res) => {
    if (res?.status === 409 || res?.error) {
      setUnsupported((u) => ({ ...u, [key]: true }))
    }
    fail(res, `${key} is not available on this phone`)
  }

  const [aw, ah] = aspect
  const header = h(
    'div',
    { style: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } },
    h(
      Tip,
      { label: connected ? 'Connected' : 'Not connected' },
      h('span', { style: INLINE }, h(StatusDot, { tone: connected ? 'good' : 'muted' })),
    ),
    h(
      'div',
      { style: { flex: 1, minWidth: 0 } },
      h(
        'div',
        { style: { ...ELLIPSIS, fontSize: 13, fontWeight: 600, color: 'var(--ui-text-primary)' } },
        connected ? st.device || 'Phone' : 'No phone',
      ),
      h(
        'div',
        { style: { ...MUTED, fontSize: 11 } },
        connected ? (paused ? 'Live view paused' : 'Live') : 'Pocket Autopilot',
      ),
    ),
    h(
      Tip,
      { label: paused ? 'Resume live view' : 'Pause live view' },
      h(
        'span',
        { style: INLINE },
        h(
          Button,
          {
            variant: 'ghost',
            size: 'icon-xs',
            disabled: !connected,
            'aria-label': paused ? 'Resume live view' : 'Pause live view',
            onClick: () => setPaused((p) => !p),
          },
          h(paused ? icons.Play : icons.Pause, { className: 'size-3.5' }),
        ),
      ),
    ),
    h(
      Tip,
      { label: 'Pop out with scrcpy' },
      h(
        'span',
        { style: INLINE },
        h(
          Button,
          { variant: 'ghost', size: 'icon-xs', disabled: !connected, 'aria-label': 'Pop out with scrcpy', onClick: popOut },
          h(icons.ExternalLink, { className: 'size-3.5' }),
        ),
      ),
    ),
  )

  const screen = connected
    ? h(
        'div',
        { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
        h(
          'div',
          {
            style: {
              position: 'relative',
              alignSelf: 'center',
              width: `min(100%, calc(60vh * ${aw} / ${ah}))`,
              aspectRatio: `${aw} / ${ah}`,
              borderRadius: 18,
              overflow: 'hidden',
              background: 'var(--ui-bg-elevated)',
              boxShadow: 'inset 0 0 0 1px var(--ui-stroke-secondary)',
            },
          },
          shot
            ? h('img', {
                src: shot.image,
                alt: 'Phone screen. Click to tap.',
                draggable: false,
                onClick: onTap,
                style: {
                  display: 'block',
                  width: '100%',
                  height: '100%',
                  objectFit: 'contain',
                  cursor: 'pointer',
                  userSelect: 'none',
                  opacity: paused ? 0.55 : 1,
                },
              })
            : h(
                'div',
                { style: { ...MUTED, fontSize: 12, height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } },
                'Waiting for the first frame…',
              ),
          paused
            ? h(
                'span',
                {
                  style: {
                    position: 'absolute',
                    left: '50%',
                    bottom: 8,
                    transform: 'translateX(-50%)',
                    background: 'var(--ui-bg-elevated)',
                    color: 'var(--ui-text-secondary)',
                    fontSize: 11,
                    padding: '2px 8px',
                    borderRadius: 999,
                  },
                },
                'Paused',
              )
            : null,
          spot ? h(Ripple, { key: spot.id, ctx, spot, onDone: () => setSpot(null) }) : null,
        ),
        h(
          'div',
          { style: { display: 'flex', justifyContent: 'center', gap: 6, flexWrap: 'wrap' } },
          ['back', 'home', 'recents']
            .filter((key) => !unsupported[key])
            .map((key) => {
              const meta = { back: ['Back', icons.ChevronLeft], home: ['Home', icons.CircleIcon], recents: ['Recents', icons.Layers3] }[key]
              return h('span', { key, style: INLINE }, navButton(ctx, meta[0], meta[1], key, onKeyFailed))
            }),
        ),
      )
    : disconnected(st, status.isLoading)

  return h(
    'div',
    { style: { display: 'flex', flexDirection: 'column', gap: 10, padding: '12px 14px 16px', minWidth: 0, height: '100%', overflowY: 'auto' } },
    header,
    appRequestsSection(ctx, apps),
    screen,
    routinesSection(ctx, routines, { connected, runningNames }),
    logSection(ctx, runs),
  )
}

function StatusChip({ ctx }) {
  const routines = useRoutines(ctx)
  const list = routinesOf(routines.data)
  const last = lastRunOf(list)
  const label = `📱 ${list.length} routine${list.length === 1 ? '' : 's'}`
  const text = last ? `${label} · last ${last.ok ? '✓' : '✗'}` : label
  return h(
    Tip,
    { label: 'Show Pocket Autopilot' },
    h(
      'span',
      { style: INLINE },
      h(Button, { variant: 'ghost', size: 'xs', 'aria-label': 'Show Pocket Autopilot', onClick: revealPane }, text),
    ),
  )
}

function RoutineCard({ ctx, name }) {
  const [open, setOpen] = useState(false)
  const status = useStatus(ctx)
  const routines = useRoutines(ctx)
  const runs = useValue($runLog)
  const detail = useQuery({
    queryKey: [ID, 'routine', name],
    queryFn: () => call(ctx, `/routines/${name}`),
    enabled: open,
  })
  const meta = routinesOf(routines.data).find((r) => r.name === name)
  const connected = status.data?.connected === true
  const running = runs.some((r) => r.routine === name && r.status === 'running')
  const approved = meta?.approved === true
  const reason = runReason({ approved, connected, running })
  const subtitle = meta
    ? `v${meta.version} · ${approved ? 'approved' : 'not approved'}${meta.last_run ? ` · last run ${lastRunText(meta.last_run)}` : ''}`
    : routines.isLoading
      ? 'Loading…'
      : 'Not found'
  const code = detail.data?.routine?.code

  return h(
    'div',
    {
      style: { border: LINE, borderRadius: 10, padding: 10, display: 'flex', flexDirection: 'column', gap: 8, background: 'var(--ui-bg-elevated)', minWidth: 0 },
    },
    h(
      'div',
      { style: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 } },
      h(
        'div',
        { style: { flex: 1, minWidth: 0 } },
        h('div', { style: { ...ELLIPSIS, fontSize: 13, fontWeight: 600, color: 'var(--ui-text-primary)' } }, meta?.title || name),
        h('div', { style: { ...MUTED, fontSize: 11 } }, subtitle),
      ),
      h(
        Tip,
        { label: reason },
        h(
          'span',
          { style: INLINE },
          h(
            Button,
            {
              variant: 'chip',
              size: 'xs',
              disabled: !approved || !connected || running,
              'aria-label': `Run ${name}`,
              onClick: () => startRoutine(ctx, name),
            },
            h(icons.Play, { className: 'size-3' }),
            'Run',
          ),
        ),
      ),
    ),
    h(
      Button,
      { variant: 'ghost', size: 'xs', 'aria-expanded': open, onClick: () => setOpen((o) => !o), style: { alignSelf: 'flex-start' } },
      h(DisclosureCaret, { open }),
      'Show code',
    ),
    open
      ? h(
          'pre',
          {
            style: {
              ...MONO,
              margin: 0,
              padding: 8,
              borderRadius: 8,
              maxHeight: 220,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              background: 'var(--ui-bg-tertiary)',
              color: 'var(--ui-text-secondary)',
            },
          },
          detail.data?.ok === false ? detail.data.error : code || 'Loading code…',
        )
      : null,
  )
}

// ---- plugin ---------------------------------------------------------------

export default {
  id: ID,
  name: 'Pocket Autopilot',
  defaultEnabled: true,
  register(ctx) {
    ctx.onEvent(RUN_EVENTS.started, (event) => onRunStarted(event.payload || {}))
    ctx.onEvent(RUN_EVENTS.event, (event) => onRunEvent(event.payload || {}))
    ctx.onEvent(RUN_EVENTS.finished, (event) => onRunFinished(event.payload || {}))
    ctx.onEvent(APP_EVENTS.requested, () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'apps'] })
      revealPane()
    })
    ctx.onEvent(APP_EVENTS.changed, () => queryClient.invalidateQueries({ queryKey: [ID, 'apps'] }))
    ctx.onEvent(APP_EVENTS.settings, () => queryClient.invalidateQueries({ queryKey: [ID, 'apps'] }))
    ctx.onEvent(ROUTINES_CHANGED, () => queryClient.invalidateQueries({ queryKey: [ID, 'routines'] }))
    ctx.addEventListener(document, 'visibilitychange', () => {
      if (document.visibilityState === 'visible') queryClient.invalidateQueries({ queryKey: [ID] })
    })
    ctx.registerMany([
      {
        id: 'phone',
        area: PANES_AREA,
        title: 'Pocket Autopilot',
        data: { placement: 'right', width: '320px' },
        render: () => h(PhonePane, { ctx }),
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 60,
        render: () => h(StatusChip, { ctx }),
      },
      {
        id: 'routine',
        area: TRANSCRIPT_DIRECTIVE_AREA,
        data: {
          name: 'routine',
          render: ({ attrs }) => {
            const name = String(attrs?.name ?? '')
            return NAME_RE.test(name) ? h(RoutineCard, { ctx, name }) : null
          },
        },
      },
    ])
  },
}
