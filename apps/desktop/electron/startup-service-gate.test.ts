import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import {
  completeDesktopBootWhenReady,
  createDesktopStartupServices,
  createStartupServiceLedger,
  desktopBootMayComplete,
  findStartupServiceRecord,
  formatStartupDegradedMessage,
  GATEWAY_SOCKET_SERVICE,
  hasStartupServiceLedger,
  HERMES_CONFIG_SERVICE,
  LOCAL_PRIMARY_BACKEND_SERVICE,
  PROFILE_POOL_SERVICE,
  readStartupServiceRecords,
  REMOTE_PRIMARY_BACKEND_SERVICE,
  requiredServiceIdsForMode,
  runStartupServiceGate,
  SESSION_LIST_SERVICE,
  startupModeFromConnection,
  startupModeFromStartupServices,
  type StartupProbeResult,
  type StartupServiceRow,
  WORKSPACE_CWD_SERVICE
} from './startup-service-gate'

function serviceDeps(overrides: Record<string, unknown> = {}) {
  return {
    probeGatewaySocket: vi.fn(async () => ({ ready: true, via: 'gateway.connectionState=open' })),
    probeHermesConfig: vi.fn(async () => ({ ready: true, via: 'refreshHermesConfig' })),
    probePrimaryBackend: vi.fn(async (serviceId: string) => ({ ready: true, via: `main:${serviceId}` })),
    repairGatewaySocket: vi.fn(async () => {}),
    repairHermesConfig: vi.fn(async () => {}),
    repairPrimaryBackend: vi.fn(async () => {}),
    ...overrides
  }
}

function row(
  id: string,
  probe: () => StartupProbeResult | Promise<StartupProbeResult>,
  overrides: Partial<StartupServiceRow> = {}
): StartupServiceRow {
  return {
    id,
    label: id,
    nextAction: `Restart ${id}`,
    owner: `owner:${id}`,
    probe,
    requiredFor: ['local', 'remote'],
    ...overrides
  }
}

// ── mode-scoped required-service matrix ────────────────────────────────────

test('the required-service set differs by mode and never requires the other mode local/remote row', () => {
  const services = createDesktopStartupServices(serviceDeps())

  assert.deepEqual(requiredServiceIdsForMode(services, 'local'), [
    LOCAL_PRIMARY_BACKEND_SERVICE,
    GATEWAY_SOCKET_SERVICE,
    HERMES_CONFIG_SERVICE
  ])
  assert.deepEqual(requiredServiceIdsForMode(services, 'remote'), [
    REMOTE_PRIMARY_BACKEND_SERVICE,
    GATEWAY_SOCKET_SERVICE,
    HERMES_CONFIG_SERVICE
  ])
})

test('optional and lazy profile-pool services are required in neither mode', () => {
  const services = createDesktopStartupServices(serviceDeps())

  for (const id of [WORKSPACE_CWD_SERVICE, SESSION_LIST_SERVICE, PROFILE_POOL_SERVICE]) {
    assert.equal(requiredServiceIdsForMode(services, 'local').includes(id), false)
    assert.equal(requiredServiceIdsForMode(services, 'remote').includes(id), false)
  }
})

test('a profile-pinned window requires its pool backend instead of the primary row', () => {
  const services = createDesktopStartupServices(serviceDeps(), {
    primaryBackendRequired: false,
    profileBackendRequired: true
  })

  for (const mode of ['local', 'remote'] as const) {
    assert.deepEqual(requiredServiceIdsForMode(services, mode), [
      GATEWAY_SOCKET_SERVICE,
      HERMES_CONFIG_SERVICE,
      PROFILE_POOL_SERVICE
    ])
  }
})

test('remote mode never probes the local-only primary backend row', async () => {
  const deps = serviceDeps()
  const result = await runStartupServiceGate({ mode: 'remote', services: createDesktopStartupServices(deps) })

  assert.equal(result.status, 'ready')
  assert.deepEqual(
    deps.probePrimaryBackend.mock.calls.map(call => call[0]),
    [REMOTE_PRIMARY_BACKEND_SERVICE]
  )

  const local = result.outcomes.find(outcome => outcome.id === LOCAL_PRIMARY_BACKEND_SERVICE)
  assert.equal(local?.status, 'skipped')
  assert.equal(local?.required, false)
})

test('local mode never probes the remote-only primary backend row', async () => {
  const deps = serviceDeps()
  const result = await runStartupServiceGate({ mode: 'local', services: createDesktopStartupServices(deps) })

  assert.equal(result.status, 'ready')
  assert.deepEqual(
    deps.probePrimaryBackend.mock.calls.map(call => call[0]),
    [LOCAL_PRIMARY_BACKEND_SERVICE]
  )
})

test('an optional row is skipped without ever running its probe', async () => {
  const optional = vi.fn(() => {
    throw new Error('optional service must not be probed at cold startup')
  })

  const result = await runStartupServiceGate({
    mode: 'local',
    services: [row('required', async () => ({ ready: true, via: 'probe' })), row('lazy', optional, { requiredFor: [] })]
  })

  assert.equal(result.status, 'ready')
  assert.equal(optional.mock.calls.length, 0)
})

// ── ordering ───────────────────────────────────────────────────────────────

test('required rows are probed in declared acceptance order', async () => {
  const calls: string[] = []

  const services = ['a', 'b', 'c'].map(id =>
    row(id, async () => {
      calls.push(id)

      return { ready: true, via: 'probe' }
    })
  )

  await runStartupServiceGate({ mode: 'local', services })
  assert.deepEqual(calls, ['a', 'b', 'c'])
})

test('a terminal required failure stops the run instead of probing every later row', async () => {
  const later = vi.fn(async () => ({ ready: true, via: 'probe' }))

  const result = await runStartupServiceGate({
    mode: 'local',
    services: [row('broken', async () => ({ ready: false, via: 'probe', detail: 'refused' })), row('later', later)]
  })

  assert.equal(result.status, 'degraded')
  assert.equal(later.mock.calls.length, 0)
})

// ── reuse, bounded repair, no duplicate repair ─────────────────────────────

test('a healthy already-owned service is reused and never repaired', async () => {
  const repair = vi.fn(async () => {})

  const result = await runStartupServiceGate({
    mode: 'local',
    services: [row('owned', async () => ({ ready: true, reused: true, via: 'existing owner' }), { repair })]
  })

  assert.equal(result.status, 'ready')
  assert.equal(repair.mock.calls.length, 0)
  assert.equal(result.outcomes[0].status, 'reused')
  assert.equal(result.outcomes[0].repairAttempted, false)
})

test('a recoverable failure gets exactly one repair and exactly one re-probe', async () => {
  const repair = vi.fn(async () => {})
  let probes = 0

  const probe = vi.fn(async () => {
    probes += 1

    return probes === 1 ? { ready: false, via: 'probe', detail: 'not up yet' } : { ready: true, via: 'probe' }
  })

  const result = await runStartupServiceGate({ mode: 'local', services: [row('flaky', probe, { repair })] })

  assert.equal(result.status, 'ready')
  assert.equal(repair.mock.calls.length, 1)
  assert.equal(probe.mock.calls.length, 2)
  assert.equal(result.outcomes[0].repairAttempted, true)
  assert.equal(result.outcomes[0].reprobed, true)
})

test('a still-failing service is repaired once, never twice', async () => {
  const repair = vi.fn(async () => {})
  const probe = vi.fn(async () => ({ ready: false, via: 'probe', detail: 'still down' }))

  const result = await runStartupServiceGate({ mode: 'local', services: [row('dead', probe, { repair })] })

  assert.equal(result.status, 'degraded')
  assert.equal(repair.mock.calls.length, 1)
  assert.equal(probe.mock.calls.length, 2)
})

test('a repair that throws is still counted as the one bounded attempt', async () => {
  const repair = vi.fn(async () => {
    throw new Error('owner refused the repair')
  })

  const probe = vi.fn(async () => ({ ready: false, via: 'probe', detail: 'down' }))
  const result = await runStartupServiceGate({ mode: 'local', services: [row('dead', probe, { repair })] })

  assert.equal(result.status, 'degraded')
  assert.equal(repair.mock.calls.length, 1)
  assert.equal(probe.mock.calls.length, 1)
  assert.match(result.report!.attemptedRecovery, /owner refused the repair/)
})

test('a service with no owner-side repair fails terminally without any repair attempt', async () => {
  const result = await runStartupServiceGate({
    mode: 'local',
    services: [row('unrecoverable', async () => ({ ready: false, via: 'probe', detail: 'gone' }))]
  })

  assert.equal(result.status, 'degraded')
  assert.equal(result.outcomes[0].repairAttempted, false)
  assert.match(result.report!.attemptedRecovery, /no owner-side repair/)
})

test('a probe that throws is a failure, not a crash', async () => {
  const result = await runStartupServiceGate({
    mode: 'local',
    services: [
      row('boom', async () => {
        throw new Error('probe exploded')
      })
    ]
  })

  assert.equal(result.status, 'degraded')
  assert.match(result.report!.cause, /probe exploded/)
})

// ── normalized degraded report ─────────────────────────────────────────────

test('a terminal failure produces one normalized degraded report naming service, cause, recovery and next action', async () => {
  const result = await runStartupServiceGate({
    mode: 'local',
    services: [
      row('gateway-socket', async () => ({ ready: false, via: 'connectionState', detail: 'socket closed' }), {
        label: 'Gateway socket',
        nextAction: 'Open Settings → Gateway and reconnect.',
        owner: 'renderer:HermesGateway',
        repair: async () => {}
      })
    ]
  })

  assert.equal(result.status, 'degraded')
  assert.deepEqual(result.report, {
    attemptedRecovery: 'One bounded repair through renderer:HermesGateway, then one re-probe; still not ready.',
    cause: 'socket closed',
    nextAction: 'Open Settings → Gateway and reconnect.',
    owner: 'renderer:HermesGateway',
    service: 'Gateway socket',
    serviceId: 'gateway-socket'
  })

  const message = formatStartupDegradedMessage(result.report!)
  assert.match(message, /Gateway socket/)
  assert.match(message, /socket closed/)
  assert.match(message, /re-probe/)
  assert.match(message, /Open Settings → Gateway and reconnect\./)
})

// ── cancellation: no owner work, and no verdict, after the caller is gone ──

test('a run cancelled before its first probe never probes anything', async () => {
  const probe = vi.fn(async () => ({ ready: true, via: 'probe' }))

  const result = await runStartupServiceGate({
    cancelled: () => true,
    mode: 'local',
    services: [row('required', probe)]
  })

  assert.equal(probe.mock.calls.length, 0)
  assert.equal(result.status, 'cancelled')
  assert.equal(result.report, null)
})

test('a cancelled run stops before the next required probe', async () => {
  let cancelled = false
  const second = vi.fn(async () => ({ ready: true, via: 'probe' }))

  const result = await runStartupServiceGate({
    cancelled: () => cancelled,
    mode: 'local',
    services: [
      row('first', async () => {
        // The effect that started this run was torn down mid-acceptance.
        cancelled = true

        return { ready: true, via: 'probe' }
      }),
      row('second', second)
    ]
  })

  assert.equal(second.mock.calls.length, 0)
  assert.equal(result.status, 'cancelled')
  assert.equal(result.report, null)
})

test('cancellation during the one owner repair stops the run before the re-probe', async () => {
  let cancelled = false
  const probe = vi.fn(async () => ({ detail: 'down', ready: false, via: 'probe' }))

  const repair = vi.fn(async () => {
    // Teardown lands while the owner's bounded repair is still in flight.
    cancelled = true
  })

  const result = await runStartupServiceGate({
    cancelled: () => cancelled,
    mode: 'local',
    services: [row('flaky', probe, { repair })]
  })

  assert.equal(repair.mock.calls.length, 1)
  assert.equal(probe.mock.calls.length, 1, 'the re-probe must not run after cancellation')
  assert.equal(result.status, 'cancelled')
  assert.equal(result.report, null)
})

test('a cancelled run neither completes nor degrades the boot', async () => {
  const complete = vi.fn()
  const degrade = vi.fn()

  const result = await runStartupServiceGate({
    cancelled: () => true,
    mode: 'local',
    services: [row('required', async () => ({ ready: true, via: 'probe' }))]
  })

  assert.equal(desktopBootMayComplete(result), false)
  assert.equal(completeDesktopBootWhenReady({ complete, degrade, result }), false)
  assert.equal(complete.mock.calls.length, 0)
  assert.equal(degrade.mock.calls.length, 0, 'a torn-down window must not be given a degraded status')
})

// ── the aggregate gate: completeDesktopBoot() unreachability ───────────────

test('completeDesktopBoot is reachable only after every mode-required row passed', async () => {
  const calls: string[] = []

  const services = [
    row(LOCAL_PRIMARY_BACKEND_SERVICE, async () => {
      calls.push(`probe:${LOCAL_PRIMARY_BACKEND_SERVICE}`)

      return { ready: true, via: 'main ledger' }
    }),
    row(GATEWAY_SOCKET_SERVICE, async () => {
      calls.push(`probe:${GATEWAY_SOCKET_SERVICE}`)

      return { ready: true, via: 'connectionState' }
    }),
    row(HERMES_CONFIG_SERVICE, async () => {
      calls.push(`probe:${HERMES_CONFIG_SERVICE}`)

      return { ready: true, via: 'refreshHermesConfig' }
    })
  ]

  const result = await runStartupServiceGate({ mode: 'local', services })

  const completed = completeDesktopBootWhenReady({
    complete: () => calls.push('completeDesktopBoot'),
    degrade: () => calls.push('degradeDesktopBoot'),
    result
  })

  assert.equal(completed, true)
  assert.deepEqual(calls, [
    `probe:${LOCAL_PRIMARY_BACKEND_SERVICE}`,
    `probe:${GATEWAY_SOCKET_SERVICE}`,
    `probe:${HERMES_CONFIG_SERVICE}`,
    'completeDesktopBoot'
  ])
})

test('completeDesktopBoot is never called when any mode-required row fails', async () => {
  const complete = vi.fn()
  const degrade = vi.fn()

  const result = await runStartupServiceGate({
    mode: 'local',
    services: [
      row(LOCAL_PRIMARY_BACKEND_SERVICE, async () => ({ ready: true, via: 'main ledger' })),
      row(GATEWAY_SOCKET_SERVICE, async () => ({ ready: false, via: 'connectionState', detail: 'closed' })),
      row(HERMES_CONFIG_SERVICE, async () => ({ ready: true, via: 'refreshHermesConfig' }))
    ]
  })

  const completed = completeDesktopBootWhenReady({ complete, degrade, result })

  assert.equal(completed, false)
  assert.equal(complete.mock.calls.length, 0)
  assert.equal(degrade.mock.calls.length, 1)
  assert.equal(degrade.mock.calls[0][0].serviceId, GATEWAY_SOCKET_SERVICE)
})

test('bypassing the aggregate gate fails: a forged ready verdict cannot complete boot', () => {
  const forged = {
    mode: 'local' as const,
    outcomes: [
      {
        detail: 'closed',
        id: GATEWAY_SOCKET_SERVICE,
        label: 'Gateway socket',
        owner: 'renderer:HermesGateway',
        repairAttempted: true,
        reprobed: true,
        required: true,
        status: 'failed' as const,
        via: 'connectionState'
      }
    ],
    report: null,
    status: 'ready' as const
  }

  assert.equal(desktopBootMayComplete(forged), false)

  const complete = vi.fn()
  const degrade = vi.fn()
  assert.equal(completeDesktopBootWhenReady({ complete, degrade, result: forged }), false)
  assert.equal(complete.mock.calls.length, 0)
  assert.equal(degrade.mock.calls.length, 1)
})

test('bypassing the aggregate gate fails: an empty or missing verdict cannot complete boot', () => {
  assert.equal(desktopBootMayComplete(null), false)
  assert.equal(desktopBootMayComplete(undefined), false)
  assert.equal(
    desktopBootMayComplete({ mode: 'local', outcomes: [], report: null, status: 'ready' }),
    false,
    'a verdict with no required rows is not an acceptance'
  )

  const complete = vi.fn()
  const degrade = vi.fn()
  assert.equal(completeDesktopBootWhenReady({ complete, degrade, result: null }), false)
  assert.equal(complete.mock.calls.length, 0)
  assert.equal(degrade.mock.calls.length, 1)
  assert.equal(degrade.mock.calls[0][0].serviceId, 'startup-service-gate')
})

test('a passing verdict whose optional rows were skipped still completes boot', async () => {
  const result = await runStartupServiceGate({
    mode: 'local',
    services: [
      row('required', async () => ({ ready: true, via: 'probe' })),
      row('lazy', async () => ({ ready: false, via: 'probe' }), { requiredFor: [] })
    ]
  })

  const complete = vi.fn()
  const degrade = vi.fn()

  assert.equal(completeDesktopBootWhenReady({ complete, degrade, result }), true)
  assert.equal(complete.mock.calls.length, 1)
  assert.equal(degrade.mock.calls.length, 0)
})

// ── main-process ledger: authoritative probe evidence, not PID existence ───

test('the ledger records the authoritative probe evidence main actually observed', () => {
  const ledger = createStartupServiceLedger(() => 1234)

  ledger.record({
    detail: null,
    id: LOCAL_PRIMARY_BACKEND_SERVICE,
    label: 'Local Hermes backend',
    owner: 'main:startHermes',
    status: 'ready',
    via: '/api/health + /api/ws session-token probe'
  })

  assert.deepEqual(ledger.rows(), [
    {
      at: 1234,
      detail: null,
      id: LOCAL_PRIMARY_BACKEND_SERVICE,
      label: 'Local Hermes backend',
      owner: 'main:startHermes',
      status: 'ready',
      via: '/api/health + /api/ws session-token probe'
    }
  ])
})

test('a later record for the same service replaces the earlier one', () => {
  const ledger = createStartupServiceLedger(() => 1)

  ledger.record({
    detail: null,
    id: LOCAL_PRIMARY_BACKEND_SERVICE,
    label: 'Local Hermes backend',
    owner: 'main:startHermes',
    status: 'ready',
    via: '/api/health'
  })
  ledger.record({
    detail: 'exited',
    id: LOCAL_PRIMARY_BACKEND_SERVICE,
    label: 'Local Hermes backend',
    owner: 'main:startHermes',
    status: 'failed',
    via: '/api/health'
  })

  assert.equal(ledger.rows().length, 1)
  assert.equal(ledger.rows()[0].status, 'failed')
})

test('a record without real probe evidence is not readable as a ready service', () => {
  const snapshot = {
    startupServices: [
      { at: 1, detail: null, id: 'pid-only', label: 'PID only', owner: 'main', status: 'ready', via: '' },
      {
        at: 2,
        detail: null,
        id: LOCAL_PRIMARY_BACKEND_SERVICE,
        label: 'Local Hermes backend',
        owner: 'main:startHermes',
        status: 'ready',
        via: '/api/health'
      }
    ]
  }

  assert.deepEqual(
    readStartupServiceRecords(snapshot).map(record => record.id),
    [LOCAL_PRIMARY_BACKEND_SERVICE]
  )
  assert.equal(findStartupServiceRecord(snapshot, 'pid-only'), null)
  assert.equal(findStartupServiceRecord(snapshot, LOCAL_PRIMARY_BACKEND_SERVICE)?.via, '/api/health')
})

test('a runtime that publishes no ledger is distinguishable from one that published an empty ledger', () => {
  // The compatibility ladder turns on exactly this distinction: "this main
  // process cannot report startup services" must not read the same as "this
  // main process reports that nothing is ready".
  assert.equal(hasStartupServiceLedger({ phase: 'backend.ready', progress: 94 }), false)
  assert.equal(hasStartupServiceLedger({ startupServices: 'nope' }), false)
  assert.equal(hasStartupServiceLedger(null), false)
  assert.equal(hasStartupServiceLedger(undefined), false)
  assert.equal(hasStartupServiceLedger({ startupServices: [] }), true)
  assert.equal(
    hasStartupServiceLedger({
      startupServices: [
        {
          at: 1,
          detail: null,
          id: LOCAL_PRIMARY_BACKEND_SERVICE,
          label: 'Local Hermes backend',
          owner: 'main:startHermes',
          status: 'ready',
          via: '/api/health'
        }
      ]
    }),
    true
  )
})

test('a malformed or absent boot-progress snapshot reads as no records', () => {
  assert.deepEqual(readStartupServiceRecords(null), [])
  assert.deepEqual(readStartupServiceRecords({}), [])
  assert.deepEqual(readStartupServiceRecords({ startupServices: 'nope' }), [])
  assert.deepEqual(readStartupServiceRecords({ startupServices: [null, 7, {}] }), [])
  assert.equal(findStartupServiceRecord(undefined, LOCAL_PRIMARY_BACKEND_SERVICE), null)
})

// ── mode resolution ────────────────────────────────────────────────────────

test('the startup mode follows the resolved primary connection', () => {
  assert.equal(startupModeFromConnection({ mode: 'remote' }), 'remote')
  assert.equal(startupModeFromConnection({ mode: 'local' }), 'local')
  assert.equal(startupModeFromConnection(null), 'local')
  assert.equal(startupModeFromConnection({}), 'local')
})

test('a cold start with no connection takes its mode from the primary row main recorded', () => {
  // Nothing resolved, so there is no connection to read the mode off. Main
  // recorded the primary row THIS boot required, and that row names the mode.
  const record = (id: string) => ({
    startupServices: [
      { at: 1, detail: 'refused', id, label: id, owner: 'main:startHermes', status: 'failed', via: 'main:startHermes' }
    ]
  })

  assert.equal(startupModeFromStartupServices(record(REMOTE_PRIMARY_BACKEND_SERVICE)), 'remote')
  assert.equal(startupModeFromStartupServices(record(LOCAL_PRIMARY_BACKEND_SERVICE)), 'local')
  // A runtime that predates the ledger reports no rows at all: fall back
  // rather than inventing a mode.
  assert.equal(startupModeFromStartupServices({ phase: 'backend.error' }), 'local')
  assert.equal(startupModeFromStartupServices(null), 'local')
  assert.equal(startupModeFromStartupServices(null, 'remote'), 'remote')
})
