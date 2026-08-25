import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { BackendExit, DesktopBootProgress } from '@/global'
import { translateNow } from '@/i18n'
import { $desktopBoot, DESKTOP_BOOT_DEGRADED_PHASE } from '@/store/boot'
import { closeSecondaryGateways, isActivePrimary } from '@/store/gateway'
import { reconnectGateway } from '@/store/gateway-reconnect'
import { $notifications } from '@/store/notifications'
import { $activeGatewayProfile, $profiles, ensureGatewayProfile } from '@/store/profile'
import { $connection, $currentCwd, $gatewayState } from '@/store/session'

import {
  LOCAL_PRIMARY_BACKEND_SERVICE,
  REMOTE_PRIMARY_BACKEND_SERVICE,
  type StartupServiceRecord
} from '../../../../electron/startup-service-gate'

import { takeGatewaySurvivor } from './gateway-hmr-survivor'
import { useGatewayBoot } from './use-gateway-boot'

// End-to-end-ish repro of the "remote VPS → stuck on CONNECTING, no Settings"
// bug that drives the REAL useGatewayBoot hook + REAL HermesGateway through a
// fake WebSocket we fully control. No Docker / no real port: from the desktop's
// point of view a "remote VPS" is just a WebSocket that opens once and later
// refuses to reopen, so that is exactly (and only) what we fake.
//
// The previous test (gateway-connecting-overlay.test.tsx) hand-set the stores
// and asserted the overlays; this one proves the HOOK actually PRODUCES that
// stuck store combo — closing the "inferred by reading code" gap on the
// post-boot reconnect loop.

type Listener = (ev: unknown) => void
let connectionApplied: null | (() => void) = null
// Main's "the backend child died" signal, captured so a test can fire it in the
// exact window it lands in production: during the cold start, or after it.
let backendExit: null | ((payload: BackendExit) => void) = null

// Minimal WebSocket stand-in implementing only what json-rpc-gateway.connect()
// touches: readyState, add/removeEventListener('open'|'error'|'close'), close().
class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  // Flipped by the test: 'open' = next socket connects; 'fail' = next socket
  // errors (a dead remote); 'park' = the handshake hangs until the test
  // releases it. Mirrors a VPS going away after the first connect.
  static mode: 'open' | 'fail' | 'park' = 'open'
  static instances: FakeWebSocket[] = []

  readyState = 0
  private listeners: Record<string, Set<Listener>> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)

    // 'park' = the handshake never settles on its own, so a test can tear the
    // effect down while connect() is still awaiting THIS socket.
    if (FakeWebSocket.mode === 'park') {
      return
    }

    const willOpen = FakeWebSocket.mode === 'open'
    // Resolve on the next microtask/macrotask so connect()'s promise wiring is
    // in place before open/error fires (matches real async socket handshake).
    setTimeout(() => {
      if (willOpen) {
        this.readyState = FakeWebSocket.OPEN
        this.emit('open', {})
      } else {
        this.readyState = FakeWebSocket.CLOSED
        this.emit('error', {})
      }
    }, 0)
  }

  // Complete a parked handshake, as a slow backend finally accepting would.
  // A socket that was already closed stays closed: a real WebSocket cannot
  // reopen, so neither may this one.
  openNow() {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return
    }

    this.readyState = FakeWebSocket.OPEN
    this.emit('open', {})
  }

  addEventListener(type: string, fn: Listener) {
    ;(this.listeners[type] ??= new Set()).add(fn)
  }

  removeEventListener(type: string, fn: Listener) {
    this.listeners[type]?.delete(fn)
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  // Force-drop an open socket, as a sleeping laptop / restarted remote would.
  drop() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', {})
  }

  private emit(type: string, ev: unknown) {
    for (const fn of this.listeners[type] ?? []) {
      fn(ev)
    }
  }
}

const primaryConn = {
  authMode: 'token' as const,
  baseUrl: 'https://vps.example.com',
  profile: 'default',
  token: 't',
  wsUrl: 'wss://vps.example.com/api/ws?token=t'
}

const coderConn = {
  authMode: 'token' as const,
  baseUrl: 'https://coder.example.com',
  profile: 'coder',
  token: 'c',
  wsUrl: 'wss://coder.example.com/api/ws?token=c'
}

// Main's boot-progress snapshot as the renderer reads it. `startupServices` is
// optional on purpose: a main process that predates the startup-service ledger
// publishes no rows at all, which is the compatibility ladder's other side —
// the default fake below is exactly that runtime.
type FakeBootProgress = DesktopBootProgress

function fakeDesktop() {
  return {
    getConnection: vi.fn(async (profile?: null | string) => {
      const key = (profile ?? '').trim()

      return !key || key === 'default' ? primaryConn : coderConn
    }),
    getGatewayWsUrl: vi.fn(async (conn?: { wsUrl?: string }) => conn?.wsUrl ?? primaryConn.wsUrl),
    getBootProgress: vi.fn(async (): Promise<FakeBootProgress> => ({
      error: null,
      fakeMode: false,
      message: '',
      phase: 'init',
      progress: 0,
      retryable: false,
      running: true,
      timestamp: Date.now()
    })),
    onBootProgress: vi.fn(() => () => undefined),
    onBackendExit: vi.fn((callback: (payload: BackendExit) => void) => {
      backendExit = callback

      return () => {
        backendExit = null
      }
    }),
    onConnectionApplied: vi.fn(callback => {
      connectionApplied = callback

      return () => {
        connectionApplied = null
      }
    }),
    onPowerResume: vi.fn(() => () => undefined),
    revalidateConnection: vi.fn(async () => ({ ok: true, rebuilt: false })),
    onWindowStateChanged: vi.fn(() => () => undefined),
    touchBackend: vi.fn(async () => undefined),
    profile: { get: vi.fn(async () => ({ profile: 'default' })) }
  }
}

function Harness({
  beforeConnectionSwitch = () => undefined,
  refreshHermesConfig,
  refreshSessions
}: {
  beforeConnectionSwitch?: () => void
  refreshHermesConfig?: () => Promise<void>
  refreshSessions?: () => Promise<void>
} = {}) {
  useGatewayBoot({
    beforeConnectionSwitch,
    handleGatewayEvent: () => undefined,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig: refreshHermesConfig ?? (async () => undefined),
    refreshSessions: refreshSessions ?? (async () => undefined)
  })

  // Stands in for the app shell. Revealing/keeping the window is NOT a
  // readiness verdict, so a degraded startup must leave this mounted and
  // renderable behind the one degraded status.
  return <div data-testid="app-shell" />
}

// One of main's authoritative startup-service records, as the renderer reads
// it off the boot-progress snapshot. `via` is the probe evidence.
function ledgerRow(id: string, status: 'failed' | 'ready', detail: string | null = null): StartupServiceRecord {
  return {
    at: Date.now(),
    detail,
    id,
    label: id === REMOTE_PRIMARY_BACKEND_SERVICE ? 'Remote Hermes gateway' : 'Local Hermes backend',
    owner: 'main:startHermes',
    status,
    via: 'main:startHermes /api/health ladder'
  }
}

// Main's boot-progress snapshot for a primary backend that failed to start,
// carrying the ledger row for the primary this mode required.
function failedBootProgress(id: string, message: string, retryable = false): FakeBootProgress {
  return {
    error: message,
    fakeMode: false,
    message: `Desktop boot failed: ${message}`,
    phase: 'backend.error',
    progress: 24,
    retryable,
    running: false,
    startupServices: [ledgerRow(id, 'failed', message)],
    timestamp: Date.now()
  }
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  // Drop any parked gateway left by a prior file/case (globalThis slot).
  const leftover = takeGatewaySurvivor()

  if (leftover) {
    try {
      leftover.gateway.close()
    } catch {
      // ignore
    }
  }

  closeSecondaryGateways()
  $activeGatewayProfile.set('default')
  $connection.set(null)
  $profiles.set([])
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  window.history.replaceState({}, '', '/')
  connectionApplied = null
  backendExit = null
  $notifications.set([])
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = fakeDesktop()
  $gatewayState.set('idle')
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: '',
    phase: 'init',
    progress: 0,
    running: true,
    timestamp: Date.now(),
    visible: true
  })
})

afterEach(() => {
  cleanup()
  // Vitest keeps import.meta.hot truthy, so the boot effect's cleanup parks an
  // open gateway instead of tearing it down (the real HMR path). Drain + close
  // that survivor so the next test boots a fresh socket instead of adoptBoot().
  const survivor = takeGatewaySurvivor()

  if (survivor) {
    try {
      survivor.gateway.close()
    } catch {
      // ignore
    }
  }

  closeSecondaryGateways()
  $activeGatewayProfile.set('default')
  $connection.set(null)
  $profiles.set([])
  $notifications.set([])
  backendExit = null
  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
  window.localStorage.removeItem('hermes.desktop.workspace-cwd')
  $currentCwd.set('')
  window.history.replaceState({}, '', '/')
})

// Let pending microtasks (awaits) AND the queued 0ms socket open/error fire.
async function flushAsync() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

// Drive the exponential backoff forward by its full cap so the next scheduled
// reconnect attempt actually runs (1s,2s,4s,8s,15s,15s…). Returns after the
// attempt's async work settles.
async function advanceBackoff() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })
}

// The boot phases that ARE a terminal status, in publication order.
// "One failure episode, one status" is an ordering claim, not an end-state
// one: a legacy failDesktopBoot() that lands before the aggregate gate's
// normalized report is still two statuses even though the last one wins.
const TERMINAL_BOOT_PHASES = new Set(['renderer.error', 'renderer.ready', DESKTOP_BOOT_DEGRADED_PHASE])

function recordTerminalStatuses() {
  const phases: string[] = []

  const off = $desktopBoot.listen(state => {
    if (TERMINAL_BOOT_PHASES.has(state.phase) && phases.at(-1) !== state.phase) {
      phases.push(state.phase)
    }
  })

  return { off, phases }
}

// Main's boot-progress snapshot for a primary backend main itself recorded as
// ready — the ledger row a startup-time child exit has to override.
function readyBootProgress(id: string): FakeBootProgress {
  return {
    error: null,
    fakeMode: false,
    message: 'Hermes backend is ready',
    phase: 'backend.ready',
    progress: 94,
    retryable: false,
    running: true,
    startupServices: [ledgerRow(id, 'ready')],
    timestamp: Date.now()
  }
}

describe('useGatewayBoot remote reconnect loop (real hook, fake socket)', () => {
  it('INITIAL boot against a dead VPS: getConnection hangs (waitForHermes) → app sits in the connecting combo, then fails', async () => {
    // The report's actual path: a fresh launch pointed at an unreachable VPS.
    // startHermes()'s remote branch awaits waitForHermes() for 45s before it
    // throws, so the renderer's `await desktop.getConnection()` stays pending
    // that whole window. During it: gatewayState is still 'idle' (connect was
    // never reached) and boot.error is null → connecting=true → the fullscreen
    // CONNECTING overlay, latched, blocking Settings.
    let rejectConn: (e: Error) => void = () => undefined
    let connectionAttempts = 0
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(() => {
      connectionAttempts += 1

      // First call: the dead-VPS wait (main's waitForHermes). Any later call —
      // the startup gate's ONE bounded repair through the same owner — meets
      // main's already-failed start and rejects at once.
      return connectionAttempts === 1
        ? new Promise((_resolve, reject) => {
            rejectConn = reject
          })
        : Promise.reject(new Error('Hermes backend did not become ready: timeout'))
    })
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // getConnection is still pending — the dead-VPS wait. No socket was ever
    // created, gatewayState never left idle, boot.error is null.
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // ^ connecting === true here → fullscreen CONNECTING, no Settings.

    // After ~45s waitForHermes gives up and getConnection rejects → boot()
    // catch → failDesktopBoot → the BootFailureOverlay recovery surface.
    await act(async () => {
      rejectConn(new Error('Hermes backend did not become ready: timeout'))
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('resets the old machine context before connecting an applied gateway', async () => {
    const beforeConnectionSwitch = vi.fn()
    render(<Harness beforeConnectionSwitch={beforeConnectionSwitch} />)
    await flushAsync()
    expect(connectionApplied).not.toBeNull()

    act(() => connectionApplied?.())
    expect(beforeConnectionSwitch).toHaveBeenCalledTimes(1)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')
  })

  it('re-fetches the profile rail from the NEW backend after a connection apply (#85731)', async () => {
    // The reported repro: connected to backend A, the rail shows A's named
    // profiles; the user applies a different remote/Cloud connection (soft
    // re-home). The rail must repopulate from backend B — before the fix
    // nothing deterministically re-pulled /api/profiles on the soft switch,
    // so the rail kept (or, with a stale in-flight response, collapsed to)
    // the previous backend's list.
    const desktop = fakeDesktop() as ReturnType<typeof fakeDesktop> & {
      api: ReturnType<typeof vi.fn>
    }

    desktop.api = vi.fn(async ({ path }: { path: string }) => {
      if (path === '/api/profiles/active') {
        return { active: 'default', current: 'default' }
      }

      if (path === '/api/profiles') {
        return {
          profiles: [
            { is_default: true, name: 'default' },
            { is_default: false, name: 'cloud-eric' }
          ]
        }
      }

      throw new Error(`unexpected api call: ${path}`)
    })
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    // The rail currently mirrors backend A's profile universe.
    $profiles.set([
      { is_default: true, name: 'default' },
      { is_default: false, name: 'eric' }
    ] as never)

    // Settings → Gateway apply lands: main tears down softly and notifies.
    act(() => connectionApplied?.())
    await flushAsync()
    await flushAsync()

    expect($gatewayState.get()).toBe('open')
    // Backend B's list replaced A's — the rail survives the switch instead of
    // painting the previous backend's (or an empty) universe.
    expect($profiles.get().map(profile => profile.name)).toEqual(['default', 'cloud-eric'])
  })

  it('a remote that drops post-boot keeps looping with NO boot.error (the dead-end CONNECTING combo)', async () => {
    render(<Harness />)
    await flushAsync()

    // Initial boot connected.
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    expect(FakeWebSocket.instances).toHaveLength(1)

    // The remote VPS goes away: drop the live socket, and make every reopen
    // fail from here on.
    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Burn a couple backoff cycles BEFORE the escalation threshold (<6 attempts,
    // ~the first ~15s). This is the window where stock and fixed behave the
    // same: socket down, hook retrying, gatewayState non-open, boot.error still
    // null → CONNECTING covers the screen with no recovery surface. (Past ~45s
    // the fix raises boot.error; that's asserted in the next test.)
    await advanceBackoff()

    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // It is actively retrying, not idle — more sockets were minted.
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
  })

  it('FIX: after the prolonged drop the hook raises a recoverable boot error (the escape hatch)', async () => {
    render(<Harness />)
    await flushAsync()
    expect($desktopBoot.get().error).toBeNull()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Walk the backoff past the >=6 attempt threshold (~45s of failures).
    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    // The hook surfaced the recoverable error → BootFailureOverlay (Use local
    // gateway / Sign in / Retry) becomes reachable instead of CONNECTING.
    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('FIX: a successful reconnect clears the recoverable error', async () => {
    render(<Harness />)
    await flushAsync()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    expect($desktopBoot.get().error).toBeTruthy()

    // The remote comes back: next reconnect attempt opens.
    FakeWebSocket.mode = 'open'
    await advanceBackoff()

    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('manual reconnect revalidates, re-resolves, re-mints, and re-dials the dropped socket', async () => {
    const desktop = fakeDesktop()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    expect($gatewayState.get()).toBe('open')
    act(() => FakeWebSocket.instances[0].drop())
    FakeWebSocket.mode = 'open'

    await act(async () => {
      const reconnect = reconnectGateway()
      await vi.advanceTimersByTimeAsync(0)
      await reconnect
    })

    expect(desktop.revalidateConnection).toHaveBeenCalledOnce()
    // The manual reconnect dials the WINDOW-owned primary backend (no profile
    // arg) — same contract as the sleep/wake reconnect: passing the active
    // profile would retarget the primary socket after a live profile swap.
    const lastCall = desktop.getConnection.mock.calls.at(-1) ?? []
    expect(lastCall.length === 0 || lastCall[0] == null || lastCall[0] === '').toBe(true)
    expect(desktop.getGatewayWsUrl).toHaveBeenCalledTimes(2)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect($gatewayState.get()).toBe('open')
  })

  it('FIX: a failed session-list fetch during boot is non-fatal — the app still boots', async () => {
    // The version-skew report: gateway WS connects fine, but refreshSessions()
    // rejects (e.g. older backend 404s an endpoint the fallback didn't cover,
    // or a transient read error). That must NOT reject boot() into
    // failDesktopBoot's "Hermes couldn't start" overlay — the socket is open
    // and the app is fully usable with an empty sidebar.
    const refreshSessions = vi.fn(async () => {
      throw new Error('404: {"detail":"No such API endpoint: /api/profiles/sessions/sidebar"}')
    })

    render(<Harness refreshSessions={refreshSessions} />)
    await flushAsync()

    expect(refreshSessions).toHaveBeenCalled()
    expect($gatewayState.get()).toBe('open')
    // Boot completed: no error, overlay dismissed.
    expect($desktopBoot.get().error).toBeNull()
    expect($desktopBoot.get().visible).toBe(false)
    expect($desktopBoot.get().phase).toBe('renderer.ready')
  })

  it('seeds the configured default project dir pre-connect — no route-resume race (#71873)', async () => {
    // The reporter's scenario: a configured default project dir must be applied
    // at boot regardless of route-resume timing. The seed now runs BEFORE the
    // gateway opens, so no session restore can race it (route-resume is gated
    // on gatewayState === 'open').
    const desktop = fakeDesktop() as {
      sanitizeWorkspaceCwd?: unknown
      settings?: unknown
    }

    desktop.settings = {
      getDefaultProjectDir: vi.fn(async () => ({
        defaultLabel: 'C:\\Users\\sonny',
        dir: 'C:\\Hermes',
        resolvedCwd: 'C:\\Hermes'
      })),
      pickDefaultProjectDir: vi.fn(async () => undefined),
      setDefaultProjectDir: vi.fn(async () => undefined)
    }
    desktop.sanitizeWorkspaceCwd = vi.fn(async (cwd: string) => ({ cwd }))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    // Record the cwd at the exact moment the gateway opens its WebSocket: if
    // the seed moved back post-connect, this would still be '' here and the
    // end-state assertion would pass anyway (the seed would run later in the
    // same flush). The construction-time snapshot is what proves ordering.
    let cwdAtConnect = ''

    class RecordingSocket extends FakeWebSocket {
      constructor(url: string) {
        super(url)
        cwdAtConnect = $currentCwd.get()
      }
    }

    ;(globalThis as { WebSocket: unknown }).WebSocket = RecordingSocket

    render(<Harness />)
    await flushAsync()

    expect(cwdAtConnect).toBe('C:\\Hermes')
    expect($currentCwd.get()).toBe('C:\\Hermes')
  })

  it('FIX: primary sleep/wake reconnect dials the window backend, not the active secondary profile', async () => {
    const desktop = fakeDesktop()

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toBe(primaryConn.wsUrl)

    // Profile swap opens a secondary WS; briefly use real timers so that
    // handshake isn't wedged behind the suite's fake clock.
    vi.useRealTimers()
    await ensureGatewayProfile('coder')
    vi.useFakeTimers()

    expect(isActivePrimary()).toBe(false)
    expect($activeGatewayProfile.get()).toBe('coder')
    expect($connection.get()?.profile).toBe('coder')
    expect($connection.get()?.baseUrl).toBe(coderConn.baseUrl)

    const callsBeforeDrop = desktop.getConnection.mock.calls.length
    const socketsBeforeDrop = FakeWebSocket.instances.length
    const primarySocket = FakeWebSocket.instances[0]

    act(() => primarySocket.drop())
    await flushAsync()
    await advanceBackoff()

    const reconnectCalls = desktop.getConnection.mock.calls.slice(callsBeforeDrop)
    expect(reconnectCalls.some(args => (args[0] ?? '').trim() === 'coder')).toBe(false)
    expect(reconnectCalls.some(args => args.length === 0 || args[0] == null || args[0] === '')).toBe(true)

    const primaryReconnectSockets = FakeWebSocket.instances
      .slice(socketsBeforeDrop)
      .filter(socket => socket.url === primaryConn.wsUrl)

    expect(primaryReconnectSockets.length).toBeGreaterThan(0)
    expect($connection.get()?.profile).toBe('coder')
    expect($connection.get()?.baseUrl).toBe(coderConn.baseUrl)
  })

  it('FIX #82679: a transient remote boot failure self-heals — the next attempt rebuilds the dropped connection', async () => {
    // The reported class: the app relaunches (or wakes) against a registered
    // SSH/HTTP remote whose transport dropped. startHermes() rejects with a
    // transient transport error ("Could not verify the existing SSH backend"),
    // main tags the boot progress `retryable`, and — before the fix — the app
    // parked on "Desktop boot failed" until the user re-entered the exact same
    // connection details. Now the renderer retries the boot with backoff and
    // the second attempt (fresh bootstrap, same details) succeeds.
    const desktop = fakeDesktop()
    desktop.getConnection = vi
      .fn()
      .mockRejectedValueOnce(new Error('Could not verify the existing SSH backend.'))
      .mockImplementation(async () => primaryConn)
    desktop.getBootProgress = vi.fn(async () => ({
      error: 'Could not verify the existing SSH backend.',
      fakeMode: false,
      message: 'Desktop boot failed: Could not verify the existing SSH backend.',
      phase: 'backend.error',
      progress: 24,
      retryable: true,
      running: false,
      timestamp: Date.now()
    }))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // First attempt failed but the failure is retryable: no terminal error,
    // the overlay shows the retry status instead of the dead-end failure.
    expect($desktopBoot.get().error).toBeNull()
    expect($gatewayState.get()).not.toBe('open')

    // Walk past the first backoff delay (2s base, 15s cap, full jitter).
    await advanceBackoff()

    // Second boot attempt rebuilt the connection — no manual re-entry.
    expect(desktop.getConnection.mock.calls.length).toBeGreaterThan(1)
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('FIX #82679: boot retries are BOUNDED — a persistently dead remote ends in the recovery overlay, not a spinner', async () => {
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(async () => {
      throw new Error('Could not verify the existing SSH backend.')
    })
    desktop.getBootProgress = vi.fn(async () => ({
      error: 'Could not verify the existing SSH backend.',
      fakeMode: false,
      message: 'Desktop boot failed: Could not verify the existing SSH backend.',
      phase: 'backend.error',
      progress: 24,
      retryable: true,
      running: false,
      timestamp: Date.now()
    }))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // Exhaust the bounded retry budget (5 attempts, ≤15s jittered delay each).
    for (let i = 0; i < 7; i += 1) {
      await advanceBackoff()
    }

    // 1 initial + 5 bounded retries; the loop then STOPS retrying and the
    // exhausted failure is handed to the aggregate startup gate, which spends
    // exactly ONE more bounded repair through the SAME existing owner
    // (revalidate + the single-flight getConnection) before publishing the one
    // degraded status that surfaces the real recovery affordance.
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(7)
    expect($desktopBoot.get().error).toBeTruthy()

    // No further attempts after the budget is spent — bounded, not infinite,
    // and the gate never repairs a second time.
    await advanceBackoff()
    expect(desktop.getConnection).toHaveBeenCalledTimes(7)
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
  })

  it('FIX #82679: a NON-retryable boot failure (local / confirmed reauth) fails immediately without auto-retry', async () => {
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(async () => {
      throw new Error('401: gateway session expired')
    })
    desktop.getBootProgress = vi.fn(async () => ({
      error: '401: gateway session expired',
      fakeMode: false,
      message: 'Desktop boot failed: 401: gateway session expired',
      phase: 'backend.error',
      progress: 24,
      retryable: false,
      running: false,
      timestamp: Date.now()
    }))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    expect($desktopBoot.get().error).toBeTruthy()
    // One boot attempt, then the aggregate gate's single bounded repair through
    // the existing owner. That is not a retry loop: the boot is never
    // re-attempted.
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)

    // Still no retry later: a missing capability is not a transient failure.
    await advanceBackoff()
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })
})

describe('useGatewayBoot cold-start startup-service acceptance (real hook seam)', () => {
  it('a required LOCAL primary-backend failure enters the aggregate gate: one owner repair, one normalized degraded status, and completeDesktopBoot() is never called', async () => {
    // The bypass this suite exists to catch: main's required primary backend
    // never came up, so `getConnection()` rejects BEFORE the gate's usual
    // entry point. That failure must still land in the same aggregate
    // boundary — one repair through the existing owner, one re-probe, one
    // normalized degraded report — instead of the raw fail/notify path.
    const failure = 'Hermes backend did not become ready: timeout'
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(async () => {
      throw new Error(failure)
    })
    desktop.getBootProgress = vi.fn(async () => failedBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE, failure))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()
    await flushAsync()

    const boot = $desktopBoot.get()

    // ONE normalized degraded status naming service / cause / recovery / next
    // action — not a popup storm, and not a completed boot.
    expect(boot.phase).toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect(boot.error).toContain('Local Hermes backend')
    expect(boot.error).toContain(failure)
    expect(boot.error).toContain('One bounded repair through main:startHermes')
    expect(boot.error).toContain('Repair install')

    // completeDesktopBoot() unreachable: the boot is not ready, and the shell
    // stays mounted/renderable behind the status.
    expect(boot.progress).toBeLessThan(100)
    expect(boot.running).toBe(false)
    expect(boot.visible).toBe(true)
    expect(screen.getByTestId('app-shell')).toBeTruthy()

    // Exactly one repair, through the EXISTING owner (revalidate + the same
    // single-flight getConnection): no second backend owner, no retry loop,
    // and no renderer-side duplicate of main's HTTP/WebSocket ladder.
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
    expect(FakeWebSocket.instances).toHaveLength(0)

    await advanceBackoff()
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })

  it('a required REMOTE primary-backend failure names the remote row: the mode comes from main’s ledger, not from a connection that never resolved', async () => {
    const failure = 'Remote gateway session has expired.'
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(async () => {
      throw new Error(failure)
    })
    desktop.getBootProgress = vi.fn(async () => failedBootProgress(REMOTE_PRIMARY_BACKEND_SERVICE, failure))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()
    await flushAsync()

    const boot = $desktopBoot.get()

    expect(boot.phase).toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect(boot.error).toContain('Remote Hermes gateway')
    expect(boot.error).toContain('Settings → Gateway')
    // Remote mode never touches the local-only row.
    expect(boot.error).not.toContain('Local Hermes backend')
    // The cause survives verbatim, so the overlay's reauth detection (and its
    // "Sign in" affordance) still resolves off the boot error.
    expect(boot.error).toContain(failure)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })

  it('a ledger-aware cold start completes only after every mode-required row passed', async () => {
    const desktop = fakeDesktop()
    desktop.getBootProgress = vi.fn(async () => ({
      error: null,
      fakeMode: false,
      message: 'Hermes backend is ready',
      phase: 'backend.ready',
      progress: 94,
      retryable: false,
      running: true,
      startupServices: [ledgerRow(LOCAL_PRIMARY_BACKEND_SERVICE, 'ready')],
      timestamp: Date.now()
    }))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // Primary backend (main's own probe evidence), gateway socket, settings —
    // all three required rows passed, so the boot may declare itself ready.
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().phase).toBe('renderer.ready')
    expect($desktopBoot.get().progress).toBe(100)
    expect($desktopBoot.get().visible).toBe(false)
    expect($desktopBoot.get().error).toBeNull()
  })

  it('a profile-pinned helper window requires its pool backend and ignores the unrelated primary ledger row', async () => {
    window.history.replaceState({}, '', '/?win=hud&profile=coder')
    const desktop = fakeDesktop()
    desktop.getBootProgress = vi.fn(async () => readyBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    expect(desktop.getConnection).toHaveBeenCalledWith('coder')
    expect(desktop.revalidateConnection).not.toHaveBeenCalled()
    expect($connection.get()?.profile).toBe('coder')
    expect($desktopBoot.get().phase).toBe('renderer.ready')
  })

  it('a gateway-socket repair republishes the re-minted descriptor before declaring readiness', async () => {
    const repairedConn = {
      ...primaryConn,
      baseUrl: 'https://vps-repaired.example.com',
      token: 'repaired-token',
      wsUrl: 'wss://vps-repaired.example.com/api/ws?token=repaired-token'
    }

    const desktop = fakeDesktop()
    let connectionCalls = 0
    desktop.getConnection = vi.fn(async () => {
      connectionCalls += 1

      return connectionCalls === 1 ? primaryConn : repairedConn
    })
    desktop.getBootProgress = vi.fn(async () => readyBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    let configLoads = 0

    const refreshHermesConfig = vi.fn(async () => {
      configLoads += 1

      if (configLoads === 1) {
        FakeWebSocket.instances[0]?.drop()
      }
    })

    render(<Harness refreshHermesConfig={refreshHermesConfig} />)
    await flushAsync()
    await flushAsync()
    await flushAsync()
    await act(async () => {
      FakeWebSocket.instances.at(-1)?.openNow()
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
    expect($connection.get()?.baseUrl).toBe(repairedConn.baseUrl)
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().phase).toBe('renderer.ready')
  })

  it('a required settings failure degrades at the real seam: the socket stays open, the shell stays mounted, and boot is not completed', async () => {
    // The integration sentinel for the one load-bearing call site: if the hook
    // ever calls completeDesktopBoot() directly again instead of going through
    // the aggregate gate, this goes green-to-red.
    const refreshHermesConfig = vi.fn(async () => {
      throw new Error('500: settings unavailable')
    })

    render(<Harness refreshHermesConfig={refreshHermesConfig} />)
    await flushAsync()
    await flushAsync()

    // One probe, one bounded repair through the row's existing owner.
    expect(refreshHermesConfig).toHaveBeenCalledTimes(2)
    expect($gatewayState.get()).toBe('open')

    const boot = $desktopBoot.get()
    expect(boot.phase).toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect(boot.progress).toBeLessThan(100)
    expect(boot.visible).toBe(true)
    expect(boot.error).toContain('Hermes settings')
    expect(boot.error).toContain('500: settings unavailable')
    expect(screen.getByTestId('app-shell')).toBeTruthy()
  })

  it('unmounting mid-repair cancels the gate: no stale reconnect, no completion, no degraded status after teardown', async () => {
    const desktop = fakeDesktop()
    let releaseRepair: () => void = () => undefined
    let repairParked = false
    let connectionAttempts = 0

    desktop.getConnection = vi.fn(async () => {
      connectionAttempts += 1

      if (connectionAttempts > 1) {
        // The gateway-socket row's ONE repair, parked in flight so the effect
        // can be torn down underneath it.
        await new Promise<void>(resolve => {
          repairParked = true
          releaseRepair = resolve
        })
      }

      return primaryConn
    })
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    // Drop the live socket while the pre-gate fetches run, so the gateway-socket
    // row is genuinely not ready when the gate probes it and its owner repair
    // is the one that gets cancelled.
    const refreshHermesConfig = vi.fn(async () => {
      FakeWebSocket.instances[0]?.drop()
    })

    const view = render(<Harness refreshHermesConfig={refreshHermesConfig} />)
    await flushAsync()
    await flushAsync()

    expect(repairParked).toBe(true)
    const socketsBeforeTeardown = FakeWebSocket.instances.length
    const phaseBeforeTeardown = $desktopBoot.get().phase

    await act(async () => {
      view.unmount()
    })

    await act(async () => {
      releaseRepair()
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    // The repair resumed after teardown and did nothing: no re-dial of a socket
    // this effect no longer owns…
    expect(FakeWebSocket.instances).toHaveLength(socketsBeforeTeardown)
    expect(desktop.getGatewayWsUrl).toHaveBeenCalledTimes(1)
    // …and no verdict published for a window that is gone.
    expect($desktopBoot.get().phase).toBe(phaseBeforeTeardown)
    expect($desktopBoot.get().phase).not.toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect($desktopBoot.get().phase).not.toBe('renderer.ready')
  })
})

describe('useGatewayBoot startup backend-exit reporting (real hook seam)', () => {
  it('a child exit during a FAILED primary cold start publishes one normalized degraded status and no parallel toast', async () => {
    // The remaining half of the "one status" contract: the child that main
    // started dies while the cold start is still running. The exit listener
    // used to publish failDesktopBoot() plus a persistent error toast right
    // there — a second, differently-shaped status for the SAME failure
    // episode, landing BEFORE the aggregate gate's normalized report.
    const failure = 'Hermes backend did not become ready: timeout'
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(async () => {
      throw new Error(failure)
    })
    desktop.getBootProgress = vi.fn(async () => failedBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE, failure))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    const statuses = recordTerminalStatuses()
    render(<Harness />)

    expect(backendExit).not.toBeNull()
    act(() => backendExit?.({ code: 1, signal: null }))

    await flushAsync()
    await flushAsync()
    statuses.off()

    // ONE terminal status for one failure episode, and it is the gate's — the
    // legacy 'renderer.error' state never gets published ahead of it.
    expect(statuses.phases).toEqual([DESKTOP_BOOT_DEGRADED_PHASE])
    expect($notifications.get()).toHaveLength(0)

    const boot = $desktopBoot.get()
    expect(boot.phase).toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect(boot.error).toContain('Local Hermes backend')
    expect(boot.error).toContain(failure)
    expect(boot.error).toContain('One bounded repair through main:startHermes')
    expect(boot.error).toContain('Repair install')

    // Unchanged around the repair: not completed, shell still mounted, and
    // exactly one bounded repair through the existing owner.
    expect(boot.progress).toBeLessThan(100)
    expect(boot.running).toBe(false)
    expect(boot.visible).toBe(true)
    expect(screen.getByTestId('app-shell')).toBeTruthy()
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })

  it('a child exit during a cold start whose ledger says READY blocks completion and degrades on the exit', async () => {
    // The dangerous shape of the same race: main recorded the primary row
    // ready (its probes DID pass), the socket opened, and the child died
    // afterwards but still inside the startup episode. Suppressing the exit
    // listener's own status must not mean the exit is ignored — the row that
    // owns the backend has to judge it, or the gate declares a dead install
    // ready.
    const desktop = fakeDesktop()
    let connectionCalls = 0
    desktop.getConnection = vi.fn(async () => {
      connectionCalls += 1

      // The gate's ONE bounded repair meets a backend that is really gone.
      if (connectionCalls > 1) {
        throw new Error('Hermes backend is not running.')
      }

      return primaryConn
    })
    desktop.getBootProgress = vi.fn(async () => readyBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    const statuses = recordTerminalStatuses()
    render(<Harness />)

    expect(backendExit).not.toBeNull()
    act(() => backendExit?.({ code: 1, signal: null }))

    await flushAsync()
    await flushAsync()
    statuses.off()

    expect(statuses.phases).toEqual([DESKTOP_BOOT_DEGRADED_PHASE])
    expect($notifications.get()).toHaveLength(0)

    const boot = $desktopBoot.get()
    // The normalized report names the service, the exit as the cause, the one
    // attempted recovery, and the next action.
    expect(boot.phase).toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect(boot.error).toContain('Local Hermes backend')
    expect(boot.error).toContain(translateNow('boot.errors.backgroundExitedDuringStartup'))
    expect(boot.error).toContain('One bounded repair through main:startHermes failed')
    expect(boot.error).toContain('Repair install')
    expect(boot.progress).toBeLessThan(100)
    expect(boot.visible).toBe(true)
    expect(screen.getByTestId('app-shell')).toBeTruthy()

    // Still exactly one repair through the existing owner, never two.
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })

  it('a child exit whose owner repair SUCCEEDS rebinds to the repaired connection BEFORE readiness', async () => {
    // The other half of the ready-ledger exit, and the dangerous one: the exit
    // lands AFTER this boot already dialed its socket, so `connected` records a
    // binding to a process that is now dead. The owner rebuilds the backend
    // successfully — main's ledger goes ready again — but the renderer is still
    // holding the pre-exit socket. Spending the exit evidence and returning on
    // that stale `connected` would let the ONE re-probe read the fresh ledger
    // and declare a dead endpoint ready.
    //
    // The socket is deliberately left OPEN across the exit: a child's death and
    // its socket's close event are different events, and the close may not have
    // landed yet. Readiness must not depend on that race. It also makes the
    // gateway row's `gatewayOpen()` probe pass on the zombie socket, which is
    // exactly how a stale binding survives to completion.
    const desktop = fakeDesktop()
    // A respawned backend re-mints its session token, so the repaired
    // connection is a genuinely different endpoint than the dead one.
    const respawnedConn = { ...primaryConn, wsUrl: 'wss://vps.example.com/api/ws?token=respawned' }
    let connectionCalls = 0
    desktop.getConnection = vi.fn(async () => {
      connectionCalls += 1

      // The gate's ONE bounded repair meets an owner that CAN bring it back.
      return connectionCalls === 1 ? primaryConn : respawnedConn
    })
    // Every dial mints against whichever connection the owner has handed out.
    desktop.getGatewayWsUrl = vi.fn(async () => (connectionCalls > 1 ? respawnedConn.wsUrl : primaryConn.wsUrl))
    desktop.getBootProgress = vi.fn(async () => readyBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    // Park the first settings load so the child can die at the one moment that
    // matters: after the socket opened, still inside the startup episode.
    let releaseConfig: () => void = () => undefined
    let configCalls = 0

    const refreshHermesConfig = vi.fn(() => {
      configCalls += 1

      return configCalls === 1
        ? new Promise<void>(resolve => {
            releaseConfig = resolve
          })
        : Promise.resolve()
    })

    // "Rebound BEFORE ready" is an ordering claim, so it is sampled at the
    // instant readiness is published, not at the end of the test.
    let socketsAtReady: null | number = null
    let boundUrlAtReady: string | undefined

    const offReady = $desktopBoot.listen(state => {
      if (state.phase === 'renderer.ready' && socketsAtReady === null) {
        socketsAtReady = FakeWebSocket.instances.length
        boundUrlAtReady = FakeWebSocket.instances.at(-1)?.url
      }
    })

    const statuses = recordTerminalStatuses()
    render(<Harness refreshHermesConfig={refreshHermesConfig} />)
    await flushAsync()

    // The pre-exit state the finding depends on: one live socket on the
    // original endpoint, boot still running.
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances[0].url).toBe(primaryConn.wsUrl)
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.OPEN)
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().phase).not.toBe('renderer.ready')

    expect(backendExit).not.toBeNull()
    act(() => backendExit?.({ code: 1, signal: null }))

    await act(async () => {
      releaseConfig()
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()
    await flushAsync()
    await flushAsync()
    statuses.off()
    offReady()

    // The stale socket was dropped and a second one dialed at the REPAIRED
    // endpoint — connect() is a no-op while a socket is still open, so a
    // rebind that skipped the close would silently keep the dead binding.
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(FakeWebSocket.instances[0].readyState).toBe(FakeWebSocket.CLOSED)
    expect(FakeWebSocket.instances[1].url).toBe(respawnedConn.wsUrl)
    expect(FakeWebSocket.instances[1].readyState).toBe(FakeWebSocket.OPEN)
    expect($gatewayState.get()).toBe('open')

    // The renderer publishes the repaired descriptor, not the pre-exit one.
    expect($connection.get()?.wsUrl).toBe(respawnedConn.wsUrl)

    // The boot CONTINUED on the repaired connection instead of being declared
    // ready around it: the post-connect work ran again on the new socket.
    expect(configCalls).toBe(2)

    // The ordering itself: readiness was published only after the rebind.
    expect(socketsAtReady).toBe(2)
    expect(boundUrlAtReady).toBe(respawnedConn.wsUrl)

    // One episode, one status, and no parallel toast.
    expect(statuses.phases).toEqual(['renderer.ready'])
    expect($notifications.get()).toHaveLength(0)
    expect($desktopBoot.get().phase).toBe('renderer.ready')
    expect(screen.getByTestId('app-shell')).toBeTruthy()

    // Still exactly one repair through the EXISTING owner: no second backend,
    // no retry loop, no duplicate owner.
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
  })

  it('a child exit AFTER the boot completed still raises the persistent stopped notification', async () => {
    // Post-boot exit reporting is deliberately unchanged: once the gate has
    // published this episode's verdict, the exit is its own event again.
    render(<Harness />)
    await flushAsync()

    expect($desktopBoot.get().phase).toBe('renderer.ready')
    expect($notifications.get()).toHaveLength(0)

    act(() => backendExit?.({ code: 1, signal: null }))

    const toasts = $notifications.get()
    expect(toasts).toHaveLength(1)
    expect(toasts[0].kind).toBe('error')
    expect(toasts[0].title).toBe(translateNow('boot.errors.backendStopped'))
    expect(toasts[0].message).toBe(translateNow('boot.errors.backgroundExited'))
  })
})

describe('useGatewayBoot mid-await cancellation of owner-side work (real hook seam)', () => {
  it('unmounting while the continued boot is minting its WS URL never dials a socket the window no longer owns', async () => {
    // The named race: the primary row's ONE repair went back through the
    // existing owner, the owner handed a connection back, and THIS boot
    // continues on it. Teardown lands inside the last owner-side awaits before
    // the dial. Cleanup's gateway.close() cannot help here — there is no
    // socket yet — so a connect() that runs afterwards opens one into a
    // torn-down window.
    const failure = 'Hermes backend did not become ready: timeout'
    const desktop = fakeDesktop()
    let connectionCalls = 0
    desktop.getConnection = vi.fn(async () => {
      connectionCalls += 1

      if (connectionCalls === 1) {
        throw new Error(failure)
      }

      return primaryConn
    })
    desktop.getBootProgress = vi.fn(async () => failedBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE, failure))

    let releaseWsUrl: (url: string) => void = () => undefined
    let wsUrlParked = false
    desktop.getGatewayWsUrl = vi.fn(
      () =>
        new Promise<string>(resolve => {
          wsUrlParked = true
          releaseWsUrl = resolve
        })
    )
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    const refreshHermesConfig = vi.fn(async () => undefined)
    const view = render(<Harness refreshHermesConfig={refreshHermesConfig} />)
    await flushAsync()
    await flushAsync()

    // Parked after the owner answered, before the socket dial.
    expect(wsUrlParked).toBe(true)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
    expect(FakeWebSocket.instances).toHaveLength(0)
    const configCallsBeforeTeardown = refreshHermesConfig.mock.calls.length
    const phaseBeforeTeardown = $desktopBoot.get().phase

    await act(async () => {
      view.unmount()
    })

    await act(async () => {
      releaseWsUrl(primaryConn.wsUrl)
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    // No socket opened into the dead effect…
    expect(FakeWebSocket.instances).toHaveLength(0)
    // …no post-connect boot work ran…
    expect(refreshHermesConfig).toHaveBeenCalledTimes(configCallsBeforeTeardown)
    expect($connection.get()).toBeNull()
    // …and no verdict for a window that is gone.
    expect($desktopBoot.get().phase).toBe(phaseBeforeTeardown)
    expect($desktopBoot.get().phase).not.toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect($desktopBoot.get().phase).not.toBe('renderer.ready')
  })

  it('unmounting inside the primary owner getConnection() re-enters nothing: no mint, no dial, no re-probe, no verdict', async () => {
    // main's single-flight start cannot be recalled once issued — other
    // windows may be waiting on the same promise — so the guarantee is the
    // one that matters: whatever it returns must not re-enter this effect.
    const failure = 'Hermes backend did not become ready: timeout'
    const desktop = fakeDesktop()
    let connectionCalls = 0
    let releaseConnection: (conn: typeof primaryConn) => void = () => undefined
    let repairParked = false

    desktop.getConnection = vi.fn(async () => {
      connectionCalls += 1

      if (connectionCalls === 1) {
        throw new Error(failure)
      }

      return await new Promise<typeof primaryConn>(resolve => {
        repairParked = true
        releaseConnection = resolve
      })
    })
    desktop.getBootProgress = vi.fn(async () => failedBootProgress(LOCAL_PRIMARY_BACKEND_SERVICE, failure))
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    const view = render(<Harness />)
    await flushAsync()
    await flushAsync()

    // Parked inside the primary row's ONE owner-side repair, past revalidate.
    expect(repairParked).toBe(true)
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    const phaseBeforeTeardown = $desktopBoot.get().phase

    await act(async () => {
      view.unmount()
    })

    await act(async () => {
      releaseConnection(primaryConn)
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    expect(desktop.getGatewayWsUrl).not.toHaveBeenCalled()
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect($connection.get()).toBeNull()
    // No second revalidate, no second owner call, no re-probe, no verdict.
    expect(desktop.revalidateConnection).toHaveBeenCalledTimes(1)
    expect(desktop.getConnection).toHaveBeenCalledTimes(2)
    expect($desktopBoot.get().phase).toBe(phaseBeforeTeardown)
    expect($desktopBoot.get().phase).not.toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect($desktopBoot.get().phase).not.toBe('renderer.ready')
  })

  it('unmounting inside gateway.connect() leaves no live socket and no verdict behind', async () => {
    // Teardown landing in the socket handshake itself. Cleanup closes the
    // gateway mid-connect; when the handshake later completes, nothing may be
    // left open and nothing may be published.
    FakeWebSocket.mode = 'park'

    const view = render(<Harness />)
    await flushAsync()

    // The dial is in flight against a handshake that has not answered yet.
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect($gatewayState.get()).not.toBe('open')
    const phaseBeforeTeardown = $desktopBoot.get().phase

    await act(async () => {
      view.unmount()
    })

    await act(async () => {
      FakeWebSocket.instances[0].openNow()
      await vi.advanceTimersByTimeAsync(0)
    })
    await flushAsync()

    // Every socket this effect ever created is closed, and no second one was
    // opened behind the teardown.
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(FakeWebSocket.instances.every(socket => socket.readyState === FakeWebSocket.CLOSED)).toBe(true)
    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().phase).toBe(phaseBeforeTeardown)
    expect($desktopBoot.get().phase).not.toBe(DESKTOP_BOOT_DEGRADED_PHASE)
    expect($desktopBoot.get().phase).not.toBe('renderer.ready')
  })
})
