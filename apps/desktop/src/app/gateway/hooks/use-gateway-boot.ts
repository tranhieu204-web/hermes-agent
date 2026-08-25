import { isGatewayReauthRequired, resolveGatewayWsUrl } from '@hermes/shared'
import { useEffect, useRef } from 'react'

import type { DesktopBootProgress, HermesConnection } from '@/global'
import { HermesGateway } from '@/hermes'
import { translateNow } from '@/i18n'
import { desktopDefaultCwd } from '@/lib/desktop-fs'
import { reconnectBackoffDelayMs } from '@/lib/reconnect-backoff'
import {
  $desktopBoot,
  applyDesktopBootProgress,
  completeDesktopBoot,
  degradeDesktopBoot,
  failDesktopBoot,
  resumeDesktopBootForRetry,
  setDesktopBootStep
} from '@/store/boot'
import {
  $gateway,
  closeSecondaryGateways,
  configureGatewayRegistry,
  disposeSecondariesForConnection,
  ensureGatewayForProfile,
  gatewayActivationEpoch,
  isActivePrimary,
  pruneSecondaryGateways,
  reconnectSecondaryGateways,
  reportPrimaryGatewayState,
  setPrimaryGateway,
  touchSecondaryGateways
} from '@/store/gateway'
import { registerGatewayReconnect } from '@/store/gateway-reconnect'
import { $gatewaySwitching, wipeSessionListsForGatewaySwitch } from '@/store/gateway-switch'
import { notify, notifyError } from '@/store/notifications'
import {
  $activeGatewayProfile,
  normalizeProfileKey,
  refreshActiveProfile,
  touchActiveGatewayBackend
} from '@/store/profile'
import {
  $activeSessionId,
  $connection,
  $currentCwd,
  $sessions,
  ensureDefaultWorkspaceCwd,
  setConnection,
  setCurrentBranch,
  setCurrentCwd,
  setSessionsLoading
} from '@/store/session'
import {
  $attentionSessionIds,
  $workingSessionIds,
  liveSessionScopes,
  recordSessionEventScope,
  resetTileRuntimeBindings
} from '@/store/session-states'
import { windowProfileOverride } from '@/store/windows'
import type { RpcEvent } from '@/types/hermes'

import {
  completeDesktopBootWhenReady,
  createDesktopStartupServices,
  findStartupServiceRecord,
  hasStartupServiceLedger,
  PROFILE_POOL_SERVICE,
  runStartupServiceGate,
  startupModeFromConnection,
  startupModeFromStartupServices,
  type StartupProbeResult
} from '../../../../electron/startup-service-gate'

import { stashGatewaySurvivor, survivorIsStale, takeGatewaySurvivor } from './gateway-hmr-survivor'

// After the reconnect loop has been failing for this long, raise a recoverable
// boot error. Otherwise a dropped remote gateway loops the backoff forever
// behind the fullscreen CONNECTING overlay with no way to reach Settings /
// sign in / switch to local — the "lost connection breaks the app" dead end.
// The next successful reconnect clears it. Time-based (not attempt-count)
// because the full-jitter backoff makes attempt counts a meaningless clock:
// six jittered attempts can elapse in ~9s, while the old deterministic
// 1→15s ladder took ~45s to reach six failures — this threshold keeps that
// original ~45s calibration.
const RECONNECT_ESCALATE_AFTER_MS = 45_000

// Bounded self-heal for a failed REMOTE boot (#82679): when the primary boot
// fails on a transient remote fault (dropped SSH/HTTP registered connection,
// mint timeout — main tags those `retryable` on the boot progress), the
// renderer re-attempts the whole boot with the same full-jitter backoff the
// post-boot reconnect loop uses, up to this many attempts. Retries are
// bounded and end in the real recovery affordance (the boot-failure overlay
// with Retry / Settings), never an infinite spinner. Local failures and
// confirmed reauth rejections never enter this loop — a missing capability
// differs from a transient failure.
const BOOT_RETRY_MAX_ATTEMPTS = 5
// Base delay for boot retries. Deliberately slower than the socket reconnect
// loop's 300ms: each attempt may rebuild an SSH master + remote dashboard.
const BOOT_RETRY_BASE_DELAY_MS = 2_000

interface GatewayBootOptions {
  beforeConnectionSwitch: () => void
  handleGatewayEvent: (event: RpcEvent) => void
  onConnectionReady: (
    connection: Awaited<ReturnType<NonNullable<typeof window.hermesDesktop>['getConnection']>> | null
  ) => void
  onGatewayReady: (gateway: HermesGateway | null) => void
  refreshHermesConfig: () => Promise<void>
  refreshSessions: () => Promise<void>
}

export function useGatewayBoot({
  beforeConnectionSwitch,
  handleGatewayEvent,
  onConnectionReady,
  onGatewayReady,
  refreshHermesConfig,
  refreshSessions
}: GatewayBootOptions) {
  const callbacksRef = useRef({
    beforeConnectionSwitch,
    handleGatewayEvent,
    onConnectionReady,
    onGatewayReady,
    refreshHermesConfig,
    refreshSessions
  })

  callbacksRef.current = {
    beforeConnectionSwitch,
    handleGatewayEvent,
    onConnectionReady,
    onGatewayReady,
    refreshHermesConfig,
    refreshSessions
  }

  useEffect(() => {
    let cancelled = false
    const desktop = window.hermesDesktop

    const publish = (next: HermesConnection | null) => {
      callbacksRef.current.onConnectionReady(next)
      setConnection(next)
    }

    if (!desktop) {
      failDesktopBoot('Desktop IPC bridge is unavailable.')
      setSessionsLoading(false)

      return () => void (cancelled = true)
    }

    // --- Reconnect-after-sleep machinery -------------------------------------
    // macOS sleep silently drops the renderer's WebSocket. The backend Python
    // process keeps running, but nothing re-opened the socket on wake, so the
    // composer stayed disabled forever on "Starting Hermes...". Once the
    // initial boot succeeds we treat any non-open state as recoverable and
    // reconnect with backoff, and we nudge a reconnect on the OS/browser
    // signals that fire around wake (power resume, network online, the window
    // becoming visible).
    let bootCompleted = false
    let reconnecting = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectAttempt = 0
    // Wall-clock start of the current disconnect episode (first failed
    // reconnect attempt); null while healthy. Drives the time-based
    // escalation below. Reset on a clean open or a manual/wake reconnect.
    let reconnectFailingSince: number | null = null
    // Surface "sign in again" once per disconnect episode, not on every backoff
    // tick — a stale OAuth ticket fails every attempt and would otherwise stack
    // identical error toasts (and their haptics). Reset on the next clean open.
    let reauthNotified = false
    // Raised once the reconnect loop has been failing for
    // RECONNECT_ESCALATE_AFTER_MS so the recovery overlay replaces the
    // dead-end CONNECTING screen. Reset on a clean open or a manual/
    // wake-driven reconnect.
    let escalated = false
    // Bounded automatic boot retry for transient REMOTE failures (#82679).
    let bootRetryAttempt = 0
    let bootRetryTimer: ReturnType<typeof setTimeout> | null = null
    // True until THIS cold-start episode publishes its terminal status. While
    // it holds, the aggregate startup gate — not the backend-exit listener —
    // owns the verdict, so one failure episode produces one status. Bounded
    // boot retries stay inside the same episode; a completed, degraded, or
    // terminally failed boot ends it.
    let startupVerdictPending = true
    // The backend child exited during the episode above. Held as evidence for
    // the required primary-backend row instead of being published as its own
    // failure state + persistent toast, which used to land BEFORE the gate's
    // normalized degraded report for the very same failure.
    let startupExitFailure: string | null = null

    const clearBootRetryTimer = () => {
      if (bootRetryTimer !== null) {
        clearTimeout(bootRetryTimer)
        bootRetryTimer = null
      }
    }

    // Main's own record of a failed start. It carries both the transient/
    // terminal classification (`retryable`: a dropped SSH/HTTP registered
    // connection or a mint timeout is transient; local failures and confirmed
    // reauth rejections are not) and the startup-service ledger row naming the
    // primary backend this mode required. The renderer reads that record
    // rather than re-running main's probes against the same endpoints.
    const bootFailureProgress = async (): Promise<DesktopBootProgress | null> => {
      try {
        return await desktop.getBootProgress()
      } catch {
        return null
      }
    }

    // Wrap the live getter in a call so TS control-flow analysis doesn't narrow
    // `connectionState` to a constant across the early-return guards (the state
    // genuinely changes between reads).
    const gatewayOpen = () => gateway.connectionState === 'open'

    // The cancellation epoch re-read AFTER every socket dial.
    //
    // Cleanup closes this effect's gateway, but that only reaches a socket the
    // client already holds: a connect() that runs after teardown (because
    // cancellation landed in one of the owner-side awaits leading up to it)
    // opens a socket into a window that is gone, and cleanup has already run
    // and cannot close it. Every `await gateway.connect(...)` therefore ends
    // by re-reading the epoch and retiring what it may have opened. Returns
    // true when the caller must stop.
    const retireGatewayIfCancelled = () => {
      if (!cancelled) {
        return false
      }

      try {
        gateway.close()
      } catch {
        // Already closed by cleanup; retiring twice is not an error.
      }

      return true
    }

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const attemptReconnect = async () => {
      if (cancelled || reconnecting || gatewayOpen() || $gatewaySwitching.get()) {
        return
      }

      reconnecting = true

      try {
        // Drop a stale REMOTE backend cache before re-dialing. After sleep/wake a
        // remote backend can become unreachable, but it has no child process
        // whose 'exit' would clear the main process's cached descriptor — without
        // this the renderer re-dials the same dead endpoint forever and stays on
        // "Starting Hermes…". The probe is a no-op for a healthy or local backend.
        await desktop.revalidateConnection?.().catch(() => undefined)

        // Primary sleep/wake reconnect must dial the WINDOW-owned primary backend
        // (same as boot/softSwitch). Passing $activeGatewayProfile would retarget
        // this primary socket at a secondary profile's backend after a live swap.
        // Secondaries reconnect via reconnectSecondaryGateways().
        const conn = await desktop.getConnection()

        if (cancelled) {
          return
        }

        // Only publish the primary descriptor when the primary is active.
        // Otherwise a background-profile view would inherit the primary's
        // mode/baseUrl and break image.attach / fs / media routing (#46651).
        if (isActivePrimary()) {
          publish(conn)
        }

        // Re-mint the WS URL before reconnecting. OAuth tickets are single-use
        // with a short TTL, so the ticket baked into the cached conn.wsUrl is
        // dead on every reconnect after the initial boot — reusing it surfaces
        // as an opaque "Could not connect to Hermes gateway". resolveGatewayWsUrl
        // mints a fresh ticket rather than connecting with a stale one. An
        // explicit auth rejection asks for sign-in; transport failures stay in
        // this reconnect loop. For local/token gateways the URL carries a
        // long-lived token and the re-mint is a cheap no-op.
        const wsUrl = await resolveGatewayWsUrl(desktop, conn)

        if (cancelled) {
          return
        }

        await gateway.connect(wsUrl)

        if (retireGatewayIfCancelled()) {
          return
        }

        reconnectAttempt = 0
        reconnectFailingSince = null
        // A respawned backend re-mints (recycles) runtime ids, so any tile's
        // bound runtime id is now stale — drop them so each tile re-resumes.
        resetTileRuntimeBindings()
        // Resync state that may have moved on the backend while we were asleep.
        await callbacksRef.current.refreshHermesConfig().catch(() => undefined)
        await callbacksRef.current.refreshSessions().catch(() => undefined)
      } catch (err) {
        // OAuth session expired mid-reconnect: surface the actionable "sign in
        // again" message once instead of silently looping the backoff against a
        // ticket that can never succeed. Transport failures fall through to the
        // backoff in the finally block below.
        if (!cancelled && isGatewayReauthRequired(err) && !reauthNotified) {
          reauthNotified = true
          notifyError(err, translateNow('boot.errors.gatewaySignInRequired'))
        }
      } finally {
        reconnecting = false

        if (!cancelled && !gatewayOpen() && !$gatewaySwitching.get()) {
          if (reconnectFailingSince === null) {
            reconnectFailingSince = Date.now()
          }

          if (Date.now() - reconnectFailingSince >= RECONNECT_ESCALATE_AFTER_MS && !escalated) {
            escalated = true
            failDesktopBoot(translateNow('boot.errors.gatewayConnectionLost'))
          }

          scheduleReconnect()
        }
      }
    }

    function scheduleReconnect() {
      if (cancelled || reconnecting || reconnectTimer !== null || gatewayOpen() || $gatewaySwitching.get()) {
        return
      }

      // Full-jitter exponential backoff (300ms base, 15s cap) so a gateway
      // restart doesn't get redialed by every desktop client in lockstep —
      // an immediate-retry reconnect storm can exhaust the gateway's file
      // descriptors while it's still coming back up.
      const delay = reconnectBackoffDelayMs(reconnectAttempt)
      reconnectAttempt += 1
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null
        void attemptReconnect()
      }, delay)
    }

    const reconnectNow = async () => {
      if (cancelled || !bootCompleted || $gatewaySwitching.get()) {
        return
      }

      clearReconnectTimer()
      reconnectAttempt = 0
      reconnectFailingSince = null
      escalated = false
      reconnectSecondaryGateways()

      if (!gatewayOpen()) {
        await attemptReconnect()
      }
    }

    // Adopt the profile the primary (window) backend booted as, so same-profile
    // resumes are no-op swaps and reconnects target the right backend.
    // Best-effort: a missing preference means "default". Shared by boot + soft
    // switch.
    //
    // Helper windows (the HUD) can carry an explicit profile override in their
    // URL: the HUD is opened ON a conversation, and when that conversation
    // belongs to a non-primary profile, adopting the primary here resolves the
    // session id against the wrong backend — the HUD then falls back to the
    // default profile's last session (#82285). The override wins over the
    // stored preference; absent, behavior is unchanged.
    async function adoptPrimaryProfile() {
      const override = windowProfileOverride()

      try {
        const profileKey = override ?? (await desktop.profile?.get?.())?.profile ?? ''
        const key = normalizeProfileKey(profileKey)
        $activeGatewayProfile.set(key)
        setPrimaryGateway(gateway, key)
        void ensureGatewayForProfile(key)
      } catch {
        $activeGatewayProfile.set(normalizeProfileKey(override))
      }
    }

    // Seed the working dir from the backend default on a fresh view (nothing
    // open yet). Shared by boot + soft switch.
    async function seedDefaultCwd() {
      await ensureDefaultWorkspaceCwd()
      const remoteDefault = await desktopDefaultCwd().catch(() => null)

      if (remoteDefault?.cwd && !$activeSessionId.get() && !$currentCwd.get()) {
        setCurrentCwd(remoteDefault.cwd)
        setCurrentBranch(remoteDefault.branch || '')
      }
    }

    // Settings load as a startup-service probe/repair: resolves to null when
    // the config is loaded, or the failure detail when it is not. Returning
    // the failure (instead of rethrowing) is what lets the gate spend its one
    // bounded repair on it.
    async function runRefreshHermesConfig(): Promise<string | null> {
      try {
        await callbacksRef.current.refreshHermesConfig()

        return null
      } catch (err) {
        return err instanceof Error ? err.message : String(err)
      }
    }

    // Soft gateway-mode apply: main tore down the primary without reloading.
    // Wipe session lists so skeletons retrigger, then re-dial in place.
    const softSwitch = async () => {
      if (cancelled) {
        return
      }

      $gatewaySwitching.set(true)
      clearReconnectTimer()
      clearBootRetryTimer()
      bootRetryAttempt = 0
      reconnectAttempt = 0
      reconnectFailingSince = null
      escalated = false
      reauthNotified = false
      callbacksRef.current.beforeConnectionSwitch()
      wipeSessionListsForGatewaySwitch()

      try {
        gateway.close()
        closeSecondaryGateways()

        // Same override rule as boot(): a profile-pinned helper window stays
        // on its pinned profile's backend across a soft switch.
        const conn = await desktop.getConnection(windowProfileOverride() ?? undefined)

        if (cancelled) {
          return
        }

        publish(conn)
        const wsUrl = await resolveGatewayWsUrl(desktop, conn)

        if (cancelled) {
          return
        }

        await gateway.connect(wsUrl)

        if (retireGatewayIfCancelled()) {
          return
        }

        // Same shape as boot(): profile first (session scope depends on it),
        // then the independent fetches concurrently. refreshActiveProfile is
        // explicit here: the rail's $profiles still shows the PREVIOUS
        // backend's list after a connection/mode apply, and nothing else
        // re-pulls /api/profiles deterministically post-switch — leaving the
        // rail stale or (if a stale in-flight response landed) collapsed
        // (#85731). Best-effort like the rest: a failure keeps the cached
        // list rather than blanking the rail.
        await adoptPrimaryProfile()
        await Promise.all([
          seedDefaultCwd(),
          refreshActiveProfile().catch(() => undefined),
          callbacksRef.current.refreshHermesConfig().catch(() => undefined),
          callbacksRef.current.refreshSessions().catch(() => undefined)
        ])
        completeDesktopBoot()
        bootCompleted = true
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err)
          failDesktopBoot(message)
          notifyError(err, translateNow('boot.errors.desktopBootFailed'))
          setSessionsLoading(false)
        }
      } finally {
        $gatewaySwitching.set(false)
      }
    }

    const offBootProgress = desktop.onBootProgress(payload => {
      // Soft switch / post-boot startHermes re-emits progress — ignore so the
      // cold-boot CONNECTING overlay stays down. Errors still surface.
      if ($gatewaySwitching.get() || bootCompleted) {
        if (payload.error) {
          applyDesktopBootProgress(payload)
        }

        return
      }

      applyDesktopBootProgress(payload)
    })

    void desktop
      .getBootProgress()
      .then(snapshot => applyDesktopBootProgress(snapshot))
      .catch(() => undefined)

    setDesktopBootStep({
      phase: 'renderer.boot',
      message: translateNow('boot.steps.startingDesktopConnection'),
      progress: 6
    })

    // HMR adoption: in a dev hot update, the previous effect instance parked its
    // still-open socket instead of closing it (see the cleanup below). Re-adopt
    // it so an edit doesn't drop the live agent session. A stale (closed) parked
    // socket is discarded and we boot fresh. No-op in production: import.meta.hot
    // is undefined there, so this folds to `null` and the whole survivor module
    // dead-code-eliminates out of the bundle.
    const survivor = import.meta.hot ? takeGatewaySurvivor() : null
    const adoptedFromHmr = Boolean(survivor && !survivorIsStale(survivor))

    if (survivor && !adoptedFromHmr) {
      // Parked socket died between edits (e.g. backend restart) — release it.
      try {
        survivor.gateway.close()
      } catch {
        // ignore
      }
    }

    const gateway = adoptedFromHmr ? survivor!.gateway : new HermesGateway()

    callbacksRef.current.onGatewayReady(gateway)
    setPrimaryGateway(gateway, survivor?.profile ?? normalizeProfileKey($activeGatewayProfile.get()))
    // Secondary (background-profile) sockets funnel into the same handler.
    // Record each event's source scope first: registry-tagged events feed the
    // (connectionId, profile) keep-set so two sources exposing the same
    // profile name (every source has a 'default') can't collide.
    configureGatewayRegistry({
      onActiveConnectionChanged: publish,
      // Keep $activeGatewayProfile in lockstep with the registry's OWN record
      // of which profile the active socket serves. The registry is the only
      // party that sees eviction fallbacks (idle reap, connection removal,
      // profile delete → primary); before this mirror those fallbacks moved
      // the SOCKET back to the primary while the profile atom kept naming the
      // evicted bot. ensureGatewayProfile's "already active" fast path then
      // trusted the stale atom and skipped the re-swap, so every
      // session-scoped RPC for that bot went out on the primary socket — the
      // #89206 "Waking up… → retries gave up" wake failure, while the bot's
      // own backend sat healthy and idle.
      onActiveRouteChanged: profile => {
        const key = normalizeProfileKey(profile)

        if (normalizeProfileKey($activeGatewayProfile.get()) !== key) {
          $activeGatewayProfile.set(key)
        }
      },
      onEvent: event => {
        recordSessionEventScope(event)
        callbacksRef.current.handleGatewayEvent(event)
      },
      onActiveConnectionInvalidated: (fallbackProfile, invalidationEpoch) => {
        $activeGatewayProfile.set(fallbackProfile)
        void desktop
          .getConnection(fallbackProfile)
          .then(connection => {
            if (!cancelled && gatewayActivationEpoch() === invalidationEpoch) {
              publish(connection)
            }
          })
          .catch(() => {
            if (!cancelled && gatewayActivationEpoch() === invalidationEpoch) {
              publish(null)
            }
          })
      }
    })

    const offState = gateway.onState(st => {
      // Mirror to the composer only while the primary is the active profile —
      // a background secondary reconnect mustn't flip the foreground state.
      reportPrimaryGatewayState(st)

      if (st === 'open') {
        reconnectAttempt = 0
        reconnectFailingSince = null
        reauthNotified = false
        escalated = false
        clearReconnectTimer()

        // A revalidate-driven reconnect can rebuild the backend in place when the
        // cached remote was found dead, which re-drives the boot-progress overlay.
        // Unlike the initial boot, nothing calls completeDesktopBoot() afterwards,
        // so dismiss it here once we're open again — otherwise the overlay sticks
        // at ~94%. A no-op on a normal (non-rebuild) reconnect.
        if (bootCompleted) {
          completeDesktopBoot()
        }
      } else if (bootCompleted && !$gatewaySwitching.get() && (st === 'closed' || st === 'error')) {
        // The socket dropped after a healthy boot (typically sleep/wake). Try
        // to bring it back instead of leaving the composer stuck disabled.
        scheduleReconnect()
      }
    })

    const sourceProfile = normalizeProfileKey($activeGatewayProfile.get())

    const offEvent = gateway.onEvent(event =>
      callbacksRef.current.handleGatewayEvent({ ...event, profile: sourceProfile })
    )

    // Wake signals: power resume (macOS/Windows), network coming back, and the
    // window regaining focus/visibility. Each nudges an immediate reconnect.
    const offPowerResume = desktop.onPowerResume?.(() => void reconnectNow())
    const offConnectionApplied = desktop.onConnectionApplied?.(() => void softSwitch())
    const offGatewayReconnect = registerGatewayReconnect(reconnectNow)

    // Registry lifecycle: a removed connection's secondaries must close NOW
    // (remote/cloud have no local process whose death would drop the socket —
    // they'd keep streaming ghost events); a materially edited one is
    // disposed AND re-dialed so its sockets target the new endpoint.
    const offConnectionsChanged = desktop.connections?.onChanged?.(payload => {
      if (!payload || typeof payload.connectionId !== 'string') {
        return
      }

      disposeSecondariesForConnection(payload.connectionId, { redial: payload.reason === 'updated' })
    })

    const onOnline = () => void reconnectNow()

    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void reconnectNow()
      }
    }

    window.addEventListener('online', onOnline)
    document.addEventListener('visibilitychange', onVisible)

    // Keep live pool backends alive while this window is open (the main process
    // can't observe the direct renderer↔backend WS). No-op for the primary.
    const keepaliveTimer = setInterval(() => {
      touchActiveGatewayBackend()
      touchSecondaryGateways()
    }, 60_000)

    // Bound concurrency cost to live work: keep a background socket only while
    // its profile has a running (working) or blocked (needs-input) session.
    // Once that profile goes idle its socket is dropped and its backend is free
    // to idle-reap. The active profile is always spared.
    const recomputeKeptGateways = () => {
      const live = new Set([...$workingSessionIds.get(), ...$attentionSessionIds.get()])
      // Registry-scoped (connectionId, profile) scopes with live work. Two
      // sources can expose the same profile name (every source has a
      // 'default'), so bare profile names can't represent a non-local
      // source's liveness without keeping the wrong gateway alive.
      const keep = liveSessionScopes()

      for (const session of $sessions.get()) {
        if (live.has(session.id)) {
          keep.add(normalizeProfileKey(session.profile))
        }
      }

      pruneSecondaryGateways(keep)
    }

    const offWorking = $workingSessionIds.subscribe(() => recomputeKeptGateways())
    const offAttention = $attentionSessionIds.subscribe(() => recomputeKeptGateways())
    const offActiveProfile = $activeGatewayProfile.subscribe(() => recomputeKeptGateways())

    const offWindowState = desktop.onWindowStateChanged?.(payload => {
      const current = $connection.get()

      if (current) {
        publish({ ...current, ...payload })
      }
    })

    const offExit = desktop.onBackendExit(() => {
      if ($gatewaySwitching.get()) {
        return
      }

      // Startup: the aggregate gate owns this episode's status. A child that
      // dies mid-cold-start is the required primary-backend row's evidence,
      // not a verdict of its own — publishing failDesktopBoot() plus a
      // persistent toast here raced the gate and put a second, differently
      // shaped status in front of the normalized degraded report for one and
      // the same failure. Recorded instead, and judged on the row that owns
      // the backend (probePrimaryBackend below).
      if (startupVerdictPending) {
        startupExitFailure = translateNow('boot.errors.backgroundExitedDuringStartup')

        return
      }

      // Post-boot exit reporting is deliberately unchanged: once the gate has
      // published this episode's verdict, an exit is its own event again.
      if ($desktopBoot.get().running || $desktopBoot.get().visible) {
        failDesktopBoot(translateNow('boot.errors.backgroundExitedDuringStartup'))
      }

      notify({
        kind: 'error',
        title: translateNow('boot.errors.backendStopped'),
        message: translateNow('boot.errors.backgroundExited'),
        durationMs: 0
      })
    })

    async function boot() {
      // Exit evidence belongs to one boot attempt. A later bounded retry must
      // not inherit a previous child's death.
      startupExitFailure = null

      // Settings load is a REQUIRED startup service, so its failure is captured
      // rather than thrown: the gate below owns the verdict, and gets to spend
      // one bounded repair on it before declaring the app degraded. (Throwing
      // would skip the repair entirely.)
      let hermesConfigFailure: string | null = null
      // The required PRIMARY backend's own start failure, for the boots where
      // main could not hand the renderer a connection at all. Captured for the
      // same reason: that failure belongs in the aggregate gate, not in a
      // parallel failure path with its own recovery rules.
      let primaryStartFailure: string | null = null
      // Whether this boot is standing on a live primary connection + socket.
      let connected = false

      // The primary-backend row's ready verdicts, filtered through the exit
      // evidence the backend-exit listener recorded instead of publishing.
      //
      // This is where a startup-time child exit is JUDGED, and judging it on
      // the row that owns the backend is what makes one failure episode
      // produce one status. It has to override a ready verdict specifically:
      // main's ledger row (and the connection this boot may already stand on)
      // describe probes that really did pass — before the process died. A
      // row that already failed keeps its own, more specific cause.
      const primaryReady = (result: StartupProbeResult): StartupProbeResult =>
        startupExitFailure ? { detail: startupExitFailure, ready: false, via: 'desktop.onBackendExit' } : result

      // Everything the cold start does once the primary connection resolves.
      // Extracted so the gate's ONE bounded primary-backend repair can continue
      // THIS boot when the existing owner hands a connection back — a completed
      // boot must never mean "the backend came up but nothing else ran".
      const connectPrimary = async (conn: HermesConnection) => {
        setDesktopBootStep({
          phase: 'renderer.gateway.connect',
          message: translateNow('boot.steps.connectingGateway'),
          progress: 95
        })
        publish(conn)

        // Seed the workspace BEFORE the gateway opens: every session-restore
        // path is gated on gatewayState === 'open', so nothing can be active yet
        // and ensureDefaultWorkspaceCwd's live-session guard passes. The
        // post-connect seed could lose that race on a slow start (#71873). A
        // resumed session's own cwd still supersedes this once its runtime
        // arrives. Non-fatal: the remembered cwd is a fine fallback and the
        // post-connect pass retries the sync.
        try {
          await ensureDefaultWorkspaceCwd()
        } catch (err) {
          console.warn('Failed to seed default workspace cwd pre-connect', err)
        }

        // Each remaining step is an owner-side await, and this closure is also
        // the gate's ONE primary-backend repair continuing a boot: teardown can
        // land in any of them. Re-read the epoch between every pair so a dead
        // effect stops here instead of dialing a socket and running the whole
        // post-connect boot behind a window that is already gone.
        if (cancelled) {
          return
        }

        // Mint a fresh WS URL right before connecting. For OAuth gateways the
        // ticket is single-use with a short TTL, so the ticket baked into
        // conn.wsUrl is stale; resolveGatewayWsUrl() re-mints it rather than
        // connecting with a dead ticket. Auth rejection asks for sign-in;
        // connectivity failures remain retryable.
        const wsUrl = await resolveGatewayWsUrl(desktop, conn)

        if (cancelled) {
          return
        }

        await gateway.connect(wsUrl)
        connected = true

        if (retireGatewayIfCancelled()) {
          return
        }

        // Profile adoption must land first: refreshSessions scopes its fetch by
        // $profileScope ← $activeGatewayProfile. The remaining three fetches
        // (cwd seed, config, sessions) are independent REST calls — running
        // them serially added their sum to time-to-populated-sidebar when only
        // the max is needed.
        await adoptPrimaryProfile()

        if (cancelled) {
          return
        }

        setDesktopBootStep({
          phase: 'renderer.config',
          message: translateNow('boot.steps.loadingSettings'),
          progress: 97
        })

        await Promise.all([
          // The pre-connect seed already applied the configured default; this
          // post-connect pass covers the remote backend default. Non-fatal: a
          // failed sync must not abort boot (the remembered cwd remains).
          seedDefaultCwd().catch(err => console.warn('Failed to sync default workspace cwd post-connect', err)),
          runRefreshHermesConfig().then(failure => {
            hermesConfigFailure = failure
          }),
          // Session-list population is never boot-fatal. The gateway WS is
          // already open by this point — a failed sidebar fetch (transient
          // blip, or an endpoint the fallback couldn't cover) must leave the
          // app usable with an empty sidebar (the reconnect/turn refreshes
          // retry it), not brick boot behind the "Hermes couldn't start"
          // overlay. Matches the reconnect + softSwitch call sites.
          callbacksRef.current.refreshSessions().catch(() => {
            setSessionsLoading(false)
          })
        ])
      }

      try {
        // A profile-pinned helper window (the HUD) dials its target profile's
        // backend directly — ensureBackend spawns/reuses it from the pool.
        // Everything else keeps dialing the primary.
        let conn: HermesConnection | null = null
        const profileOverride = windowProfileOverride()

        try {
          conn = await desktop.getConnection(profileOverride ?? undefined)
        } catch (err) {
          // A required primary backend that never came up is a startup-SERVICE
          // failure, and the gate below is where required services are judged.
          // Capturing it here (instead of throwing straight to the terminal
          // fail/notify path) is what gives the most important required row the
          // same treatment as every other one: one repair through its existing
          // owner, one re-probe, and one normalized degraded status.
          primaryStartFailure = err instanceof Error ? err.message : String(err)
        }

        if (cancelled) {
          return
        }

        // The failed attempt's boot-progress snapshot: main's retryable
        // classification plus the ledger row naming the primary this mode
        // required (there is no connection left to read the mode off).
        let failureProgress: DesktopBootProgress | null = null

        if (conn) {
          await connectPrimary(conn)

          if (cancelled) {
            return
          }
        } else {
          failureProgress = await bootFailureProgress()

          if (cancelled) {
            return
          }

          // Transient remote failure (dropped SSH/HTTP registered connection,
          // mint timeout): self-heal with bounded, jittered retries instead of
          // parking on "Desktop boot failed" until the user re-enters the same
          // connection details (#82679). Main already cleared the failed cached
          // descriptor, so the next getConnection() rebuilds the connection —
          // exactly what manual re-entry forced. Exhausted retries, local
          // failures, and confirmed reauth rejections fall through to the
          // aggregate gate, never to an infinite spinner.
          if (bootRetryAttempt < BOOT_RETRY_MAX_ATTEMPTS && failureProgress?.retryable === true) {
            const delay = reconnectBackoffDelayMs(bootRetryAttempt, { baseDelayMs: BOOT_RETRY_BASE_DELAY_MS })
            bootRetryAttempt += 1
            resumeDesktopBootForRetry(translateNow('boot.steps.retryingRemoteBackend'))
            clearBootRetryTimer()
            bootRetryTimer = setTimeout(() => {
              bootRetryTimer = null
              void boot()
            }, delay)

            return
          }

          // Nothing downstream of the connection ran, so the settings row is
          // not ready either. The gate stops at the first terminal failure —
          // the primary row — so this only decides what the run would see if
          // the owner's one repair brought the backend back.
          hermesConfigFailure = 'Hermes settings were not loaded: the primary Hermes backend did not start.'
        }

        // The aggregate startup-service acceptance. Window reveal and a
        // settled fetch list are not readiness verdicts: every service THIS
        // mode requires has to answer its own authoritative probe before the
        // app may call itself ready. Healthy owned services are reused;
        // recoverable ones get exactly one repair through their existing
        // owner and one re-probe.
        const gateResult = await runStartupServiceGate({
          // This effect's own epoch. A run whose window was torn down stops
          // where it is: no owner repair, no re-probe, no verdict.
          cancelled: () => cancelled,
          mode: conn ? startupModeFromConnection(conn) : startupModeFromStartupServices(failureProgress),
          services: createDesktopStartupServices(
            {
              probeGatewaySocket: () =>
                gatewayOpen()
                  ? { ready: true, reused: true, via: 'gateway.connectionState === "open"' }
                  : {
                      detail: `The gateway socket is ${gateway.connectionState}.`,
                      ready: false,
                      via: 'gateway.connectionState'
                    },
              probeHermesConfig: () =>
                hermesConfigFailure
                  ? { detail: hermesConfigFailure, ready: false, via: 'refreshHermesConfig' }
                  : { ready: true, via: 'refreshHermesConfig' },
              // Main already ran this backend's authoritative probes (port
              // announcement, /api/health ladder, dashboard-token adoption, the
              // /api/ws session-token handshake). Read its record instead of
              // opening a second owner on the same endpoints.
              probePrimaryBackend: async serviceId => {
                // A profile-pinned helper window uses a pool backend owned by
                // main:ensureBackend, not main's primary ledger row.
                if (serviceId === PROFILE_POOL_SERVICE) {
                  return connected
                    ? primaryReady({
                        ready: true,
                        reused: true,
                        via: `resolved profile backend (${profileOverride})`
                      })
                    : {
                        detail:
                          primaryStartFailure ?? `The ${profileOverride ?? 'requested'} profile backend did not start.`,
                        ready: false,
                        via: 'main:ensureBackend'
                      }
                }

                const snapshot = await desktop.getBootProgress().catch(() => null)

                // Compatibility ladder, tied to an identified older runtime: a
                // main process that predates the startup-service ledger publishes
                // no rows at all. Its authoritative probes still ran — they are
                // what produced the connection this boot is standing on — so
                // "this build cannot report it" must not read as "it failed".
                // That reading only holds while there IS a connection: on a boot
                // whose primary start failed there is no silence to interpret,
                // because the owner's own failure is the evidence.
                if (!hasStartupServiceLedger(snapshot)) {
                  return connected
                    ? primaryReady({
                        ready: true,
                        reused: true,
                        via: 'main boot-progress (runtime predates the ledger)'
                      })
                    : {
                        detail: primaryStartFailure ?? 'The primary Hermes backend did not start.',
                        ready: false,
                        via: 'main:startHermes (runtime predates the ledger)'
                      }
                }

                const record = findStartupServiceRecord(snapshot, serviceId)

                if (!record) {
                  return connected
                    ? primaryReady({
                        ready: true,
                        reused: true,
                        via: 'resolved primary connection (main:startHermes)'
                      })
                    : {
                        detail: primaryStartFailure ?? 'The main process has not reported this backend as started.',
                        ready: false,
                        via: 'main startup-service ledger'
                      }
                }

                return record.status === 'ready'
                  ? primaryReady({ ready: true, reused: true, via: record.via })
                  : {
                      detail: record.detail || 'The primary Hermes backend did not become ready.',
                      ready: false,
                      via: record.via
                    }
              },
              // Each repair re-checks THIS effect's cancellation epoch at every
              // await boundary. Cleanup closes the socket and marks the epoch
              // dead; a repair that resumed afterwards would re-dial a gateway
              // the window no longer owns, or publish state into a torn-down view.
              repairGatewaySocket: async () => {
                if (cancelled) {
                  return
                }

                const repaired = await desktop.getConnection(profileOverride ?? undefined)

                if (cancelled) {
                  return
                }

                // Rebind through the same continuation so the published
                // descriptor, active profile, config, sessions, and socket all
                // describe the same repaired backend.
                if (connected) {
                  connected = false
                  gateway.close()
                }

                await connectPrimary(repaired)
              },
              repairHermesConfig: async () => {
                if (cancelled) {
                  return
                }

                const failure = await runRefreshHermesConfig()

                if (cancelled) {
                  return
                }

                hermesConfigFailure = failure
              },
              // Through the EXISTING owner only: revalidate drops a descriptor
              // main found dead, and getConnection joins main's single-flight
              // start. Neither spawns a second backend.
              //
              // Cancellation classification: main's single-flight start is
              // SHARED (other windows may be awaiting the same promise) and has
              // no abort token, so an owner call already issued cannot be
              // recalled — it runs to completion on the owner's side. What the
              // epoch checks below guarantee is the part that belongs to this
              // effect: nothing the owner returns re-enters a torn-down window.
              // No second revalidate, no publish, no dial, no re-probe, no
              // verdict — the gate's own post-repair check then stops the run.
              repairPrimaryBackend: async () => {
                if (cancelled) {
                  return
                }

                if (!profileOverride) {
                  await desktop.revalidateConnection?.()
                }

                if (cancelled) {
                  return
                }

                const repaired = await desktop.getConnection(profileOverride ?? undefined)

                if (cancelled) {
                  return
                }

                // Was this repair triggered by a child that died mid-startup?
                // Read before the evidence is spent below: it decides whether
                // `connected` still describes anything real.
                const exitedDuringStartup = startupExitFailure !== null

                // The owner answered for the backend that had exited, so the
                // exit evidence is spent: the ONE re-probe reads main's current
                // record instead of the exit that triggered this repair.
                startupExitFailure = null

                // `connected` records the dial this boot ALREADY made — at a
                // process that has since exited. Reusing it here would hand the
                // re-probe a fresh ready ledger while the renderer still holds
                // the dead endpoint's socket, and the gate would declare that
                // ready. A startup exit therefore invalidates the binding, not
                // just the row.
                if (connected && !exitedDuringStartup) {
                  return
                }

                if (connected) {
                  connected = false
                  // Drop the pre-exit socket BEFORE re-dialing. A child's death
                  // and its socket's close event are separate events, and
                  // connect() is a no-op while a socket still reads OPEN — so a
                  // rebind that skipped this would silently keep the renderer on
                  // the dead endpoint. Startup-only: the reconnect loop is gated
                  // on bootCompleted, so nothing re-dials behind this close.
                  gateway.close()
                }

                // The owner brought the backend back — for a boot that never got
                // a connection, or one whose connection died under it: continue
                // THIS boot on the repaired one (publish, open the socket, adopt
                // the profile, load settings) rather than leaving a half-started
                // app to be declared ready by the rows below.
                await connectPrimary(repaired)
              }
            },
            {
              primaryBackendRequired: !profileOverride,
              profileBackendRequired: Boolean(profileOverride)
            }
          )
        })

        if (cancelled) {
          return
        }

        // completeDesktopBoot() is reachable ONLY through this call. Either
        // branch is this episode's ONE terminal status, so both end the
        // window in which the backend-exit listener defers to this gate.
        completeDesktopBootWhenReady({
          complete: () => {
            completeDesktopBoot()
            bootCompleted = true
            bootRetryAttempt = 0
            startupVerdictPending = false
          },
          degrade: report => {
            degradeDesktopBoot(report)
            setSessionsLoading(false)
            startupVerdictPending = false
          },
          result: gateResult
        })
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err)

          // Anything that threw AFTER the primary connection resolved (WS
          // ticket mint, the gateway dial, profile adoption). A transient
          // remote fault self-heals with the same bounded, jittered retries as
          // a failed primary start (#82679); everything else ends in the real
          // recovery affordance (the boot-failure overlay), never a spinner.
          if (
            bootRetryAttempt < BOOT_RETRY_MAX_ATTEMPTS &&
            (await bootFailureProgress())?.retryable === true &&
            !cancelled
          ) {
            const delay = reconnectBackoffDelayMs(bootRetryAttempt, { baseDelayMs: BOOT_RETRY_BASE_DELAY_MS })
            bootRetryAttempt += 1
            resumeDesktopBootForRetry(translateNow('boot.steps.retryingRemoteBackend'))
            clearBootRetryTimer()
            bootRetryTimer = setTimeout(() => {
              bootRetryTimer = null
              void boot()
            }, delay)

            return
          }

          failDesktopBoot(message)
          notifyError(err, translateNow('boot.errors.desktopBootFailed'))
          setSessionsLoading(false)
          // Terminal for this episode too: this IS the one status, so a later
          // exit is reported by the listener again rather than swallowed.
          startupVerdictPending = false
        }
      }
    }

    // Adopt the parked socket without re-running the full boot handshake: the
    // socket is already open, the backend session is untouched, and we already
    // know the profile. We only re-publish the connection, re-sync config +
    // sessions (cheap, and the backend may have moved on between edits), and
    // dismiss any boot overlay. This is what keeps a live, mid-stream session
    // intact across an HMR update.
    async function adoptBoot() {
      bootCompleted = true
      startupVerdictPending = false
      completeDesktopBoot()

      if (survivor?.connection) {
        publish(survivor.connection)
      }

      const profile = survivor?.profile ?? $activeGatewayProfile.get()
      $activeGatewayProfile.set(profile)
      void ensureGatewayForProfile(profile)

      // Mirror the current (already-open) socket state into the composer so the
      // input doesn't sit disabled after the swap.
      reportPrimaryGatewayState(gateway.connectionState)

      await callbacksRef.current.refreshHermesConfig().catch(() => undefined)

      if (cancelled) {
        return
      }

      await callbacksRef.current.refreshSessions().catch(() => undefined)
    }

    if (adoptedFromHmr) {
      void adoptBoot()
    } else {
      void boot()
    }

    return () => {
      cancelled = true
      $gatewaySwitching.set(false)
      clearReconnectTimer()
      clearBootRetryTimer()
      clearInterval(keepaliveTimer)
      offWorking()
      offAttention()
      offActiveProfile()
      window.removeEventListener('online', onOnline)
      document.removeEventListener('visibilitychange', onVisible)
      offPowerResume?.()
      offConnectionApplied?.()
      offConnectionsChanged?.()
      offGatewayReconnect()
      offState()
      offEvent()
      offExit()
      offWindowState?.()
      offBootProgress()

      // HMR teardown vs. real unmount. On a hot update we must NOT close the
      // socket — that's the whole bug. Detach this instance's listeners (their
      // closures capture the disposed module), park the still-open gateway, and
      // let the freshly loaded effect re-adopt it. Secondaries are owned by the
      // gateway store (HMR-stable module state), so they survive untouched.
      // Production: import.meta.hot is undefined, so this branch never runs and
      // the original destructive teardown below is byte-for-byte preserved.
      if (import.meta.hot && gateway.connectionState === 'open') {
        stashGatewaySurvivor({
          gateway,
          profile: survivor?.profile ?? $activeGatewayProfile.get(),
          connection: $connection.get()
        })

        return
      }

      closeSecondaryGateways()
      gateway.close()
      publish(null)
      callbacksRef.current.onGatewayReady(null)
      setPrimaryGateway(null)
      $gateway.set(null)
    }
  }, [])
}
