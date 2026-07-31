import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { EventEmitter, once } from 'node:events'
import {
  existsSync,
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PassThrough } from 'node:stream'
import { test } from 'node:test'

import * as verifierLib from './desktop-verifier-lib.mjs'
import { launchDirectOwnedVerifierTestProcess } from './owned-verifier-test-process.mjs'

const JOB_HOST_BOOTSTRAP = fileURLToPath(
  new URL('./windows-verifier-job-host.ps1', import.meta.url)
)
const JOB_HOST_SOURCE = fileURLToPath(
  new URL('./windows-verifier-job-host.cs', import.meta.url)
)
const WINDOWS_ONLY = { skip: process.platform !== 'win32' }
const PREPARATION_DEADLINE_MS = 45_000
const CONTROLLER_DEADLINE_MS = 20_000
const MAX_CONTROLLER_OUTPUT_BYTES = 1_024

function createControllerRunRoot() {
  const root = mkdtempSync(join(tmpdir(), 'windows-job-controller-run-'))
  const hermesHome = join(root, 'hermes-home')
  const localAppData = join(root, 'local-app-data')
  const workspace = join(root, 'workspace')
  mkdirSync(hermesHome)
  mkdirSync(localAppData)
  mkdirSync(workspace)

  return { hermesHome, localAppData, root, workspace }
}

function portableTestTempBaseDir(environment = process.env) {
  const runnerTemp = environment.RUNNER_TEMP
  if (typeof runnerTemp === 'string' && runnerTemp.trim() !== '') {
    try {
      if (statSync(runnerTemp).isDirectory()) {
        return runnerTemp
      }
    } catch {
      // Fall through to Node's existing temporary directory.
    }
  }
  return tmpdir()
}

function createShortPreparationRunRoot(environment) {
  // Add-Type uses the full temporary DLL path in generated compiler metadata.
  // Keep only the cache-isolation fixture under a short, existing test-owned root.
  const root = mkdtempSync(join(portableTestTempBaseDir(environment), 'wjh-'))
  const hermesHome = join(root, 'h')
  const workspace = join(root, 'w')
  mkdirSync(hermesHome)
  mkdirSync(workspace)

  return { hermesHome, root, workspace }
}

function createPairedProductionPreparationFixture() {
  const fixture = createShortPreparationRunRoot()
  const shortLocalAppData = join(fixture.root, 'l')
  const longLocalAppData = join(
    fixture.root,
    'controlled-local-app-data',
    'length-controlled-segment',
    'a'.repeat(72)
  )
  mkdirSync(shortLocalAppData)
  mkdirSync(longLocalAppData, { recursive: true })

  return { ...fixture, longLocalAppData, shortLocalAppData }
}

function preparationOptions(fixture, { diagnostics = false } = {}) {
  return {
    cwd: fixture.workspace,
    encoding: 'utf8',
    env: {
      ...verifierLib.stripCredentialEnvironment(process.env),
      HERMES_HOME: fixture.hermesHome,
      ...(diagnostics ? { HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS: '1' } : {}),
      ...(fixture.localAppData ? { LOCALAPPDATA: fixture.localAppData } : {})
    },
    // Match the bounded runtime preparation allowance.  This runs before
    // the controller receipt deadline, so a cold compiler cannot consume
    // that separate protocol budget.
    timeout: 45_000,
    windowsHide: true
  }
}

function preparationArgs() {
  return [
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      JOB_HOST_BOOTSTRAP,
      '-Prepare'
    ]
  ]
}

async function runPreparation(fixture, {
  command,
  args,
  deadlineMs = PREPARATION_DEADLINE_MS,
  diagnostics = false,
  expectedSourceSha256 = sha256File(JOB_HOST_SOURCE)
} = {}) {
  const [defaultCommand, defaultArgs] = preparationArgs()
  const preparation = await launchDirectOwnedVerifierTestProcess(
    command ?? defaultCommand,
    args ?? defaultArgs,
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: {
        ...preparationOptions(fixture, { diagnostics }),
        stdio: ['ignore', 'ignore', 'pipe']
      }
    }
  )
  let stderr = ''
  preparation.child.stderr.setEncoding('utf8')
  preparation.child.stderr.on('data', chunk => {
    stderr += chunk
  })

  try {
    const status = await preparation.waitForExit(deadlineMs)
    return { status, stderr }
  } catch (error) {
    const diagnosticSummary = diagnostics
      ? safePreparationDiagnosticSummary(stderr, expectedSourceSha256)
      : ''
    if (diagnosticSummary !== '') {
      error.message = `${error.message}; ${diagnosticSummary}`
    }
    throw error
  } finally {
    await preparation.cleanup()
  }
}

async function startObservedPreparation(fixture, {
  command,
  args,
  diagnostics = true
} = {}) {
  const [defaultCommand, defaultArgs] = preparationArgs()
  const preparation = await launchDirectOwnedVerifierTestProcess(
    command ?? defaultCommand,
    args ?? defaultArgs,
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: {
        ...preparationOptions(fixture, { diagnostics }),
        stdio: ['ignore', 'ignore', 'pipe']
      }
    }
  )
  let stderr = ''
  preparation.child.stderr.setEncoding('utf8')
  preparation.child.stderr.on('data', chunk => {
    stderr += chunk
  })

  return {
    child: preparation.child,
    cleanup: () => preparation.cleanup(),
    readStderr: () => stderr,
    waitForExit: deadlineMs => preparation.waitForExit(deadlineMs)
  }
}

function sourceBoundPhaseEvent(stderr, eventName, phase, sourceHash) {
  const expression = new RegExp(
    `(?:^|\\r?\\n)(HermesVerifierJobHost diagnostic event=${eventName} phase=${phase} source_sha256=${sourceHash} sequence=\\d+)(?=\\r?$|\\r?\\n)`
  )
  return stderr.match(expression)?.[1] ?? null
}

async function waitForSourceBoundPhaseEvent(preparation, eventName, phase, sourceHash, deadlineMs) {
  const existing = sourceBoundPhaseEvent(preparation.readStderr(), eventName, phase, sourceHash)
  if (existing) {
    return existing
  }
  if (preparation.child.exitCode !== null || preparation.child.signalCode !== null) {
    throw new Error(`BOOTSTRAP_READY_UNOBSERVED:${phase}`)
  }

  return await new Promise((resolve, reject) => {
    let settled = false
    let timeout
    const finish = callback => value => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      preparation.child.stderr.removeListener('data', onData)
      preparation.child.removeListener('exit', onExit)
      callback(value)
    }
    const onData = () => {
      const marker = sourceBoundPhaseEvent(preparation.readStderr(), eventName, phase, sourceHash)
      if (marker) finish(resolve)(marker)
    }
    const onExit = () => finish(reject)(new Error(`BOOTSTRAP_READY_UNOBSERVED:${phase}`))
    timeout = setTimeout(
      () => finish(reject)(new Error(`BOOTSTRAP_READY_UNOBSERVED:${phase}`)),
      deadlineMs
    )
    preparation.child.stderr.on('data', onData)
    preparation.child.once('exit', onExit)
    if (preparation.child.exitCode !== null || preparation.child.signalCode !== null) {
      onExit()
      return
    }
    onData()
  })
}

function sourceBoundLockOpenEvents(stderr, sourceHash) {
  const expression = new RegExp(
    `HermesVerifierJobHost diagnostic event=(lock_open_enter|lock_open_outcome) attempt_seq=(\\d+)(?: outcome=(acquired|io_exception_retry|acquired_after_retry))? source_sha256=${sourceHash} sequence=(\\d+)`,
    'g'
  )
  return [...stderr.matchAll(expression)].map(match => ({
    attemptSequence: Number.parseInt(match[2], 10),
    event: match[1],
    outcome: match[3] ?? null,
    sequence: Number.parseInt(match[4], 10),
    text: match[0]
  }))
}

async function waitForSourceBoundLockOpenEvent(preparation, sourceHash, predicate, deadlineMs) {
  const find = () => sourceBoundLockOpenEvents(preparation.readStderr(), sourceHash).find(predicate)
  const existing = find()
  if (existing) return existing
  if (preparation.child.exitCode !== null || preparation.child.signalCode !== null) {
    throw new Error('LOCK_OPEN_EVENT_UNOBSERVED')
  }

  return await new Promise((resolve, reject) => {
    let settled = false
    let timeout
    const finish = callback => value => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      preparation.child.stderr.removeListener('data', onData)
      preparation.child.removeListener('exit', onExit)
      callback(value)
    }
    const onData = () => {
      const event = find()
      if (event) finish(resolve)(event)
    }
    const onExit = () => finish(reject)(new Error('LOCK_OPEN_EVENT_UNOBSERVED'))
    timeout = setTimeout(() => finish(reject)(new Error('LOCK_OPEN_EVENT_UNOBSERVED')), deadlineMs)
    preparation.child.stderr.on('data', onData)
    preparation.child.once('exit', onExit)
    if (preparation.child.exitCode !== null || preparation.child.signalCode !== null) {
      onExit()
      return
    }
    onData()
  })
}

function productionPreparationSpec(fixture) {
  const spec = verifierLib.createDesktopLaunchSpec({
    executable: process.execPath,
    baseEnv: { ...process.env, LOCALAPPDATA: fixture.shortLocalAppData },
    platform: 'win32',
    tempBaseDir: fixture.root
  })
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'
  return spec
}

function withControlledLocalAppData(spec, localAppData) {
  const env = { ...spec.env, LOCALAPPDATA: localAppData }
  return {
    ...spec,
    env,
    spawnOptions: { ...spec.spawnOptions, env }
  }
}

async function observeProductionPreparation(spec) {
  const startedAt = Date.now()
  try {
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 29_000
    })
    return { diagnostics, elapsedMs: Date.now() - startedAt, outcome: 'prepared' }
  } catch (error) {
    return { error: String(error.message), elapsedMs: Date.now() - startedAt, outcome: 'failed' }
  }
}

function boundedOutput(text) {
  return text.slice(0, MAX_CONTROLLER_OUTPUT_BYTES)
}

function preparationFailure(status, stderr) {
  const lockTimeout = /Windows verifier Job host bootstrap failed: verifier Job host cache lock timed out after \d+ ms/.test(stderr)
  return lockTimeout
    ? `status=${status}; cache-lock-timeout`
    : `status=${status}; preparation-failed`
}

function phaseElapsed(stderr, phase) {
  const match = stderr.match(new RegExp(
    `HermesVerifierJobHost diagnostic phase=${phase} elapsed_ms=(\\d+)`
  ))
  assert.ok(match, `expected ${phase} diagnostic`)
  return Number.parseInt(match[1], 10)
}

function phaseCount(stderr, phase) {
  return (stderr.match(new RegExp(
    `HermesVerifierJobHost diagnostic phase=${phase} elapsed_ms=\\d+`,
    'g'
  )) ?? []).length
}

function safePreparationDiagnosticSummary(stderr, expectedSourceSha256) {
  const entries = [
    ...[...stderr.matchAll(/HermesVerifierJobHost diagnostic compile_start source_sha256=([a-f0-9]{64})/g)]
      .filter(match => match[1] === expectedSourceSha256),
    ...[...stderr.matchAll(/HermesVerifierJobHost diagnostic compile_end source_sha256=([a-f0-9]{64}) output_sha256=([a-f0-9]{64})/g)]
      .filter(match => match[1] === expectedSourceSha256),
    ...[...stderr.matchAll(/^HermesVerifierJobHost diagnostic compile_outcome outcome=(exception) source_sha256=([a-f0-9]{64})\r?$/gm)]
      .filter(match => match[2] === expectedSourceSha256),
    ...stderr.matchAll(/HermesVerifierJobHost diagnostic phase=(lock_wait|validation|compile|publish) elapsed_ms=(\d+)/g)
  ].map(match => match[0])

  return [...new Set(entries)].join('; ')
}

function controllerPreparationDiagnosticSummary(stderr, expectedSourceSha256) {
  const phaseEvents = [...stderr.matchAll(new RegExp(
    `HermesVerifierJobHost diagnostic event=phase_(?:begin|end) phase=(?:cache_entry|cache_directory|lock_wait|mutex_wait|publication_lock_wait|validation|compile|publish) source_sha256=${expectedSourceSha256} sequence=\\d+`,
    'g'
  ))].map(match => match[0])
  const compileEvents = [
    ...stderr.matchAll(new RegExp(
      `HermesVerifierJobHost diagnostic compile_(?:start|end) source_sha256=${expectedSourceSha256}(?: output_sha256=[a-f0-9]{64})?`,
      'g'
    ))
  ].map(match => match[0])

  const lockOpenEvents = [
    ...stderr.matchAll(new RegExp(
      `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=\\d+ source_sha256=${expectedSourceSha256} sequence=\\d+`,
      'g'
    )),
    ...stderr.matchAll(new RegExp(
      `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=\\d+ outcome=(?:acquired|io_exception_retry|acquired_after_retry) source_sha256=${expectedSourceSha256} sequence=\\d+`,
      'g'
    ))
  ].map(match => match[0])

  return [...new Set([...phaseEvents, ...compileEvents, ...lockOpenEvents])].join('; ')
}

function assertBoundControllerPreparationDiagnostics(result, fixture) {
  const diagnostics = controllerPreparationDiagnosticSummary(
    result.stderr,
    sha256File(JOB_HOST_SOURCE)
  )
  assert.notEqual(diagnostics, '', 'expected source-bound controller preparation diagnostics')
  // `result.stderr` is the complete test-process stream. It can legitimately
  // contain the retained-root cleanup/authority contract, which is separately
  // asserted below. The operator-facing preparation summary must instead stay
  // limited to source-bound diagnostic markers.
  assert.equal(diagnostics.includes(fixture.root), false, 'diagnostic summary must not disclose the test root')
  assert.equal(diagnostics.includes('retained generated root:'), false, 'diagnostic summary must not include cleanup paths')
  return diagnostics
}

function preparationClassification(evidence) {
  const match = evidence.match(
    /HermesVerifierJobHost diagnostic classification=(DIRECT_CHILD_NONEXIT|PIPE_CLOSE_WAIT|PHASE_BEGIN_NO_END:[a-z_]+|ADD_TYPE_COMPILE_EXCEPTION|ADD_TYPE_COMPILE_STALL|DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP|FALSIFIED)/
  )
  assert.ok(match, 'expected one safe diagnostic classification')
  return match[1]
}

function mutexIdentity(stderr) {
  const match = stderr.match(
    /HermesVerifierJobHost diagnostic mutex_identity_sha256=([a-f0-9]{64})/
  )
  assert.ok(match, 'expected fixed-length pseudonymous mutex identity diagnostic')
  return match[1]
}

function sha256File(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function compileBoundary(stderr, boundary) {
  const output = boundary === 'end'
    ? ' output_sha256=([a-f0-9]{64})'
    : ''
  const match = stderr.match(new RegExp(
    `HermesVerifierJobHost diagnostic compile_${boundary} source_sha256=([a-f0-9]{64})${output}`
  ))
  assert.ok(match, `expected compile_${boundary} identity diagnostic`)
  return {
    sourceSha256: match[1],
    ...(boundary === 'end' ? { outputSha256: match[2] } : {})
  }
}

function precompileFailure(stderr) {
  const match = stderr.match(
    /HermesVerifierJobHost diagnostic precompile_failure source_sha256=([a-f0-9]{64}) class=(cache_root_unavailable)/
  )
  assert.ok(match, 'expected a structured pre-compile cache-root failure diagnostic')
  return { class: match[2], sourceSha256: match[1] }
}

async function holdNamedMutex(mutexIdentityHash) {
  const holder = await launchDirectOwnedVerifierTestProcess(
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$mutex = [System.Threading.Mutex]::new($false, 'Local\\HermesVerifierJobHost_${mutexIdentityHash}'); try { if (-not $mutex.WaitOne(5000)) { exit 2 }; [Console]::Out.WriteLine('READY'); Start-Sleep -Seconds 31 } finally { try { $mutex.ReleaseMutex() } catch {}; $mutex.Dispose() }`
    ],
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }
    }
  )
  holder.child.stdout.setEncoding('utf8')
  const [chunk] = await once(holder.child.stdout, 'data')
  assert.match(String(chunk), /READY/)
  return holder
}

async function abandonNamedMutex(mutexIdentityHash) {
  const owner = await launchDirectOwnedVerifierTestProcess(
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$mutex = [System.Threading.Mutex]::new($false, 'Local\\HermesVerifierJobHost_${mutexIdentityHash}'); if (-not $mutex.WaitOne(5000)) { exit 2 }; [Console]::Out.WriteLine('READY'); exit 0`
    ],
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }
    }
  )
  owner.child.stdout.setEncoding('utf8')
  const [chunk] = await once(owner.child.stdout, 'data')
  assert.match(String(chunk), /READY/)
  assert.equal(await owner.waitForExit(1_000), 0)
}

async function holdPublicationLock(lockPath) {
  const holder = await launchDirectOwnedVerifierTestProcess(
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$lock = [System.IO.File]::Open('${lockPath}', [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None); try { [Console]::Out.WriteLine('READY'); Start-Sleep -Seconds 31 } finally { $lock.Dispose() }`
    ],
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }
    }
  )
  holder.child.stdout.setEncoding('utf8')
  const [chunk] = await once(holder.child.stdout, 'data')
  assert.match(String(chunk), /READY/)
  return holder
}

async function holdReleaseSignaledPublicationLock(lockPath) {
  const escapedLockPath = lockPath.replaceAll("'", "''")
  const holder = await launchDirectOwnedVerifierTestProcess(
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-Command',
      `$lock = [System.IO.File]::Open('${escapedLockPath}', [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None); try { [Console]::Out.WriteLine('READY'); [Console]::Out.Flush(); [void][Console]::In.ReadLine() } finally { $lock.Dispose() }`
    ],
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true }
    }
  )
  holder.child.stdout.setEncoding('utf8')
  const [chunk] = await once(holder.child.stdout, 'data')
  assert.match(String(chunk), /READY/)
  return {
    ...holder,
    release: async () => {
      holder.child.stdin.write('RELEASE\n')
      holder.child.stdin.end()
      return await holder.waitForExit(5_000)
    }
  }
}

function cacheEntries(fixture) {
  const root = join(
    fixture.localAppData,
    'Hermes',
    'cache',
    'desktop-verifier-job-host',
    'v1'
  )
  if (!existsSync(root)) {
    return []
  }
  return readdirSync(root).map(entry => join(root, entry))
}

async function runRealController(fixture, input = '') {
  // Deep Mode 2 reconciliation for 52efb8e: prewarm is test-only and has a
  // 45-second owned-child deadline. Runtime controller preparation remains 29s.
  fixture.preparation ??= runPreparation(fixture, { diagnostics: true })
  const preparation = await fixture.preparation
  const diagnostics = assertBoundControllerPreparationDiagnostics(preparation, fixture)
  if (preparation.status !== 0) {
    throw new Error(
      `Windows Job host preparation failed before controller test: ` +
      `${preparationFailure(preparation.status, preparation.stderr)}; ${diagnostics}`
    )
  }

  const controller = await launchDirectOwnedVerifierTestProcess(
    'powershell.exe',
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      JOB_HOST_BOOTSTRAP
    ],
    {
      shutdownTimeoutMs: 1_000,
      spawnOptions: {
        cwd: fixture.workspace,
        env: {
          ...verifierLib.stripCredentialEnvironment(process.env),
          HERMES_HOME: fixture.hermesHome,
          HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS: '1',
          LOCALAPPDATA: fixture.localAppData
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true
      }
    }
  )
  let stdout = ''
  let stderr = ''
  controller.child.stdout.setEncoding('utf8')
  controller.child.stderr.setEncoding('utf8')
  controller.child.stdout.on('data', chunk => { stdout = boundedOutput(stdout + chunk) })
  controller.child.stderr.on('data', chunk => { stderr = boundedOutput(stderr + chunk) })
  const stdoutEnded = once(controller.child.stdout, 'end')
  const stderrEnded = once(controller.child.stderr, 'end')

  try {
    controller.child.stdin.end(input)
    const status = await controller.waitForExit(CONTROLLER_DEADLINE_MS)
    await Promise.all([stdoutEnded, stderrEnded])
    return { status, stderr, stdout }
  } finally {
    await controller.cleanup()
  }
}

test('Windows Job test fixtures use a portable existing temporary root and isolated local app data', () => {
  assert.equal(portableTestTempBaseDir({ RUNNER_TEMP: tmpdir() }), tmpdir())
  assert.equal(portableTestTempBaseDir({ RUNNER_TEMP: join(tmpdir(), 'missing-runner-temp') }), tmpdir())
  const runnerTempFile = join(tmpdir(), `runner-temp-file-${randomUUID()}`)
  writeFileSync(runnerTempFile, 'not-a-directory')
  const fixture = createControllerRunRoot()
  try {
    assert.equal(portableTestTempBaseDir({ RUNNER_TEMP: runnerTempFile }), tmpdir())
    assert.equal(existsSync(fixture.localAppData), true)
    assert.equal(fixture.localAppData.startsWith(fixture.root), true)
    const forbiddenDriveRoot = ['C:', 'tmp'].join('\\')
    assert.equal(readFileSync(fileURLToPath(import.meta.url), 'utf8').includes(forbiddenDriveRoot), false)
  } finally {
    rmSync(runnerTempFile, { force: true })
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('Windows Job host cache cold, warm, corrupt, and concurrent preparation remains bounded', WINDOWS_ONLY, async () => {
  const fixture = createControllerRunRoot()
  const sentinel = await launchDirectOwnedVerifierTestProcess(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1_000)'],
    { spawnOptions: { stdio: 'ignore', windowsHide: true } }
  )

  try {
    const cold = await runPreparation(fixture, { diagnostics: true })
    assert.equal(cold.status, 0, 'cold preparation')
    assertBoundControllerPreparationDiagnostics(cold, fixture)
    const entries = cacheEntries(fixture)
    assert.equal(entries.length, 1)
    const entry = entries[0]
    const dll = join(entry, 'HermesVerifierJobHost.dll')
    const manifest = join(entry, 'manifest.json')
    assert.equal(existsSync(dll), true)
    assert.equal(existsSync(manifest), true)

    const warm = await runPreparation(fixture, { diagnostics: true })
    assert.equal(warm.status, 0, 'warm preparation')
    assertBoundControllerPreparationDiagnostics(warm, fixture)
    writeFileSync(dll, 'corrupt-cache')
    const corrupt = await runPreparation(fixture, { diagnostics: true })
    assert.equal(corrupt.status, 0, 'corrupt cache recovery')
    assertBoundControllerPreparationDiagnostics(corrupt, fixture)
    assert.notEqual(readFileSync(dll, 'utf8'), 'corrupt-cache')

    const preparations = await Promise.all([
      runPreparation(fixture, { diagnostics: true }),
      runPreparation(fixture, { diagnostics: true })
    ])
    assert.deepEqual(preparations.map(result => result.status), [0, 0])
    for (const result of preparations) {
      assertBoundControllerPreparationDiagnostics(result, fixture)
    }
    assert.equal(existsSync(manifest), true)
    assert.equal(sentinel.child.exitCode, null, 'unrelated sentinel must remain alive')
  } finally {
    await sentinel.cleanup()
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('Windows Job cold preparation binds its exact source and fresh output without path disclosure', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'cold-identity-cache')
  mkdirSync(fixture.localAppData)

  try {
    assert.deepEqual(cacheEntries(fixture), [], 'isolated cache must start empty')
    const result = await runPreparation(fixture, { diagnostics: true })
    assert.equal(result.status, 0)
    const started = compileBoundary(result.stderr, 'start')
    const finished = compileBoundary(result.stderr, 'end')
    assert.equal(started.sourceSha256, finished.sourceSha256)
    for (const phase of ['cache_entry', 'cache_directory', 'compile', 'publish']) {
      assert.match(
        result.stderr,
        new RegExp(`event=phase_begin phase=${phase} source_sha256=${started.sourceSha256} sequence=\\d+`)
      )
      assert.match(
        result.stderr,
        new RegExp(`event=phase_end phase=${phase} source_sha256=${started.sourceSha256} sequence=\\d+`)
      )
    }

    const [entry] = cacheEntries(fixture)
    const outputPath = join(entry, 'HermesVerifierJobHost.dll')
    assert.equal(existsSync(outputPath), true)
    assert.equal(finished.outputSha256, sha256File(outputPath))
    assert.equal(result.stderr.includes(fixture.root), false, 'diagnostics must not disclose fixture paths')
  } finally {
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job production preparation pairs only controlled LOCALAPPDATA roots', WINDOWS_ONLY, async () => {
  const fixture = createPairedProductionPreparationFixture()
  const baseSpec = productionPreparationSpec(fixture)
  const shortSpec = withControlledLocalAppData(baseSpec, fixture.shortLocalAppData)
  const longSpec = withControlledLocalAppData(baseSpec, fixture.longLocalAppData)

  try {
    assert.deepEqual(cacheEntries({ localAppData: fixture.shortLocalAppData }), [], 'short cache starts empty')
    assert.deepEqual(cacheEntries({ localAppData: fixture.longLocalAppData }), [], 'long cache starts empty')
    assert.equal(shortSpec.spawnOptions.cwd, longSpec.spawnOptions.cwd, 'only LOCALAPPDATA may vary')
    assert.equal(shortSpec.env.HERMES_HOME, longSpec.env.HERMES_HOME, 'only LOCALAPPDATA may vary')
    assert.equal(shortSpec.env.LOCALAPPDATA, fixture.shortLocalAppData)
    assert.equal(longSpec.env.LOCALAPPDATA, fixture.longLocalAppData)
    assert.equal(shortSpec.env.PATH, longSpec.env.PATH, 'the command environment stays constant')
    const changedEnvironmentNames = [...new Set([
      ...Object.keys(shortSpec.env),
      ...Object.keys(longSpec.env)
    ])].filter(name => shortSpec.env[name] !== longSpec.env[name]).sort()
    assert.deepEqual(changedEnvironmentNames, ['LOCALAPPDATA'], 'only LOCALAPPDATA may vary')

    const short = await observeProductionPreparation(shortSpec)
    const long = await observeProductionPreparation(longSpec)
    const sourceSha256 = sha256File(JOB_HOST_SOURCE)

    for (const [label, observation, localAppData] of [
      ['short', short, fixture.shortLocalAppData],
      ['long', long, fixture.longLocalAppData]
    ]) {
      assert.ok(observation.elapsedMs < 31_000, `${label} observation must preserve the 29-second bound`)
      const evidence = observation.outcome === 'prepared'
        ? observation.diagnostics
        : observation.error
      assert.equal(evidence.includes(localAppData), false, `${label} evidence must not disclose LOCALAPPDATA`)
      assert.equal(evidence.includes(fixture.root), false, `${label} evidence must not disclose fixture paths`)
      const classification = preparationClassification(evidence)
      if (observation.outcome === 'prepared') {
        assert.equal(classification, 'FALSIFIED', `${label} completed preparation should falsify the timeout hypothesis`)
        if (/HermesVerifierJobHost diagnostic compile_start/.test(evidence)) {
          const started = compileBoundary(evidence, 'start')
          const finished = compileBoundary(evidence, 'end')
          assert.equal(started.sourceSha256, sourceSha256)
          assert.equal(finished.sourceSha256, sourceSha256)
        } else {
          assert.match(
            evidence,
            new RegExp(`event=phase_end phase=validation source_sha256=${sourceSha256}`),
            `${label} prepared through an already-valid cache without claiming a fresh compile`
          )
        }
      } else {
        assert.match(evidence, /preparation timed out after 29000ms|preparation failed/i)
        if (/precompile_failure/.test(evidence)) {
          const failure = precompileFailure(evidence)
          assert.equal(failure.sourceSha256, sourceSha256)
          assert.equal(failure.class, 'cache_root_unavailable')
        }
      }
    }

    assert.equal(
      shortSpec.env.LOCALAPPDATA === longSpec.env.LOCALAPPDATA,
      false,
      'the paired observations must differ only by their explicitly controlled roots'
    )
  } finally {
    verifierLib.cleanupUnlaunchedDesktopSpec(baseSpec)
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job host cache lock is root-scoped, canonical, and diagnostic without leaking paths', WINDOWS_ONLY, async () => {
  const first = createShortPreparationRunRoot()
  const second = createShortPreparationRunRoot()
  const shared = createShortPreparationRunRoot()
  const sourceHash = sha256File(JOB_HOST_SOURCE)
  first.localAppData = join(first.root, 'never-log-a')
  second.localAppData = join(second.root, 'never-log-b')
  shared.localAppData = join(shared.root, 'never-log-c')
  mkdirSync(first.localAppData)
  mkdirSync(second.localAppData)
  mkdirSync(shared.localAppData)
  const sharedAlias = { ...shared, localAppData: `${shared.localAppData}\\` }

  try {
    const [firstResult, secondResult] = await Promise.all([
      runPreparation(first, { diagnostics: true }),
      runPreparation(second, { diagnostics: true })
    ])
    assert.deepEqual([firstResult.status, secondResult.status], [0, 0])
    assert.equal(phaseCount(firstResult.stderr, 'compile'), 1)
    assert.equal(phaseCount(secondResult.stderr, 'compile'), 1)
    assert.ok(phaseElapsed(firstResult.stderr, 'lock_wait') < 1_000)
    assert.ok(phaseElapsed(secondResult.stderr, 'lock_wait') < 1_000)

    const [canonicalResult, aliasResult] = await Promise.all([
      runPreparation(shared, { diagnostics: true }),
      runPreparation(sharedAlias, { diagnostics: true })
    ])
    assert.deepEqual([canonicalResult.status, aliasResult.status], [0, 0])
    assert.equal(
      phaseCount(canonicalResult.stderr, 'compile') + phaseCount(aliasResult.stderr, 'compile'),
      1,
      'alias-equivalent cache roots must share one mutex and one cache publication'
    )
    assert.equal(
      phaseCount(canonicalResult.stderr, 'publish') + phaseCount(aliasResult.stderr, 'publish'),
      2,
      'only the compiler may publish the DLL and manifest'
    )

    const physicalAliasRoot = join(shared.root, 'physical-alias')
    symlinkSync(shared.localAppData, physicalAliasRoot, 'junction')
    const physicalAlias = { ...shared, localAppData: physicalAliasRoot }
    const [sharedEntry] = cacheEntries(shared)
    writeFileSync(join(sharedEntry, 'HermesVerifierJobHost.dll'), 'corrupt-cache')
    const [physicalResult, physicalAliasResult] = await Promise.all([
      runPreparation(shared, { diagnostics: true }),
      runPreparation(physicalAlias, { diagnostics: true })
    ])
    assert.deepEqual([physicalResult.status, physicalAliasResult.status], [0, 0])
    assert.equal(
      phaseCount(physicalResult.stderr, 'compile') + phaseCount(physicalAliasResult.stderr, 'compile'),
      1,
      'physical aliases must share the publication lock and compile once'
    )

    for (const { stderr, forbiddenRoot } of [
      { stderr: firstResult.stderr, forbiddenRoot: first.localAppData },
      { stderr: secondResult.stderr, forbiddenRoot: second.localAppData },
      { stderr: canonicalResult.stderr, forbiddenRoot: shared.localAppData },
      { stderr: aliasResult.stderr, forbiddenRoot: shared.localAppData },
      { stderr: physicalResult.stderr, forbiddenRoot: shared.localAppData },
      { stderr: physicalAliasResult.stderr, forbiddenRoot: physicalAliasRoot }
    ]) {
      for (const phase of ['lock_wait', 'validation']) {
        assert.ok(phaseCount(stderr, phase) >= 1, `expected ${phase} timing`)
      }
      for (const phase of ['lock_wait', 'mutex_wait', 'publication_lock_wait']) {
        assert.match(
          stderr,
          new RegExp(`event=phase_begin phase=${phase} source_sha256=${sourceHash} sequence=\\d+`),
          `a successful preparation must enter the source-bound ${phase} phase`
        )
        assert.match(
          stderr,
          new RegExp(`event=phase_end phase=${phase} source_sha256=${sourceHash} sequence=\\d+`),
          `a successful preparation must leave the source-bound ${phase} phase`
        )
      }
      const lockOpenEvents = sourceBoundLockOpenEvents(stderr, sourceHash)
      assert.ok(
        lockOpenEvents.some(event => event.event === 'lock_open_enter' && event.attemptSequence === 1),
        'every physical alias preparation must record its first native publication-lock open attempt'
      )
      assert.ok(
        lockOpenEvents.some(event => event.event === 'lock_open_outcome' && ['acquired', 'acquired_after_retry'].includes(event.outcome)),
        'physical aliases must retain a source-bound native publication-lock acquisition outcome'
      )
      assert.equal(stderr.includes(forbiddenRoot), false, 'diagnostics must not disclose a cache root')
    }
  } finally {
    for (const fixture of [first, second, shared]) {
      rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
    }
  }
})

test('Windows Job host observes an owned mutex stall only after the bootstrap mutex marker', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'owned-lock-stall-cache')
  mkdirSync(fixture.localAppData)
  let holder
  let preparation

  try {
    const warm = await runPreparation(fixture, { diagnostics: true })
    assert.equal(warm.status, 0)
    const [entry] = cacheEntries(fixture)
    writeFileSync(join(entry, 'HermesVerifierJobHost.dll'), 'corrupt-cache')
    holder = await holdNamedMutex(mutexIdentity(warm.stderr))
    preparation = await startObservedPreparation(fixture)
    const sourceHash = sha256File(JOB_HOST_SOURCE)
    await waitForSourceBoundPhaseEvent(
      preparation,
      'phase_begin',
      'mutex_wait',
      sourceHash,
      PREPARATION_DEADLINE_MS
    )
    await assert.rejects(preparation.waitForExit(2_000), /did not exit within 2000ms/i)
    const evidence = preparation.readStderr()
    assert.ok(sourceBoundPhaseEvent(evidence, 'phase_begin', 'mutex_wait', sourceHash))
    assert.equal(sourceBoundPhaseEvent(evidence, 'phase_end', 'mutex_wait', sourceHash), null)
    assert.equal(
      verifierLib.classifyWindowsJobHostPreparationDiagnostics(evidence),
      'PHASE_BEGIN_NO_END:mutex_wait'
    )
    assert.equal(evidence.includes(fixture.root), false, 'lock-stall evidence must not disclose the fixture root')
  } finally {
    await preparation?.cleanup()
    await holder?.cleanup()
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job host observes a held publication lock only after bootstrap readiness', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'publication-lock-stall-cache')
  mkdirSync(fixture.localAppData)
  let holder
  let preparation

  try {
    const warm = await runPreparation(fixture, { diagnostics: true })
    assert.equal(warm.status, 0)
    const [entry] = cacheEntries(fixture)
    writeFileSync(join(entry, 'HermesVerifierJobHost.dll'), 'corrupt-cache')
    await abandonNamedMutex(mutexIdentity(warm.stderr))
    holder = await holdPublicationLock(join(entry, 'publication.lock'))
    preparation = await startObservedPreparation(fixture)
    const sourceHash = sha256File(JOB_HOST_SOURCE)
    await waitForSourceBoundPhaseEvent(
      preparation,
      'phase_begin',
      'publication_lock_wait',
      sourceHash,
      PREPARATION_DEADLINE_MS
    )
    await assert.rejects(preparation.waitForExit(2_000), /did not exit within 2000ms/i)
    const evidence = preparation.readStderr()
    assert.ok(sourceBoundPhaseEvent(evidence, 'phase_begin', 'mutex_wait', sourceHash))
    assert.ok(sourceBoundPhaseEvent(evidence, 'phase_end', 'mutex_wait', sourceHash))
    assert.ok(sourceBoundPhaseEvent(evidence, 'phase_begin', 'publication_lock_wait', sourceHash))
    assert.equal(sourceBoundPhaseEvent(evidence, 'phase_end', 'publication_lock_wait', sourceHash), null)
    assert.equal(
      verifierLib.classifyWindowsJobHostPreparationDiagnostics(evidence),
      'PHASE_BEGIN_NO_END:publication_lock_wait'
    )
    assert.equal(evidence.includes(fixture.root), false, 'publication-lock evidence must not disclose the fixture root')
  } finally {
    await preparation?.cleanup()
    await holder?.cleanup()
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job host records native publication-lock retries with a release-signaled owned holder', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'publication-lock-release-cache')
  mkdirSync(fixture.localAppData)
  let holder
  let preparation

  try {
    const warm = await runPreparation(fixture, { diagnostics: true })
    assert.equal(warm.status, 0)
    const [entry] = cacheEntries(fixture)
    writeFileSync(join(entry, 'HermesVerifierJobHost.dll'), 'corrupt-cache')
    await abandonNamedMutex(mutexIdentity(warm.stderr))
    holder = await holdReleaseSignaledPublicationLock(join(entry, 'publication.lock'))
    preparation = await startObservedPreparation(fixture)
    const sourceHash = sha256File(JOB_HOST_SOURCE)
    const firstEnter = await waitForSourceBoundLockOpenEvent(
      preparation,
      sourceHash,
      event => event.event === 'lock_open_enter' && event.attemptSequence === 1,
      PREPARATION_DEADLINE_MS
    )
    const retry = await waitForSourceBoundLockOpenEvent(
      preparation,
      sourceHash,
      event => event.event === 'lock_open_outcome' && event.attemptSequence === 1 && event.outcome === 'io_exception_retry',
      PREPARATION_DEADLINE_MS
    )
    assert.ok(retry.sequence > firstEnter.sequence)
    assert.equal(await holder.release(), 0)
    const completed = await preparation.waitForExit(PREPARATION_DEADLINE_MS)
    assert.equal(completed, 0)

    const events = sourceBoundLockOpenEvents(preparation.readStderr(), sourceHash)
    const acquiredAfterRetry = events.find(event => event.event === 'lock_open_outcome' && event.outcome === 'acquired_after_retry')
    assert.ok(acquiredAfterRetry, 'release must allow one retried native publication-lock open to acquire')
    assert.ok(acquiredAfterRetry.attemptSequence > retry.attemptSequence)
    assert.ok(acquiredAfterRetry.sequence > retry.sequence)
    assert.equal(
      events.filter(event => event.event === 'lock_open_enter' && event.attemptSequence === acquiredAfterRetry.attemptSequence).length,
      1,
      'every successful retry must retain exactly one source-bound enter marker'
    )
    assert.equal(preparation.readStderr().includes(fixture.root), false, 'lock-open evidence must not disclose the fixture root')
  } finally {
    await preparation?.cleanup()
    await holder?.cleanup()
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job owned-preparer handshake classifies a missing bootstrap marker without treating it as lock evidence', async () => {
  const fixture = createShortPreparationRunRoot()
  let preparation

  try {
    preparation = await startObservedPreparation(fixture, {
      command: process.execPath,
      args: ['-e', 'process.exit(0)'],
      diagnostics: false
    })
    await preparation.waitForExit(1_000)
    await assert.rejects(
      waitForSourceBoundPhaseEvent(
        preparation,
        'phase_begin',
        'mutex_wait',
        sha256File(JOB_HOST_SOURCE),
        1_000
      ),
      /BOOTSTRAP_READY_UNOBSERVED:mutex_wait/
    )
    assert.equal(preparation.readStderr(), '', 'the startup classification must not require raw diagnostic output')
  } finally {
    await preparation?.cleanup()
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job host lock contention fails diagnostically before the production preparation budget', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'contention-cache-root')
  mkdirSync(fixture.localAppData)
  let holder
  let productionSpec

  try {
    const warm = await runPreparation(fixture, { diagnostics: true })
    assert.equal(warm.status, 0)
    const [entry] = cacheEntries(fixture)
    writeFileSync(join(entry, 'HermesVerifierJobHost.dll'), 'corrupt-cache')
    await abandonNamedMutex(mutexIdentity(warm.stderr))
    holder = await holdPublicationLock(join(entry, 'publication.lock'))
    productionSpec = verifierLib.createDesktopLaunchSpec({
      executable: process.execPath,
      baseEnv: { ...process.env, LOCALAPPDATA: fixture.localAppData },
      platform: 'win32',
      tempBaseDir: fixture.root
    })
    productionSpec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'
    productionSpec.spawnOptions.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

    const startedAt = Date.now()
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(productionSpec, { prepareTimeoutMs: 29_000 }),
      /preparation failed; cache lock timeout/i
    )
    const elapsedMs = Date.now() - startedAt

    assert.ok(elapsedMs < 29_000, 'lock failure must return before the outer production budget')
  } finally {
    if (productionSpec) {
      verifierLib.cleanupUnlaunchedDesktopSpec(productionSpec)
    }
    await holder?.cleanup()
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('owned preparation reports an explicit nonzero status and stderr', async () => {
  const fixture = createControllerRunRoot()

  try {
    const result = await runPreparation(fixture, {
      command: process.execPath,
      args: ['-e', "process.stderr.write('expected preparer failure'); process.exit(7)"],
      deadlineMs: 1_000
    })

    assert.equal(result.status, 7)
    assert.match(result.stderr, /expected preparer failure/)
  } finally {
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('owned preparation deadline terminates only its direct preparer', async () => {
  const fixture = createControllerRunRoot()
  const sentinel = await launchDirectOwnedVerifierTestProcess(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1_000)'],
    { spawnOptions: { stdio: 'ignore', windowsHide: true } }
  )

  try {
    await assert.rejects(
      runPreparation(fixture, {
        command: process.execPath,
        args: ['-e', 'setInterval(() => {}, 1_000)'],
        deadlineMs: 25
      }),
      /did not exit within 25ms/i
    )
    assert.equal(sentinel.child.exitCode, null, 'unrelated sentinel must remain alive')
  } finally {
    await sentinel.cleanup()
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('owned preparation timeout reports only allowlisted cold-compile diagnostics', async () => {
  const fixture = createControllerRunRoot()
  const sourceHash = sha256File(JOB_HOST_SOURCE)
  const forgedSourceHash = 'b'.repeat(64)

  try {
    await assert.rejects(
      runPreparation(fixture, {
        command: process.execPath,
        args: [
          '-e',
          `process.stderr.write('HermesVerifierJobHost diagnostic compile_start source_sha256=${sourceHash}\\nHermesVerifierJobHost diagnostic compile_start source_sha256=${forgedSourceHash}\\n${fixture.root}\\n'); setInterval(() => {}, 1_000)`
        ],
        deadlineMs: 100,
        diagnostics: true
      }),
      error => {
        assert.match(error.message, new RegExp(`compile_start source_sha256=${sourceHash}`))
        assert.equal(error.message.includes(forgedSourceHash), false, 'unbound source identities must not become evidence')
        assert.equal(error.message.includes(fixture.root), false, 'timeout diagnostic must not disclose a fixture path')
        return true
      }
    )
  } finally {
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('Windows Job preparation diagnostics classify direct-child, pipe, phase, and compiler evidence without disclosure', () => {
  const sourceHash = sha256File(JOB_HOST_SOURCE)
  const fixedPrefix = 'HermesVerifierJobHost diagnostic'

  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} lifecycle=deadline elapsed_ms=29000`
    ),
    'DIRECT_CHILD_NONEXIT'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} lifecycle=exit code=0 elapsed_ms=29000`
    ),
    'PIPE_CLOSE_WAIT'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} event=phase_begin phase=cache_entry source_sha256=${sourceHash} sequence=1`
    ),
    'PHASE_BEGIN_NO_END:cache_entry'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} event=phase_begin phase=mutex_wait source_sha256=${sourceHash} sequence=1`
    ),
    'PHASE_BEGIN_NO_END:mutex_wait'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} event=phase_begin phase=mutex_wait source_sha256=${sourceHash} sequence=1\n` +
      `${fixedPrefix} event=phase_end phase=mutex_wait source_sha256=${sourceHash} sequence=2\n` +
      `${fixedPrefix} event=phase_begin phase=publication_lock_wait source_sha256=${sourceHash} sequence=3`
    ),
    'PHASE_BEGIN_NO_END:publication_lock_wait'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} compile_start source_sha256=${sourceHash}\n` +
      `${fixedPrefix} compile_outcome outcome=exception source_sha256=${sourceHash}`
    ),
    'ADD_TYPE_COMPILE_EXCEPTION'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} compile_start source_sha256=${sourceHash}\n${fixedPrefix} lifecycle=deadline elapsed_ms=29000`
    ),
    'ADD_TYPE_COMPILE_STALL'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} lifecycle=exit code=1 elapsed_ms=20`
    ),
    'PIPE_CLOSE_WAIT'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${fixedPrefix} lifecycle=exit code=1 elapsed_ms=20\n${fixedPrefix} lifecycle=stderr_end elapsed_ms=21`
    ),
    'DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics('untrusted C:\\fixture\\secret'),
    'DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP'
  )
})

test('Windows Job Add-Type exception keeps its failure path while emitting only a source-bound outcome', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  const bootstrapDir = join(fixture.root, 'controlled-bootstrap')
  const bootstrap = join(bootstrapDir, 'windows-verifier-job-host.ps1')
  const invalidSource = join(bootstrapDir, 'windows-verifier-job-host.cs')
  mkdirSync(bootstrapDir)
  copyFileSync(JOB_HOST_BOOTSTRAP, bootstrap)
  writeFileSync(invalidSource, 'public class HermesVerifierJobHost { this is not valid C# }\n', 'utf8')
  const sourceHash = sha256File(invalidSource)

  try {
    const result = await runPreparation(fixture, {
      command: 'powershell.exe',
      args: ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', bootstrap, '-Prepare'],
      diagnostics: true,
      expectedSourceSha256: sourceHash
    })
    assert.equal(result.status, 1, 'the original Add-Type failure must retain status 1')
    assert.match(result.stderr, new RegExp(`compile_start source_sha256=${sourceHash}`))
    assert.match(result.stderr, new RegExp(`compile_outcome outcome=exception source_sha256=${sourceHash}`))
    assert.doesNotMatch(result.stderr, new RegExp(`compile_end source_sha256=${sourceHash}`))
    assert.match(result.stderr, /Windows verifier Job host bootstrap failed:/)
    const summary = safePreparationDiagnosticSummary(result.stderr, sourceHash)
    assert.match(summary, new RegExp(`compile_outcome outcome=exception source_sha256=${sourceHash}`))
    assert.equal(summary.includes(fixture.root), false, 'consumer summary must not disclose the controlled fixture root')
  } finally {
    rmSync(fixture.root, { force: true, recursive: true })
  }
})

test('Windows Job lock-wait summaries retain only exact source-bound marker vocabulary', () => {
  const sourceHash = sha256File(JOB_HOST_SOURCE)
  const forgedSourceHash = 'c'.repeat(64)
  const pathBearingSuffix = ' C:\\fixture\\must-not-escape'
  const summary = controllerPreparationDiagnosticSummary(
    [
      `HermesVerifierJobHost diagnostic event=phase_begin phase=lock_wait source_sha256=${sourceHash} sequence=7${pathBearingSuffix}`,
      `HermesVerifierJobHost diagnostic event=phase_begin phase=mutex_wait source_sha256=${sourceHash} sequence=8`,
      `HermesVerifierJobHost diagnostic event=phase_end phase=mutex_wait source_sha256=${sourceHash} sequence=9`,
      `HermesVerifierJobHost diagnostic event=phase_begin phase=publication_lock_wait source_sha256=${sourceHash} sequence=10`,
      `HermesVerifierJobHost diagnostic event=phase_end phase=publication_lock_wait source_sha256=${sourceHash} sequence=11`,
      `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=1 source_sha256=${sourceHash} sequence=12${pathBearingSuffix}`,
      `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=1 outcome=io_exception_retry source_sha256=${sourceHash} sequence=13`,
      `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=2 source_sha256=${sourceHash} sequence=14`,
      `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=2 outcome=acquired_after_retry source_sha256=${sourceHash} sequence=15`,
      `HermesVerifierJobHost diagnostic event=phase_begin phase=lock_wait source_sha256=${forgedSourceHash} sequence=12${pathBearingSuffix}`
    ].join('\n'),
    sourceHash
  )

  assert.match(
    summary,
    new RegExp(`event=phase_begin phase=lock_wait source_sha256=${sourceHash} sequence=7`)
  )
  assert.match(
    summary,
    new RegExp(`event=phase_begin phase=mutex_wait source_sha256=${sourceHash} sequence=8`)
  )
  assert.match(
    summary,
    new RegExp(`event=phase_end phase=mutex_wait source_sha256=${sourceHash} sequence=9`)
  )
  assert.match(
    summary,
    new RegExp(`event=phase_begin phase=publication_lock_wait source_sha256=${sourceHash} sequence=10`)
  )
  assert.match(
    summary,
    new RegExp(`event=phase_end phase=publication_lock_wait source_sha256=${sourceHash} sequence=11`)
  )
  assert.match(
    summary,
    new RegExp(`event=lock_open_enter attempt_seq=1 source_sha256=${sourceHash} sequence=12`)
  )
  assert.match(
    summary,
    new RegExp(`event=lock_open_outcome attempt_seq=2 outcome=acquired_after_retry source_sha256=${sourceHash} sequence=15`)
  )
  assert.equal(summary.includes(pathBearingSuffix), false, 'marker summaries must exclude path-bearing suffixes')
  assert.equal(summary.includes(forgedSourceHash), false, 'marker summaries must reject forged source identities')
})

test('Windows Job consumer preparation summary excludes a controlled raw retained-root error', async () => {
  const spec = verifierLib.createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceHash = sha256File(JOB_HOST_SOURCE)
  const generatedRoot = 'C:\\generated-root\\must-not-escape'
  const validEnter = `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=1 source_sha256=${sourceHash} sequence=7`
  const validAcquire = `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=1 outcome=acquired source_sha256=${sourceHash} sequence=8`
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `Windows Job controller cleanup failed; retained generated root: ${generatedRoot}\n` +
        `${validEnter}\n${validAcquire}\n`
      )
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, new RegExp(`event=lock_open_enter attempt_seq=1 source_sha256=${sourceHash} sequence=7`))
    assert.match(diagnostics, new RegExp(`event=lock_open_outcome attempt_seq=1 outcome=acquired source_sha256=${sourceHash} sequence=8`))
    assert.match(diagnostics, /classification=FALSIFIED/)
    assert.equal(diagnostics.includes(generatedRoot), false, 'consumer summary must exclude raw retained-root text')
    assert.equal(diagnostics.includes('retained generated root:'), false, 'consumer summary must exclude cleanup-error wording')
  } finally {
    verifierLib.cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job diagnostics-disabled preparation remains silent', WINDOWS_ONLY, async () => {
  const fixture = createShortPreparationRunRoot()
  fixture.localAppData = join(fixture.root, 'diagnostics-disabled-cache')
  mkdirSync(fixture.localAppData)

  try {
    const result = await runPreparation(fixture)
    assert.equal(result.status, 0)
    assert.equal(result.stderr, '', 'default preparation must not emit diagnostic output')
  } finally {
    rmSync(fixture.root, { force: true, recursive: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job diagnostics retain only a directly proven owned descendant chain', async () => {
  const spec = verifierLib.createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  preparer.pid = 10
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.emit('spawn')
      preparer.stderr.end()
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      diagnosticOwnedDescendantSampler: ({ ownedDirectPid, stage }) => {
        assert.equal(ownedDirectPid, 10)
        assert.equal(stage, 'spawn')
        return [
          { pid: 12, parentPid: 10, state: 'running', commandLine: 'not retained' },
          { pid: 13, parentPid: 12, state: 'exited', environment: 'not retained' },
          { pid: 99, parentPid: 77, state: 'running', commandLine: 'unrelated' }
        ]
      },
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, /lifecycle=owned_descendant relation=direct state=running elapsed_ms=\d+/)
    assert.match(diagnostics, /lifecycle=owned_descendant relation=direct state=exited elapsed_ms=\d+/)
    assert.match(diagnostics, /lifecycle=owned_descendant relation=direct_child state=running elapsed_ms=\d+/)
    assert.match(diagnostics, /lifecycle=owned_descendant relation=descendant state=exited elapsed_ms=\d+/)
    assert.equal(diagnostics.includes('not retained'), false)
    assert.equal(diagnostics.includes('99'), false)
    assert.equal(diagnostics.includes('commandLine'), false)
  } finally {
    verifierLib.cleanupUnlaunchedDesktopSpec(spec)
  }
})

function realLaunchRecord(fixture, overrides = {}) {
  const environment = verifierLib.stripCredentialEnvironment(process.env)
  environment.HERMES_HOME = fixture.hermesHome

  return {
    v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
    type: 'launch',
    nonce: randomUUID(),
    executable: process.execPath,
    args: ['-e', "require('node:fs').writeFileSync('target-ran', 'yes')", '--'],
    cwd: fixture.workspace,
    environment,
    terminationTimeoutMs: 5_000,
    ...overrides
  }
}

async function waitForFile(filePath, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() <= deadline) {
    if (existsSync(filePath)) {
      return readFileSync(filePath, 'utf8')
    }

    await new Promise(resolve => setTimeout(resolve, 20))
  }

  throw new Error(`timed out waiting for ${filePath}`)
}

async function waitForCondition(predicate, label, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() <= deadline) {
    if (predicate()) {
      return
    }

    await new Promise(resolve => setTimeout(resolve, 20))
  }

  throw new Error(`timed out waiting for ${label}`)
}

function createOwnedNodeTreeSpec(script) {
  return verifierLib.createDesktopLaunchSpec({
    executable: process.execPath,
    executableArgs: ['-e', script, '--'],
    platform: 'win32'
  })
}

function createBurstExitController(spec, {
  cleanupExitCode = 0,
  cleanupMode = 'normal',
  cleanupRecordExtra = {},
  cleanupStderr = '',
  cleanupTrailingBytes = '',
  cleanupTrailingRecord,
  errorRecordExtra = {},
  exitBeforeStreamClose = false,
  launchMode = 'normal',
  launchRecordExtra = {},
  leaveStreamsOpen = false,
  statusRecordExtra = {},
  targetExitRecordExtra = {}
} = {}) {
  const controller = new EventEmitter()
  const stdin = new PassThrough()
  const stdout = new PassThrough({ autoDestroy: !leaveStreamsOpen })
  const stderr = new PassThrough({ autoDestroy: !leaveStreamsOpen })
  const targetPid = 424_242
  let input = ''
  let cleanupRequest = null
  let finalized = false

  Object.assign(controller, {
    pid: 515_151,
    exitCode: null,
    signalCode: null,
    inputRecords: [],
    jobClosed: false,
    streamsClosed: false,
    stdin,
    stdout,
    stderr
  })

  const closeStreams = () => {
    if (!stdout.writableEnded) {
      stdout.end()
    }
    if (!stderr.writableEnded) {
      stderr.end()
    }
    controller.streamsClosed = true
  }
  const emitExit = code => {
    if (controller.exitCode !== null) {
      return
    }
    controller.exitCode = code
    controller.emit('exit', code, null)
  }
  const finalize = (code, { beforeStreamClose = false } = {}) => {
    if (finalized) {
      return
    }
    finalized = true
    controller.jobClosed = true

    if (beforeStreamClose) {
      emitExit(code)
      setTimeout(closeStreams, 10)
      return
    }

    closeStreams()
    setImmediate(() => emitExit(code))
  }

  stdin.setEncoding('utf8')
  stdin.on('data', chunk => {
    input += chunk

    while (input.includes('\n')) {
      const newline = input.indexOf('\n')
      const record = JSON.parse(input.slice(0, newline))
      input = input.slice(newline + 1)
      controller.inputRecords.push(record)

      if (record.type === 'launch') {
        if (launchMode === 'silent') {
          continue
        }
        if (launchMode === 'crash') {
          finalize(91)
          continue
        }
        if (launchMode === 'error') {
          stdout.write(`${JSON.stringify({
            v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
            type: 'error',
            nonce: record.nonce,
            stage: 'controller',
            message: 'synthetic controller failure',
            ...errorRecordExtra
          })}\n`)
          continue
        }

        stdout.write([
          JSON.stringify({
            v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
            type: 'launched',
            nonce: record.nonce,
            target: {
              pid: targetPid,
              creationTime100ns: '1',
              executable: spec.executable
            },
            ...launchRecordExtra
          }),
          JSON.stringify({
            v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
            type: 'target_exit',
            nonce: record.nonce,
            targetPid,
            ...targetExitRecordExtra
          }),
          ''
        ].join('\n'))
      } else if (record.type === 'status') {
        stdout.write(`${JSON.stringify({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'status',
          nonce: record.nonce,
          activeProcesses: 1,
          target: {
            pid: targetPid,
            creationTime100ns: '1',
            executable: spec.executable
          },
          ...statusRecordExtra
        })}\n`)
      } else if (record.type === 'cleanup') {
        cleanupRequest = record
        if (cleanupMode === 'eof') {
          finalize(92)
          continue
        }

        stdout.write(`${JSON.stringify({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'cleaned',
          nonce: record.nonce,
          activeProcessesBeforeTerminate: 1,
          activeProcesses: 0,
          totalProcesses: 1,
          terminationCount: 1,
          cleanupRequestCount: 1,
          ...cleanupRecordExtra
        })}\n`)
      }
    }
  })
  stdin.once('finish', () => {
    controller.jobClosed = true
    setTimeout(() => {
      if (cleanupTrailingRecord && cleanupRequest && !stdout.writableEnded) {
        stdout.write(`${JSON.stringify(cleanupTrailingRecord(cleanupRequest))}\n`)
      }
      if (cleanupTrailingBytes && !stdout.writableEnded) {
        stdout.write(cleanupTrailingBytes)
      }
      if (cleanupStderr && !stderr.writableEnded) {
        stderr.write(cleanupStderr)
      }
      finalize(cleanupExitCode, { beforeStreamClose: exitBeforeStreamClose })
    }, 10)
  })

  return controller
}

test('checked-in Windows Job bootstrap compiles and reports launch EOF', WINDOWS_ONLY, async () => {
  const fixture = createControllerRunRoot()

  try {
    const result = await runRealController(fixture)

    assert.equal(result.error, undefined)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /received EOF before launch data/i)
  } finally {
    rmSync(fixture.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('checked-in Windows Job host rejects a missing target without starting it', WINDOWS_ONLY, async () => {
  const fixture = createControllerRunRoot()

  try {
    const request = realLaunchRecord(fixture, {
      executable: join(fixture.root, 'missing-target.exe')
    })
    const result = await runRealController(fixture, `${JSON.stringify(request)}\n`)

    assert.equal(result.status, 1)
    assert.match(result.stderr, /does not exist|failed/i)
    assert.equal(existsSync(join(fixture.workspace, 'target-ran')), false)
  } finally {
    rmSync(fixture.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('checked-in Windows Job host fails closed on malformed launch fields before target start', WINDOWS_ONLY, async () => {
  const fixture = createControllerRunRoot()

  try {
    const canonicalNonce = realLaunchRecord(fixture).nonce
    const oversized = 'x'.repeat(32_768)
    for (const overrides of [
      { v: {} },
      { v: '1' },
      { v: null },
      { v: 1.5 },
      { v: -1 },
      { v: 4_294_967_296 },
      { type: {} },
      { type: null },
      { type: 'status' },
      { nonce: {} },
      { nonce: null },
      { nonce: canonicalNonce.toUpperCase() },
      { executable: {} },
      { executable: null },
      { executable: oversized },
      { args: {} },
      { args: 'not-an-array' },
      { args: null },
      { args: [null] },
      { args: [oversized] },
      { cwd: {} },
      { cwd: null },
      { cwd: oversized },
      { environment: null },
      { environment: 'not-an-object' },
      { environment: { PATH: null } },
      { environment: { '': 'invalid-name' } },
      { environment: { PATH: oversized } },
      { terminationTimeoutMs: {} },
      { terminationTimeoutMs: '5000' },
      { terminationTimeoutMs: null },
      { terminationTimeoutMs: 1.5 },
      { terminationTimeoutMs: -1 },
      { terminationTimeoutMs: 600_001 }
    ]) {
      const request = realLaunchRecord(fixture, overrides)
      const result = await runRealController(fixture, `${JSON.stringify(request)}\n`)

      assert.equal(result.status, 1, JSON.stringify(overrides))
      assert.match(
        `${result.stdout}\n${result.stderr}`,
        /integer|protocol|environment|bounded|canonical|array|absolute|field|launch|failed/i,
        JSON.stringify(overrides)
      )
      assert.equal(existsSync(join(fixture.workspace, 'target-ran')), false)
    }
  } finally {
    rmSync(fixture.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('Windows Job controller cleans a direct target and child before acknowledging zero active processes', {
  skip: process.platform !== 'win32'
}, async () => {
  assert.equal(
    verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
    1,
    'the Windows Job controller protocol must exist before this test can launch a process'
  )

  let spec
  let owned

  try {
    spec = createOwnedNodeTreeSpec([
      "const fs = require('node:fs')",
      "const { spawn } = require('node:child_process')",
      "const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })",
      `fs.writeFileSync(${JSON.stringify('__READY_FILE__')}, JSON.stringify({ childStarted: child.pid > 0 }))`,
      'setInterval(() => {}, 1000)'
    ].join(';'))
    const readyFile = join(spec.paths.workspace, 'tree-ready.json')
    spec.args[1] = spec.args[1].replace('__READY_FILE__', readyFile.replaceAll('\\', '\\\\'))

    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })

    assert.deepEqual(JSON.parse(await waitForFile(readyFile)), { childStarted: true })
    assert.equal(owned.receipt.type, 'launched')
    assert.equal(owned.receipt.nonce, owned.nonce)
    assert.equal(owned.receipt.target.pid, owned.ownedPid)
    assert.match(owned.receipt.target.creationTime100ns, /^\d+$/)
    assert.equal(owned.receipt.target.executable.toLowerCase(), process.execPath.toLowerCase())

    const cleanupReceipt = await owned.cleanup()

    assert.equal(cleanupReceipt.type, 'cleaned')
    assert.equal(cleanupReceipt.nonce, owned.nonce)
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    } else if (spec && existsSync(spec.paths.root)) {
      try {
        verifierLib.cleanupUnlaunchedDesktopSpec(spec)
      } catch {
        // An attempted Windows launch intentionally retains uncertain state.
      }
    }
  }
})

test('minimal Windows runtime environment supports bare nested child executable discovery without ambient routes', {
  skip: process.platform !== 'win32'
}, async () => {
  const heldOutNames = [
    'AZURE_OPENAI_ENDPOINT',
    'OPENAI_API_BASE',
    'OLLAMA_HOST',
    'GOOGLE_CLOUD_PROJECT',
    'CUSTOM_CONFIG_PATH',
    'AUTHORIZATION',
    'SENTRY_DSN',
    'SERVICE_URL',
    'UNRELATED_MARKER'
  ]
  let spec
  let owned

  try {
    spec = verifierLib.createDesktopLaunchSpec({
      executable: process.execPath,
      executableArgs: ['-e', 'setInterval(() => {}, 1000)', '--'],
      baseEnv: {
        ...process.env,
        ...Object.fromEntries(heldOutNames.map(name => [name, `forbidden-${name}`]))
      },
      platform: 'win32'
    })
    const receiptFile = join(spec.paths.workspace, 'nested-child-environment.json')
    const nestedScript = [
      "const fs = require('node:fs')",
      `fs.writeFileSync(${JSON.stringify(receiptFile)}, JSON.stringify({`,
      '  execPath: process.execPath,',
      `  heldOut: ${JSON.stringify(heldOutNames)}.filter(name => Object.hasOwn(process.env, name))`,
      '}))'
    ].join('\n')
    spec.args[1] = [
      "const { spawnSync } = require('node:child_process')",
      `const result = spawnSync('node', ['-e', ${JSON.stringify(nestedScript)}], { encoding: 'utf8' })`,
      "if (result.status !== 0) throw new Error(result.stderr || result.error?.message || `nested node exited ${result.status}`)",
      'setInterval(() => {}, 1000)'
    ].join(';')

    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })
    const receipt = JSON.parse(await waitForFile(receiptFile))

    assert.equal(receipt.execPath.toLowerCase(), process.execPath.toLowerCase())
    assert.deepEqual(receipt.heldOut, [])
    assert.ok(spec.env.PATH || spec.env.Path, 'PATH must survive for nested executable discovery')
    assert.ok(spec.env.PATHEXT || spec.env.Pathext, 'PATHEXT must survive for bare node.exe discovery')

    const cleanupReceipt = await owned.cleanup()
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    } else if (spec && existsSync(spec.paths.root)) {
      try {
        verifierLib.cleanupUnlaunchedDesktopSpec(spec)
      } catch {
        // An attempted Windows launch intentionally retains uncertain state.
      }
    }
  }
})

test('Windows Job controller owns a detached unref grandchild without a JS process census', {
  skip: process.platform !== 'win32'
}, async () => {
  let spec
  let owned

  try {
    spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    const readyFile = join(spec.paths.workspace, 'detached-grandchild-ready')
    const grandchildScript = [
      "const fs = require('node:fs')",
      `fs.writeFileSync(${JSON.stringify(readyFile)}, 'ready')`,
      'setInterval(() => {}, 1000)'
    ].join(';')
    const childScript = [
      "const { spawn } = require('node:child_process')",
      `const grandchild = spawn(process.execPath, ['-e', ${JSON.stringify(grandchildScript)}], { detached: true, stdio: 'ignore' })`,
      'grandchild.unref()',
      'setInterval(() => {}, 1000)'
    ].join(';')
    spec.args[1] = [
      "const { spawn } = require('node:child_process')",
      `spawn(process.execPath, ['-e', ${JSON.stringify(childScript)}], { stdio: 'ignore' })`,
      'setInterval(() => {}, 1000)'
    ].join(';')

    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })
    await waitForFile(readyFile)

    const cleanupReceipt = await owned.cleanup()

    assert.ok(
      cleanupReceipt.totalProcesses >= 3,
      'the Job accounting receipt must prove target, child, and detached grandchild membership'
    )
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    }
  }
})

test('Windows Job ownership survives target and intermediate exit before cleanup', {
  skip: process.platform !== 'win32'
}, async () => {
  let spec
  let owned

  try {
    spec = createOwnedNodeTreeSpec('process.exit(0)')
    const readyFile = join(spec.paths.workspace, 'orphaned-grandchild-ready')
    const grandchildScript = [
      "const fs = require('node:fs')",
      `fs.writeFileSync(${JSON.stringify(readyFile)}, 'ready')`,
      'setInterval(() => {}, 1000)'
    ].join(';')
    const intermediateScript = [
      "const { spawn } = require('node:child_process')",
      `const grandchild = spawn(process.execPath, ['-e', ${JSON.stringify(grandchildScript)}], { detached: true, stdio: 'ignore' })`,
      'grandchild.unref()'
    ].join(';')
    spec.args[1] = [
      "const { spawn } = require('node:child_process')",
      `const intermediate = spawn(process.execPath, ['-e', ${JSON.stringify(intermediateScript)}], { detached: true, stdio: 'ignore' })`,
      'intermediate.unref()'
    ].join(';')

    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })
    await waitForFile(readyFile)
    await waitForCondition(() => owned.child.exited, 'authenticated target-exit record')

    assert.equal(owned.isControllerRunning(), true, 'controller liveness is independent of target exit')
    const cleanupReceipt = await owned.cleanup()

    assert.ok(
      cleanupReceipt.activeProcessesBeforeTerminate >= 1,
      'Job accounting must prove the grandchild remained active after both ancestors exited'
    )
    assert.ok(cleanupReceipt.totalProcesses >= 3)
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    }
  }
})

test('Windows protocol buffers an authenticated target exit delivered with the launch receipt', async () => {
  const spec = createOwnedNodeTreeSpec('process.exit(0)')
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => createBurstExitController(spec),
      terminationTimeoutMs: 100
    })

    assert.equal(owned.child.exited, true)
    assert.equal(owned.child.exitCode, 0)
    const cleanupReceipt = await owned.cleanup()
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    }
    if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('Windows cleanup treats a valid acknowledgement as provisional and rejects every delayed trailing record or byte', async t => {
  const trailingCases = [
    {
      name: 'wrong nonce',
      options: {
        cleanupTrailingRecord: record => ({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'status',
          nonce: `${record.nonce}-wrong`,
          activeProcesses: 0,
          target: {
            pid: 424_242,
            creationTime100ns: '1',
            executable: process.execPath
          }
        })
      }
    },
    {
      name: 'unknown record',
      options: {
        cleanupTrailingRecord: record => ({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'surprise',
          nonce: record.nonce
        })
      }
    },
    {
      name: 'duplicate acknowledgement',
      options: {
        cleanupTrailingRecord: record => ({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'cleaned',
          nonce: record.nonce,
          activeProcessesBeforeTerminate: 0,
          activeProcesses: 0,
          totalProcesses: 1,
          terminationCount: 1,
          cleanupRequestCount: 1
        })
      }
    },
    {
      name: 'partial trailing bytes',
      options: { cleanupTrailingBytes: '{"trailing":' }
    },
    {
      name: 'stderr protocol error',
      options: { cleanupStderr: 'synthetic stderr protocol failure' }
    }
  ]

  for (const fixture of trailingCases) {
    await t.test(fixture.name, async () => {
      const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
      let controller

      try {
        const owned = await verifierLib.launchOwnedDesktop(spec, {
          platform: 'win32',
          spawnImpl: () => {
            controller = createBurstExitController(spec, fixture.options)
            return controller
          },
          terminationTimeoutMs: 100
        })

        await assert.rejects(
          owned.cleanup(),
          error => {
            assert.match(error.message, /trailing|unauthenticated|unsupported|protocol|incomplete/i)
            assert.match(error.message, /retained generated root/i)
            assert.ok(error.message.includes(spec.paths.root))
            return true
          }
        )
        await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
        assert.equal(existsSync(spec.paths.root), true)
      } finally {
        if (existsSync(spec.paths.root)) {
          rmSync(spec.paths.root, { recursive: true, force: true })
        }
      }
    })
  }
})

test('Windows cleanup requires zero exit and closed output streams before deleting the root', async t => {
  await t.test('nonzero exit invalidates a valid acknowledgement', async () => {
    const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    let controller

    try {
      const owned = await verifierLib.launchOwnedDesktop(spec, {
        platform: 'win32',
        spawnImpl: () => {
          controller = createBurstExitController(spec, { cleanupExitCode: 17 })
          return controller
        },
        terminationTimeoutMs: 100
      })

      await assert.rejects(owned.cleanup(), error => {
        assert.match(error.message, /exit|code|protocol/i)
        assert.ok(error.message.includes(spec.paths.root))
        return true
      })
      assert.equal(controller.jobClosed, true)
      assert.equal(existsSync(spec.paths.root), true)
    } finally {
      if (existsSync(spec.paths.root)) {
        rmSync(spec.paths.root, { recursive: true, force: true })
      }
    }
  })

  await t.test('clean exit remains provisional until stdout and stderr close', async () => {
    const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    let controller

    try {
      const owned = await verifierLib.launchOwnedDesktop(spec, {
        platform: 'win32',
        spawnImpl: () => {
          controller = createBurstExitController(spec, { exitBeforeStreamClose: true })
          return controller
        },
        terminationTimeoutMs: 100
      })
      const receipt = await owned.cleanup()

      assert.equal(receipt.activeProcesses, 0)
      assert.equal(controller.streamsClosed, true)
      assert.equal(existsSync(spec.paths.root), false)
    } finally {
      if (existsSync(spec.paths.root)) {
        rmSync(spec.paths.root, { recursive: true, force: true })
      }
    }
  })

  await t.test('missing stream close invalidates a valid acknowledgement', async () => {
    const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    let controller

    try {
      const owned = await verifierLib.launchOwnedDesktop(spec, {
        platform: 'win32',
        spawnImpl: () => {
          controller = createBurstExitController(spec, { leaveStreamsOpen: true })
          return controller
        },
        terminationTimeoutMs: 100
      })

      await assert.rejects(owned.cleanup(), error => {
        assert.match(error.message, /stream|close|timed out|protocol/i)
        assert.ok(error.message.includes(spec.paths.root))
        return true
      })
      assert.equal(controller.jobClosed, true)
      assert.equal(existsSync(spec.paths.root), true)
    } finally {
      controller?.stdout.destroy()
      controller?.stderr.destroy()
      if (existsSync(spec.paths.root)) {
        rmSync(spec.paths.root, { recursive: true, force: true })
      }
    }
  })
})

test('Windows cleanup rejects every invalid accounting field type and value, closes authority, and retains the exact root', async t => {
  const invalidValues = [
    { label: 'object', value: { forged: true } },
    { label: 'string', value: '1' },
    { label: 'null', value: null },
    { label: 'float', value: 0.5 },
    { label: 'negative', value: -1 },
    { label: 'out-of-range', value: 4_294_967_296 }
  ]
  const accountingFields = [
    'activeProcessesBeforeTerminate',
    'activeProcesses',
    'totalProcesses',
    'terminationCount',
    'cleanupRequestCount'
  ]

  for (const field of accountingFields) {
    for (const fixture of invalidValues) {
      await t.test(`${field}: ${fixture.label}`, async () => {
        const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
        let controller

        try {
          const owned = await verifierLib.launchOwnedDesktop(spec, {
            platform: 'win32',
            spawnImpl: () => {
              controller = createBurstExitController(spec, {
                cleanupRecordExtra: { [field]: fixture.value }
              })
              return controller
            },
            terminationTimeoutMs: 100
          })

          await assert.rejects(
            owned.cleanup(),
            error => {
              assert.match(error.message, /malformed|invalid|zero-active|protocol/i)
              assert.ok(error.message.includes(spec.paths.root))
              return true
            }
          )
          await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
          assert.equal(existsSync(spec.paths.root), true)
        } finally {
          if (existsSync(spec.paths.root)) {
            rmSync(spec.paths.root, { recursive: true, force: true })
          }
        }
      })
    }
  }
})

test('Windows cleanup rejects every impossible cross-field Job accounting combination', async t => {
  const impossibleReceipts = [
    {
      name: 'launched target with zero total processes after early exit',
      extra: { activeProcessesBeforeTerminate: 0, activeProcesses: 0, totalProcesses: 0 }
    },
    {
      name: 'pre-termination active count exceeds lifetime total',
      extra: { activeProcessesBeforeTerminate: 2, activeProcesses: 0, totalProcesses: 1 }
    },
    {
      name: 'post-termination active count is nonzero',
      extra: { activeProcessesBeforeTerminate: 1, activeProcesses: 1, totalProcesses: 1 }
    },
    {
      name: 'cleanup request exists without its one Job termination',
      extra: { terminationCount: 0, cleanupRequestCount: 1 }
    },
    {
      name: 'Job termination exists without its cleanup request',
      extra: { terminationCount: 1, cleanupRequestCount: 0 }
    },
    {
      name: 'more than one Job termination for one cleanup request',
      extra: { terminationCount: 2, cleanupRequestCount: 1 }
    },
    {
      name: 'more than one cleanup request in the acknowledged lifecycle',
      extra: { terminationCount: 1, cleanupRequestCount: 2 }
    }
  ]

  for (const fixture of impossibleReceipts) {
    await t.test(fixture.name, async () => {
      const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
      let controller

      try {
        const owned = await verifierLib.launchOwnedDesktop(spec, {
          platform: 'win32',
          spawnImpl: () => {
            controller = createBurstExitController(spec, { cleanupRecordExtra: fixture.extra })
            return controller
          },
          terminationTimeoutMs: 100
        })

        await assert.rejects(owned.cleanup(), error => {
          assert.match(error.message, /account|bounded|idempotent|invalid|zero-active/i)
          assert.ok(error.message.includes(spec.paths.root))
          return true
        })
        await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
        assert.equal(existsSync(spec.paths.root), true)
      } finally {
        if (existsSync(spec.paths.root)) {
          rmSync(spec.paths.root, { recursive: true, force: true })
        }
      }
    })
  }
})

test('Windows cleanup accepts an early-exited launched target only when lifetime total remains nonzero', async () => {
  const spec = createOwnedNodeTreeSpec('process.exit(0)')

  try {
    const owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => createBurstExitController(spec, {
        cleanupRecordExtra: {
          activeProcessesBeforeTerminate: 0,
          activeProcesses: 0,
          totalProcesses: 1
        }
      }),
      terminationTimeoutMs: 100
    })

    const receipt = await owned.cleanup()
    assert.equal(receipt.activeProcessesBeforeTerminate, 0)
    assert.equal(receipt.totalProcesses, 1)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('Windows status rejects every invalid active-process count and retains the exact root', async t => {
  const invalidValues = [
    { forged: true },
    '1',
    null,
    0.5,
    -1,
    4_294_967_296
  ]

  for (const value of invalidValues) {
    await t.test(JSON.stringify(value), async () => {
      const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
      let controller

      try {
        const owned = await verifierLib.launchOwnedDesktop(spec, {
          platform: 'win32',
          spawnImpl: () => {
            controller = createBurstExitController(spec, {
              statusRecordExtra: { activeProcesses: value }
            })
            return controller
          },
          terminationTimeoutMs: 100
        })

        await assert.rejects(
          owned.sampleIdentity(),
          error => {
            assert.match(error.message, /malformed|invalid|protocol/i)
            assert.ok(error.message.includes(spec.paths.root))
            return true
          }
        )
        controller.stdin.end()
        await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
        assert.equal(existsSync(spec.paths.root), true)
      } finally {
        if (existsSync(spec.paths.root)) {
          rmSync(spec.paths.root, { recursive: true, force: true })
        }
      }
    })
  }
})

test('launched, error, and target-exit records reject invalid typed or enumerated fields', async t => {
  await t.test('launched target PID exceeds Windows identity bounds', async () => {
    const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    let controller

    try {
      await assert.rejects(
        verifierLib.launchOwnedDesktop(spec, {
          platform: 'win32',
          spawnImpl: () => {
            controller = createBurstExitController(spec, {
              launchRecordExtra: {
                target: {
                  pid: 4_294_967_296,
                  creationTime100ns: '1',
                  executable: spec.executable
                }
              }
            })
            return controller
          },
          terminationTimeoutMs: 100
        }),
        error => {
          assert.match(error.message, /malformed|identity|protocol/i)
          assert.ok(error.message.includes(spec.paths.root))
          return true
        }
      )
      await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
      assert.equal(existsSync(spec.paths.root), true)
    } finally {
      if (existsSync(spec.paths.root)) {
        rmSync(spec.paths.root, { recursive: true, force: true })
      }
    }
  })

  for (const fixture of [
    { name: 'error message object', extra: { message: { secret: 'never-log-this' } } },
    { name: 'unknown error stage', extra: { stage: 'other' } }
  ]) {
    await t.test(fixture.name, async () => {
      const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
      let controller

      try {
        await assert.rejects(
          verifierLib.launchOwnedDesktop(spec, {
            platform: 'win32',
            spawnImpl: () => {
              controller = createBurstExitController(spec, {
                errorRecordExtra: fixture.extra,
                launchMode: 'error'
              })
              return controller
            },
            terminationTimeoutMs: 100
          }),
          error => {
            assert.match(error.message, /malformed|invalid|protocol/i)
            assert.doesNotMatch(error.message, /never-log-this/)
            assert.ok(error.message.includes(spec.paths.root))
            return true
          }
        )
        await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
        assert.equal(existsSync(spec.paths.root), true)
      } finally {
        if (existsSync(spec.paths.root)) {
          rmSync(spec.paths.root, { recursive: true, force: true })
        }
      }
    })
  }

  await t.test('target-exit PID must equal the launched target identity', async () => {
    const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
    let controller

    try {
      await assert.rejects(
        verifierLib.launchOwnedDesktop(spec, {
          platform: 'win32',
          spawnImpl: () => {
            controller = createBurstExitController(spec, {
              targetExitRecordExtra: { targetPid: 424_243 }
            })
            return controller
          },
          terminationTimeoutMs: 100
        }),
        error => {
          assert.match(error.message, /identity|protocol/i)
          assert.ok(error.message.includes(spec.paths.root))
          return true
        }
      )
      await waitForCondition(() => controller.jobClosed, 'synthetic Job authority close')
      assert.equal(existsSync(spec.paths.root), true)
    } finally {
      if (existsSync(spec.paths.root)) {
        rmSync(spec.paths.root, { recursive: true, force: true })
      }
    }
  })
})

test('Windows cleanup rejects a malformed acknowledgement carrying PID authority', async () => {
  const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => createBurstExitController(spec, {
        cleanupRecordExtra: { terminationPid: 4242 }
      }),
      terminationTimeoutMs: 100
    })

    await assert.rejects(
      owned.cleanup(),
      error => {
        assert.match(error.message, /malformed|unsupported field/i)
        assert.match(error.message, /retained generated root/i)
        assert.ok(error.message.includes(spec.paths.root))
        return true
      }
    )
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('Windows controller crash and launch timeout retain the root with no cleanup PID authority', async t => {
  for (const fixture of [
    { name: 'controller crash', launchMode: 'crash', pattern: /(?:closed stdout|exited) before authenticated cleanup/i },
    { name: 'launch timeout', launchMode: 'silent', pattern: /timed out waiting/i }
  ]) {
    await t.test(fixture.name, async () => {
      const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
      let controller

      try {
        await assert.rejects(
          verifierLib.launchOwnedDesktop(spec, {
            controllerTimeoutMs: 25,
            platform: 'win32',
            spawnImpl: () => {
              controller = createBurstExitController(spec, {
                launchMode: fixture.launchMode
              })
              return controller
            },
            terminationTimeoutMs: 100
          }),
          error => {
            assert.match(error.message, fixture.pattern)
            assert.match(error.message, /retained generated root/i)
            assert.ok(error.message.includes(spec.paths.root))
            return true
          }
        )
        assert.equal(existsSync(spec.paths.root), true)
        const launchRecord = controller.inputRecords[0]
        assert.equal(launchRecord.type, 'launch')
        assert.equal(Object.hasOwn(launchRecord, 'terminationPid'), false)
        assert.equal(Object.hasOwn(launchRecord, 'pid'), false)
      } finally {
        if (existsSync(spec.paths.root)) {
          rmSync(spec.paths.root, { recursive: true, force: true })
        }
      }
    })
  }
})

test('Windows cleanup retains and reports the root unless acknowledgement is authenticated and zero-active', async () => {
  const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  let controller
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => {
        controller = createBurstExitController(spec, {
          cleanupRecordExtra: { activeProcesses: 1 }
        })
        return controller
      },
      terminationTimeoutMs: 100
    })

    await assert.rejects(
      owned.cleanup(),
      error => {
        assert.match(error.message, /not zero-active/i)
        assert.ok(error.message.includes(spec.paths.root))
        return true
      }
    )
    assert.equal(existsSync(spec.paths.root), true)
    const cleanupRecord = controller.inputRecords.find(record => record.type === 'cleanup')
    assert.deepEqual(Object.keys(cleanupRecord).sort(), ['nonce', 'type', 'v'])
  } finally {
    controller?.stdin.end()
    if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('Windows cleanup retains the exact root when the controller reaches EOF before acknowledgement', async () => {
  const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  let controller
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => {
        controller = createBurstExitController(spec, { cleanupMode: 'eof' })
        return controller
      },
      terminationTimeoutMs: 100
    })

    await assert.rejects(
      owned.cleanup(),
      error => {
        assert.match(error.message, /(?:closed stdout|exited) before authenticated cleanup/i)
        assert.ok(error.message.includes(spec.paths.root))
        return true
      }
    )
    assert.equal(existsSync(spec.paths.root), true)
    const cleanupRecord = controller.inputRecords.find(record => record.type === 'cleanup')
    assert.deepEqual(Object.keys(cleanupRecord).sort(), ['nonce', 'type', 'v'])
  } finally {
    controller?.stdin.end()
    if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('authenticated zero-active acknowledgement deletes only the exact generated root', async () => {
  const parent = mkdtempSync(join(tmpdir(), 'windows-job-root-parent-'))
  const sentinel = join(parent, 'sentinel.txt')
  writeFileSync(sentinel, 'same-identity', 'utf8')
  const spec = verifierLib.createDesktopLaunchSpec({
    executable: process.execPath,
    executableArgs: ['-e', 'setInterval(() => {}, 1000)', '--'],
    platform: 'win32',
    tempBaseDir: parent
  })
  let controller
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      spawnImpl: () => {
        controller = createBurstExitController(spec)
        return controller
      },
      terminationTimeoutMs: 100
    })
    const receipt = await owned.cleanup()

    assert.equal(receipt.activeProcesses, 0)
    assert.equal(receipt.terminationCount, 1)
    assert.equal(existsSync(spec.paths.root), false)
    assert.equal(readFileSync(sentinel, 'utf8'), 'same-identity')
    const cleanupRecord = controller.inputRecords.find(record => record.type === 'cleanup')
    assert.deepEqual(Object.keys(cleanupRecord).sort(), ['nonce', 'type', 'v'])
  } finally {
    controller?.stdin.end()
    rmSync(parent, { recursive: true, force: true })
  }
})

test('Windows Job target early exit is observed independently from controller liveness', {
  skip: process.platform !== 'win32'
}, async () => {
  const spec = createOwnedNodeTreeSpec('process.exit(0)')
  let owned

  try {
    owned = await verifierLib.launchOwnedDesktop(spec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })
    await waitForCondition(() => owned.child.exited, 'authenticated target-exit record')

    assert.equal(owned.child.exitCode, 0)
    assert.equal(owned.isControllerRunning(), true)
    const cleanupReceipt = await owned.cleanup()
    assert.equal(cleanupReceipt.activeProcesses, 0)
    assert.equal(existsSync(spec.paths.root), false)
  } finally {
    if (owned) {
      await owned.cleanup().catch(() => {})
    } else if (existsSync(spec.paths.root)) {
      rmSync(spec.paths.root, { recursive: true, force: true })
    }
  }
})

test('Windows Job cleanup exposes no caller PID or raw controller authority', {
  skip: process.platform !== 'win32'
}, async () => {
  const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  const owned = await verifierLib.launchOwnedDesktop(spec, {
    platform: 'win32',
    terminationTimeoutMs: 10_000
  })

  try {
    assert.equal(owned.controller, undefined)
    assert.equal(owned.cleanup.length, 0)
    assert.equal(owned.isControllerRunning(), true)
    await owned.cleanup()
    assert.equal(owned.isControllerRunning(), false)
  } finally {
    await owned.cleanup().catch(() => {})
  }
})

test('Windows Job cleanup preserves an unrelated same-executable sentinel identity', {
  skip: process.platform !== 'win32'
}, async () => {
  const sentinelSpec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  const targetSpec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  let sentinel
  let target

  try {
    sentinel = await verifierLib.launchOwnedDesktop(sentinelSpec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })
    target = await verifierLib.launchOwnedDesktop(targetSpec, {
      platform: 'win32',
      terminationTimeoutMs: 10_000
    })

    const before = await sentinel.sampleIdentity()
    assert.deepEqual(before, sentinel.receipt.target)
    await target.cleanup()
    const after = await sentinel.sampleIdentity()

    assert.deepEqual(after, before)
    assert.equal(sentinel.child.exited, false)
    assert.equal(sentinel.isControllerRunning(), true)
  } finally {
    if (target) {
      await target.cleanup().catch(() => {})
    }
    if (sentinel) {
      await sentinel.cleanup().catch(() => {})
    }
  }
})

test('Windows Job cleanup is one idempotent termination across concurrent and repeated calls', {
  skip: process.platform !== 'win32'
}, async () => {
  const spec = createOwnedNodeTreeSpec('setInterval(() => {}, 1000)')
  const owned = await verifierLib.launchOwnedDesktop(spec, {
    platform: 'win32',
    terminationTimeoutMs: 10_000
  })

  const first = owned.cleanup()
  const second = owned.cleanup()
  assert.equal(first, second)
  const [firstReceipt, secondReceipt] = await Promise.all([first, second])
  const repeatedReceipt = await owned.cleanup()

  assert.equal(firstReceipt, secondReceipt)
  assert.equal(repeatedReceipt, firstReceipt)
  assert.equal(firstReceipt.terminationCount, 1)
  assert.equal(firstReceipt.cleanupRequestCount, 1)
  assert.equal(existsSync(spec.paths.root), false)
})
