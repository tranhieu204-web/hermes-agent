/**
 * startup-service-gate.ts
 *
 * The aggregate startup-service acceptance for Hermes Desktop.
 *
 * Revealing the shell is not a readiness verdict: createWindow() shows the
 * window from the renderer's `ready-to-show` (or the load fallback) while
 * startHermes() is still resolving the backend, and the renderer then called
 * completeDesktopBoot() as soon as its own fetches settled. Nothing asked the
 * one question that decides whether the app is actually usable — "is every
 * service this mode REQUIRES operational right now?" — so a half-started
 * install declared itself ready and failed at the first real interaction.
 *
 * This module is that missing question, and nothing else:
 *
 *   - It owns the deterministic required/optional service matrix per mode
 *     (local vs remote), so remote startups never start local-only services
 *     and lazy per-profile backends never block a cold boot.
 *   - It runs each required row's AUTHORITATIVE probe in declared order.
 *     A probe is the service's own real check (an /api/health ladder, a live
 *     WebSocket session-token handshake, a config fetch). Process existence is
 *     deliberately not expressible here: a row is ready only when its probe
 *     says so and names the evidence in `via`.
 *   - A healthy, already-owned service is REUSED. The gate never starts,
 *     restarts, or duplicates anything — every repair goes back through the
 *     row's existing owner, exactly once, followed by exactly one re-probe.
 *   - A terminal failure produces ONE normalized degraded report (service,
 *     cause, attempted recovery, next action) instead of a popup storm, and
 *     stops the run rather than probing every remaining row.
 *   - completeDesktopBoot() is reachable ONLY through
 *     completeDesktopBootWhenReady(), which re-derives the verdict from the
 *     outcomes instead of trusting a `status` field. A caller that hands it a
 *     forged or empty verdict gets the degraded path, not a completed boot.
 *
 * Deliberately pure and dependency-free (no electron, no DOM, no node): main
 * and the renderer both consume it, and the whole acceptance is exercisable in
 * a unit test without booting Electron.
 */

export type StartupMode = 'local' | 'remote'

/** Canonical service ids. Shared so main's ledger and the renderer's rows agree. */
export const LOCAL_PRIMARY_BACKEND_SERVICE = 'local-primary-backend'
export const REMOTE_PRIMARY_BACKEND_SERVICE = 'remote-primary-backend'
export const GATEWAY_SOCKET_SERVICE = 'gateway-socket'
export const HERMES_CONFIG_SERVICE = 'hermes-config'
export const WORKSPACE_CWD_SERVICE = 'workspace-cwd'
export const SESSION_LIST_SERVICE = 'session-list'
export const PROFILE_POOL_SERVICE = 'profile-pool-backends'

/** Reported when the gate itself is missing — a bypass attempt, not a service fault. */
const GATE_SERVICE_ID = 'startup-service-gate'

export interface StartupProbeResult {
  /** True only when the service's own authoritative check succeeded. */
  ready: boolean
  /**
   * What proved it. Recorded in the outcome and required on a ledger record:
   * "this PID exists" is not evidence, and a row with no evidence is not ready.
   */
  via: string
  /** Why it is not ready. Surfaced verbatim as the degraded report's cause. */
  detail?: string
  /** The service was already owned and healthy, so nothing was started. */
  reused?: boolean
}

export interface StartupServiceRow {
  id: string
  /** Human label for the one degraded status the user sees. */
  label: string
  /** Who owns this service's lifecycle. The gate never takes ownership. */
  owner: string
  /** The modes that require this service before the app may declare itself ready. */
  requiredFor: readonly StartupMode[]
  probe: () => StartupProbeResult | Promise<StartupProbeResult>
  /**
   * One bounded repair, routed through the EXISTING owner. Omitted when the
   * service has no owner-side recovery — those fail terminally on first probe
   * rather than pretending an attempt was made.
   */
  repair?: () => unknown | Promise<unknown>
  /** What the user should do when the bounded repair could not succeed. */
  nextAction: string
}

export interface StartupServiceOutcome {
  id: string
  label: string
  owner: string
  required: boolean
  status: 'ready' | 'reused' | 'skipped' | 'failed'
  via: string
  detail: string | null
  repairAttempted: boolean
  reprobed: boolean
}

export interface StartupDegradedReport {
  serviceId: string
  service: string
  owner: string
  cause: string
  attemptedRecovery: string
  nextAction: string
}

export interface StartupGateResult {
  mode: StartupMode
  outcomes: StartupServiceOutcome[]
  /**
   * 'cancelled' is deliberately NOT a verdict: it means the caller that owned
   * this run was torn down mid-acceptance, so the run stopped where it was. A
   * cancelled run can neither complete a boot nor degrade one — there is no
   * window left to declare ready and none left to show a status to.
   */
  status: 'ready' | 'degraded' | 'cancelled'
  report: StartupDegradedReport | null
}

export interface StartupServiceGateOptions {
  mode: StartupMode
  services: readonly StartupServiceRow[]
  /**
   * The caller's cancellation epoch (a React effect's own `cancelled` flag, a
   * window teardown, …). Checked before every probe, before and after every
   * repair, and before every re-probe, so a torn-down caller can never leave
   * owner-side work — a re-dial, a revalidate, a re-probe — running behind it.
   * Absent means "never cancelled".
   */
  cancelled?: () => boolean
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function causeFrom(result: StartupProbeResult): string {
  const detail = typeof result.detail === 'string' ? result.detail.trim() : ''

  return detail || `Probe "${result.via}" did not report the service as ready.`
}

async function probeOnce(row: StartupServiceRow): Promise<StartupProbeResult> {
  try {
    const result = await row.probe()

    // A probe that answers with a non-result shape is not evidence of anything.
    if (!result || typeof result !== 'object' || typeof result.ready !== 'boolean') {
      return { ready: false, via: 'probe', detail: 'Probe returned no readiness result.' }
    }

    return result
  } catch (error) {
    return { ready: false, via: 'probe threw', detail: errorText(error) }
  }
}

/** The service ids this mode requires, in acceptance order. */
export function requiredServiceIdsForMode(services: readonly StartupServiceRow[], mode: StartupMode): string[] {
  return services.filter(row => row.requiredFor.includes(mode)).map(row => row.id)
}

/** Where a cancelled run stopped. Carries no verdict, by construction. */
function cancelledRun(mode: StartupMode, outcomes: StartupServiceOutcome[]): StartupGateResult {
  return { mode, outcomes, report: null, status: 'cancelled' }
}

/**
 * Run the aggregate acceptance for `mode`.
 *
 * Rows this mode does not require are SKIPPED without probing — that is what
 * keeps a remote startup from touching local-only services and keeps lazy
 * per-profile backends off the cold-start path. Required rows are probed in
 * declared order; the first terminal failure ends the run with one normalized
 * report.
 *
 * Every await boundary is a cancellation boundary: probes and owner repairs
 * are asynchronous, so a caller torn down mid-run must not have its owners
 * repaired, re-probed, or reported on afterwards.
 */
export async function runStartupServiceGate({
  cancelled = () => false,
  mode,
  services
}: StartupServiceGateOptions): Promise<StartupGateResult> {
  const outcomes: StartupServiceOutcome[] = []

  for (const row of services) {
    // Before this row's probe.
    if (cancelled()) {
      return cancelledRun(mode, outcomes)
    }

    const required = row.requiredFor.includes(mode)

    if (!required) {
      outcomes.push({
        detail: null,
        id: row.id,
        label: row.label,
        owner: row.owner,
        repairAttempted: false,
        reprobed: false,
        required: false,
        status: 'skipped',
        via: `not required in ${mode} mode`
      })

      continue
    }

    let result = await probeOnce(row)
    let repairAttempted = false
    let reprobed = false
    let repairFailure: string | null = null

    // After the probe, before any repair: never hand work to an owner on
    // behalf of a caller that is already gone.
    if (cancelled()) {
      return cancelledRun(mode, outcomes)
    }

    if (!result.ready && row.repair) {
      repairAttempted = true

      try {
        await row.repair()

        // After the repair, before the re-probe: the repair is itself an await
        // boundary, so teardown can land inside it.
        if (cancelled()) {
          return cancelledRun(mode, outcomes)
        }

        reprobed = true
        result = await probeOnce(row)
      } catch (error) {
        // The owner refused or failed the repair. That is terminal: a second
        // attempt is exactly the hot loop this gate exists to prevent.
        repairFailure = errorText(error)
      }
    }

    // After the re-probe, before this row is recorded or reported on.
    if (cancelled()) {
      return cancelledRun(mode, outcomes)
    }

    if (result.ready) {
      outcomes.push({
        detail: null,
        id: row.id,
        label: row.label,
        owner: row.owner,
        repairAttempted,
        reprobed,
        required: true,
        status: result.reused === true ? 'reused' : 'ready',
        via: result.via
      })

      continue
    }

    const attemptedRecovery = !row.repair
      ? 'None: no owner-side repair is available for this service.'
      : repairFailure
        ? `One bounded repair through ${row.owner} failed: ${repairFailure}`
        : `One bounded repair through ${row.owner}, then one re-probe; still not ready.`

    outcomes.push({
      detail: causeFrom(result),
      id: row.id,
      label: row.label,
      owner: row.owner,
      repairAttempted,
      reprobed,
      required: true,
      status: 'failed',
      via: result.via
    })

    return {
      mode,
      outcomes,
      report: {
        attemptedRecovery,
        cause: causeFrom(result),
        nextAction: row.nextAction,
        owner: row.owner,
        service: row.label,
        serviceId: row.id
      },
      status: 'degraded'
    }
  }

  return { mode, outcomes, report: null, status: 'ready' }
}

/**
 * Whether the app may declare itself ready.
 *
 * Re-derived from the outcomes on purpose. Trusting `status` alone would make
 * the gate bypassable by any caller that can produce an object literal, and
 * "no required rows at all" is a misconfiguration, not an acceptance.
 */
export function desktopBootMayComplete(result: StartupGateResult | null | undefined): boolean {
  if (!result || result.status !== 'ready' || result.report) {
    return false
  }

  const outcomes = Array.isArray(result.outcomes) ? result.outcomes : []
  const required = outcomes.filter(outcome => outcome && outcome.required === true)

  if (required.length === 0) {
    return false
  }

  return required.every(outcome => outcome.status === 'ready' || outcome.status === 'reused')
}

function reportFor(result: StartupGateResult | null | undefined): StartupDegradedReport {
  if (result?.report) {
    return result.report
  }

  const failed = (Array.isArray(result?.outcomes) ? result.outcomes : []).find(
    outcome => outcome && outcome.required === true && outcome.status !== 'ready' && outcome.status !== 'reused'
  )

  if (failed) {
    return {
      attemptedRecovery: failed.repairAttempted
        ? `One bounded repair through ${failed.owner}; still not ready.`
        : 'None.',
      cause: failed.detail || `Probe "${failed.via}" did not report the service as ready.`,
      nextAction: 'Reopen Hermes; if it keeps failing, run a repair from Settings.',
      owner: failed.owner,
      service: failed.label,
      serviceId: failed.id
    }
  }

  return {
    attemptedRecovery: 'None: the startup-service check did not produce a verdict.',
    cause: 'Hermes Desktop did not complete its startup-service check.',
    nextAction: 'Reopen Hermes; if it keeps failing, run a repair from Settings.',
    owner: 'desktop:startup',
    service: 'Hermes Desktop startup check',
    serviceId: GATE_SERVICE_ID
  }
}

export interface DesktopBootCompletion {
  result: StartupGateResult | null | undefined
  complete: () => void
  degrade: (report: StartupDegradedReport) => void
}

/**
 * The ONLY door to completeDesktopBoot().
 *
 * Calls `complete` exactly once when — and only when — every mode-required row
 * passed; otherwise calls `degrade` exactly once with the normalized report.
 * Never both. Callers wire completeDesktopBoot() into `complete` so the "boot
 * is finished" declaration cannot be reached around the aggregate acceptance.
 */
export function completeDesktopBootWhenReady({ complete, degrade, result }: DesktopBootCompletion): boolean {
  // A run cancelled mid-flight is not a verdict. Declaring a torn-down window
  // ready is wrong, and so is publishing a degraded status for it — the next
  // effect owns the next acceptance. Checked here as well as at the call site
  // so a caller that forgets cannot resurrect either outcome.
  if (result?.status === 'cancelled') {
    return false
  }

  if (!desktopBootMayComplete(result)) {
    degrade(reportFor(result))

    return false
  }

  complete()

  return true
}

/** The one clear degraded/blocked status the renderer shows. */
export function formatStartupDegradedMessage(report: StartupDegradedReport): string {
  return (
    `Hermes Desktop is degraded: ${report.service} is not ready. ` +
    `Cause: ${report.cause} Recovery: ${report.attemptedRecovery} Next: ${report.nextAction}`
  )
}

// ── main-process ledger ────────────────────────────────────────────────────
//
// The primary backend's authoritative probes already run in main (port
// announcement, the /api/health ladder, dashboard-token adoption, the
// WebSocket session-token handshake). Re-running them from the renderer would
// put a second owner on the same endpoints, so main RECORDS what it observed
// and the renderer's row reads the record. The ledger is the transport, not a
// second probe.

export interface StartupServiceRecord {
  id: string
  label: string
  owner: string
  status: 'ready' | 'failed'
  via: string
  detail: string | null
  at: number
}

export interface StartupServiceLedger {
  record: (entry: Omit<StartupServiceRecord, 'at'>) => void
  rows: () => StartupServiceRecord[]
  clear: () => void
}

export function createStartupServiceLedger(now: () => number = Date.now): StartupServiceLedger {
  const records = new Map<string, StartupServiceRecord>()

  return {
    clear: () => records.clear(),
    record: entry => {
      records.set(entry.id, { ...entry, at: now() })
    },
    rows: () => [...records.values()]
  }
}

function readRecord(value: unknown): StartupServiceRecord | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as Partial<StartupServiceRecord>

  // `via` is load-bearing: it is the probe evidence. A record that claims a
  // status without naming what proved it is discarded rather than trusted.
  if (typeof raw.id !== 'string' || !raw.id || typeof raw.via !== 'string' || !raw.via) {
    return null
  }

  if (raw.status !== 'ready' && raw.status !== 'failed') {
    return null
  }

  return {
    at: typeof raw.at === 'number' ? raw.at : 0,
    detail: typeof raw.detail === 'string' ? raw.detail : null,
    id: raw.id,
    label: typeof raw.label === 'string' ? raw.label : raw.id,
    owner: typeof raw.owner === 'string' ? raw.owner : 'unknown',
    status: raw.status,
    via: raw.via
  }
}

/**
 * Whether this boot-progress snapshot comes from a runtime that publishes the
 * ledger at all.
 *
 * The compatibility ladder hinges on this distinction: a main process that
 * predates the ledger reports NOTHING, which must not read the same as a
 * ledger-aware main reporting that a service is not ready. Callers fall back to
 * the pre-ledger evidence for the former and honour the verdict for the latter.
 */
export function hasStartupServiceLedger(snapshot: unknown): boolean {
  if (!snapshot || typeof snapshot !== 'object') {
    return false
  }

  return Array.isArray((snapshot as { startupServices?: unknown }).startupServices)
}

/** Read main's recorded rows off a boot-progress snapshot. Never throws. */
export function readStartupServiceRecords(snapshot: unknown): StartupServiceRecord[] {
  if (!snapshot || typeof snapshot !== 'object') {
    return []
  }

  const rows = (snapshot as { startupServices?: unknown }).startupServices

  if (!Array.isArray(rows)) {
    return []
  }

  const records: StartupServiceRecord[] = []

  for (const row of rows) {
    const record = readRecord(row)

    if (record) {
      records.push(record)
    }
  }

  return records
}

export function findStartupServiceRecord(snapshot: unknown, id: string): StartupServiceRecord | null {
  return readStartupServiceRecords(snapshot).find(record => record.id === id) ?? null
}

// ── the desktop's service matrix ───────────────────────────────────────────

export interface DesktopStartupServiceDeps {
  /** Reads main's authoritative record for the mode's primary-backend row. */
  probePrimaryBackend: (serviceId: string) => StartupProbeResult | Promise<StartupProbeResult>
  /** One revalidate through main, the primary backend's existing owner. */
  repairPrimaryBackend: () => unknown | Promise<unknown>
  probeGatewaySocket: () => StartupProbeResult | Promise<StartupProbeResult>
  /** One re-dial on the SAME gateway instance — never a second socket. */
  repairGatewaySocket: () => unknown | Promise<unknown>
  probeHermesConfig: () => StartupProbeResult | Promise<StartupProbeResult>
  repairHermesConfig: () => unknown | Promise<unknown>
}

/** Optional rows are declared so the matrix is explicit; they are never probed. */
function lazyProbe(reason: string): () => StartupProbeResult {
  return () => ({ ready: true, via: reason })
}

/**
 * The desktop's required/optional service matrix, in acceptance order.
 *
 * local  → local primary backend, gateway socket, Hermes config
 * remote → remote primary backend, gateway socket, Hermes config
 *
 * The workspace cwd, the session list, and the per-profile backend pool are
 * deliberately optional: each is lazy or already non-fatal at boot, and making
 * any of them required would brick a usable app behind a transient fetch.
 */
export interface DesktopStartupServiceOptions {
  primaryBackendRequired?: boolean
  profileBackendRequired?: boolean
}

export function createDesktopStartupServices(
  deps: DesktopStartupServiceDeps,
  { primaryBackendRequired = true, profileBackendRequired = false }: DesktopStartupServiceOptions = {}
): StartupServiceRow[] {
  return [
    {
      id: LOCAL_PRIMARY_BACKEND_SERVICE,
      label: 'Local Hermes backend',
      nextAction: 'Reopen Hermes, or run Settings → Advanced → Repair install.',
      owner: 'main:startHermes',
      probe: () => deps.probePrimaryBackend(LOCAL_PRIMARY_BACKEND_SERVICE),
      repair: () => deps.repairPrimaryBackend(),
      requiredFor: primaryBackendRequired ? ['local'] : []
    },
    {
      id: REMOTE_PRIMARY_BACKEND_SERVICE,
      label: 'Remote Hermes gateway',
      nextAction: 'Open Settings → Gateway and check the connection, then reconnect.',
      owner: 'main:startHermes',
      probe: () => deps.probePrimaryBackend(REMOTE_PRIMARY_BACKEND_SERVICE),
      repair: () => deps.repairPrimaryBackend(),
      requiredFor: primaryBackendRequired ? ['remote'] : []
    },
    {
      id: GATEWAY_SOCKET_SERVICE,
      label: 'Hermes gateway connection',
      nextAction: 'Open Settings → Gateway and reconnect.',
      owner: 'renderer:HermesGateway',
      probe: () => deps.probeGatewaySocket(),
      repair: () => deps.repairGatewaySocket(),
      requiredFor: ['local', 'remote']
    },
    {
      id: HERMES_CONFIG_SERVICE,
      label: 'Hermes settings',
      nextAction: 'Reopen Hermes; if it keeps failing, check the backend log.',
      owner: 'renderer:refreshHermesConfig',
      probe: () => deps.probeHermesConfig(),
      repair: () => deps.repairHermesConfig(),
      requiredFor: ['local', 'remote']
    },
    {
      id: WORKSPACE_CWD_SERVICE,
      label: 'Default workspace directory',
      nextAction: 'Pick a project directory in Settings → Sessions.',
      owner: 'renderer:seedDefaultCwd',
      probe: lazyProbe('non-fatal at boot; the remembered cwd is a valid fallback'),
      requiredFor: []
    },
    {
      id: SESSION_LIST_SERVICE,
      label: 'Session list',
      nextAction: 'The sidebar refills on the next refresh; no action needed.',
      owner: 'renderer:refreshSessions',
      probe: lazyProbe('non-fatal at boot; refreshed by reconnect and each turn'),
      requiredFor: []
    },
    {
      id: PROFILE_POOL_SERVICE,
      label: 'Profile backends',
      nextAction: 'None; profile backends start when a profile is first used.',
      owner: 'main:ensureBackend',
      probe: profileBackendRequired
        ? () => deps.probePrimaryBackend(PROFILE_POOL_SERVICE)
        : lazyProbe('lazy; spawned on first use of a non-primary profile'),
      repair: profileBackendRequired ? () => deps.repairPrimaryBackend() : undefined,
      requiredFor: profileBackendRequired ? ['local', 'remote'] : []
    }
  ]
}

/** The startup mode of the resolved primary connection. Unknown shapes are local. */
export function startupModeFromConnection(connection: unknown): StartupMode {
  const mode = (connection as { mode?: unknown } | null | undefined)?.mode

  return mode === 'remote' ? 'remote' : 'local'
}

/**
 * The startup mode when NO connection resolved.
 *
 * A cold start whose primary backend never came up has no descriptor to read
 * the mode off, but main recorded the primary row this boot actually required
 * — and that row names the mode. Falls back rather than inventing one when the
 * runtime publishes no ledger at all.
 */
export function startupModeFromStartupServices(snapshot: unknown, fallback: StartupMode = 'local'): StartupMode {
  const ids = readStartupServiceRecords(snapshot).map(record => record.id)

  if (ids.includes(REMOTE_PRIMARY_BACKEND_SERVICE)) {
    return 'remote'
  }

  if (ids.includes(LOCAL_PRIMARY_BACKEND_SERVICE)) {
    return 'local'
  }

  return fallback
}
