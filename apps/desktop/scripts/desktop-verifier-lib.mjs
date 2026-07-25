import { randomUUID } from 'node:crypto'
import { spawn as nodeSpawn, spawnSync as nodeSpawnSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const LOOPBACK_ADDRESS = '127.0.0.1'
const DESKTOP_ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const REPO_ROOT = resolve(DESKTOP_ROOT, '..', '..')
const OWNED_SPEC = Symbol('desktop-verifier-owned-spec')
const DEFAULT_PORT_TIMEOUT_MS = 30_000
const DEFAULT_PAGE_TIMEOUT_MS = 30_000
const DEFAULT_POLL_INTERVAL_MS = 100
const DEFAULT_TERMINATION_TIMEOUT_MS = 5_000

const CREDENTIAL_ENV_SUFFIXES = [
  '_API_KEY',
  '_BASE_URL',
  '_TOKEN',
  '_SECRET',
  '_PASSWORD',
  '_CREDENTIALS',
  '_CREDENTIAL_FILE',
  '_CREDENTIALS_FILE',
  '_ACCESS_KEY',
  '_PRIVATE_KEY',
  '_PROFILE',
  '_PROFILE_NAME',
  '_OAUTH_TOKEN'
]

const CREDENTIAL_ENV_NAMES = new Set([
  'ACCESS_KEY',
  'API_KEY',
  'AWS_ACCESS_KEY_ID',
  'AWS_CONFIG_FILE',
  'AWS_DEFAULT_PROFILE',
  'AWS_PROFILE',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_SHARED_CREDENTIALS_FILE',
  'BASE_URL',
  'CLOUDSDK_CONFIG',
  'CREDENTIALS',
  'GOOGLE_APPLICATION_CREDENTIALS',
  'HERMES_CONFIG',
  'HERMES_DESKTOP_CONNECTION_MODE',
  'HERMES_DESKTOP_REMOTE_TOKEN',
  'HERMES_DESKTOP_REMOTE_URL',
  'HERMES_PROFILE',
  'HERMES_PROFILE_NAME',
  'HERMES_ENV',
  'PASSWORD',
  'PRIVATE_KEY',
  'PROFILE',
  'SECRET'
])
const nodeKill = process.kill.bind(process)

function isPositivePid(pid) {
  return Number.isSafeInteger(pid) && pid > 0
}

function assertOwnedSpec(spec) {
  const ownership = spec?.[OWNED_SPEC]

  if (!ownership || ownership.root !== spec.paths?.root) {
    throw new Error('desktop verifier cleanup requires a repository-owned launch spec')
  }

  return ownership
}

function childHasExited(child) {
  return child.exitCode !== null || child.signalCode !== null
}

function childExitDescription(child) {
  if (child.exitCode !== null) {
    return `exit code ${child.exitCode}`
  }

  if (child.signalCode !== null) {
    return `signal ${child.signalCode}`
  }

  return 'an unknown exit state'
}

function throwIfAborted(signal) {
  if (!signal?.aborted) {
    return
  }

  if (signal.reason instanceof Error) {
    throw signal.reason
  }

  throw new Error(signal.reason ? String(signal.reason) : 'desktop verification interrupted')
}

function sleep(ms, signal) {
  throwIfAborted(signal)

  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)

      try {
        throwIfAborted(signal)
      } catch (error) {
        reject(error)
      }
    }

    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function waitForChildExit(child, timeoutMs, label = 'owned desktop child') {
  if (childHasExited(child)) {
    return Promise.resolve()
  }

  return new Promise((resolve, reject) => {
    const cleanupListeners = () => {
      clearTimeout(timer)
      child.removeListener('exit', onExit)
    }
    const onExit = () => {
      cleanupListeners()
      resolve()
    }
    const timer = setTimeout(() => {
      cleanupListeners()
      reject(new Error(`${label} PID ${child.pid} did not exit within ${timeoutMs}ms`))
    }, timeoutMs)

    child.once('exit', onExit)
  })
}

export function isCredentialEnvVar(name) {
  const normalized = String(name).toUpperCase()

  return CREDENTIAL_ENV_NAMES.has(normalized) ||
    normalized.startsWith('HERMES_DESKTOP_REMOTE_') ||
    CREDENTIAL_ENV_SUFFIXES.some(suffix => normalized.endsWith(suffix))
}

export function stripCredentialEnvironment(baseEnv) {
  const env = {}

  for (const [name, value] of Object.entries(baseEnv ?? {})) {
    if (value !== undefined && value !== null && !isCredentialEnvVar(name)) {
      env[name] = String(value)
    }
  }

  return env
}

export function assertExistingDesktopInspectionAllowed(baseEnv = process.env) {
  if (baseEnv?.HERMES_DESKTOP_ALLOW_EXISTING !== '1') {
    throw new Error(
      'test:desktop:existing is operator deployment inspection only, never verification; ' +
      'set HERMES_DESKTOP_ALLOW_EXISTING=1 to acknowledge live-profile use'
    )
  }
}

function isPathInside(root, candidate) {
  const pathFromRoot = relative(root, candidate)

  return pathFromRoot !== '' &&
    !pathFromRoot.startsWith('..') &&
    !isAbsolute(pathFromRoot)
}

export function assertDesktopExecutableProvenance(executable, {
  repoRoot = REPO_ROOT,
  realpathImpl = realpathSync
} = {}) {
  if (typeof executable !== 'string' || executable.trim() === '') {
    throw new Error('desktop verifier requires a non-empty executable path')
  }

  let realRepoRoot
  let realExecutable

  try {
    realRepoRoot = realpathImpl(resolve(repoRoot))
    realExecutable = realpathImpl(resolve(executable))
  } catch (error) {
    throw new Error(`Desktop executable provenance could not be resolved: ${executable}`, {
      cause: error
    })
  }

  if (!statSync(realExecutable).isFile()) {
    throw new Error(`Desktop executable is not a file: ${realExecutable}`)
  }

  const outputRoots = [
    resolve(realRepoRoot, 'apps', 'desktop', 'dist'),
    resolve(realRepoRoot, 'apps', 'desktop', 'release')
  ]

  if (!outputRoots.some(root => isPathInside(root, realExecutable))) {
    throw new Error(
      `Desktop executable is outside the current Git worktree Desktop build output roots: ` +
      realExecutable
    )
  }

  return realExecutable
}

export function createDesktopLaunchSpec({
  executable,
  executableArgs = [],
  fakeBoot = false,
  baseEnv = process.env,
  tempBaseDir = tmpdir(),
  platform = process.platform
} = {}) {
  if (typeof executable !== 'string' || executable.trim() === '') {
    throw new Error('desktop verifier requires a non-empty executable path')
  }

  if (!Array.isArray(executableArgs) ||
      executableArgs.some(argument => typeof argument !== 'string')) {
    throw new Error('desktop verifier executable args must be an array of strings')
  }

  const root = mkdtempSync(join(tempBaseDir, 'hermes-desktop-verifier-'))
  const paths = {
    root,
    userDataDir: join(root, 'electron-user-data'),
    hermesHome: join(root, 'hermes-home'),
    workspace: join(root, 'workspace')
  }

  try {
    mkdirSync(paths.userDataDir)
    mkdirSync(paths.hermesHome)
    mkdirSync(paths.workspace)
  } catch (error) {
    rmSync(root, { recursive: true, force: true })
    throw error
  }

  const env = stripCredentialEnvironment(baseEnv)

  env.HERMES_DESKTOP_USER_DATA_DIR = paths.userDataDir
  env.HERMES_HOME = paths.hermesHome
  env.HERMES_DESKTOP_APP_NAME = `HermesDesktopVerifier-${randomUUID()}`
  env.HERMES_DESKTOP_CWD = paths.workspace
  env.HERMES_DESKTOP_IGNORE_EXISTING = '1'

  delete env.HERMES_DESKTOP_DEV_SERVER
  delete env.HERMES_DESKTOP_HERMES
  delete env.HERMES_DESKTOP_HERMES_ROOT

  if (fakeBoot) {
    env.HERMES_DESKTOP_BOOT_FAKE = '1'
    env.HERMES_DESKTOP_BOOT_FAKE_STEP_MS = '120'
    delete env.HERMES_DESKTOP_BOOT_FAKE_ERROR
  } else {
    delete env.HERMES_DESKTOP_BOOT_FAKE
    delete env.HERMES_DESKTOP_BOOT_FAKE_ERROR
    delete env.HERMES_DESKTOP_BOOT_FAKE_STEP_MS
  }

  const args = [
    ...executableArgs,
    `--user-data-dir=${paths.userDataDir}`,
    `--remote-debugging-address=${LOOPBACK_ADDRESS}`,
    '--remote-debugging-port=0'
  ]
  const spawnOptions = {
    cwd: paths.workspace,
    env,
    stdio: ['ignore', 'inherit', 'inherit']
  }

  if (platform !== 'win32') {
    spawnOptions.detached = true
  }

  const spec = {
    executable,
    args,
    env,
    paths,
    spawnOptions
  }

  Object.defineProperty(spec, OWNED_SPEC, {
    value: {
      launched: false,
      removed: false,
      root
    }
  })

  return spec
}

function removeGeneratedRoot(spec) {
  const ownership = assertOwnedSpec(spec)

  if (ownership.removed) {
    return
  }

  rmSync(ownership.root, {
    recursive: true,
    force: false,
    maxRetries: 10,
    retryDelay: 50
  })
  ownership.removed = true
}

export function cleanupUnlaunchedDesktopSpec(spec) {
  const ownership = assertOwnedSpec(spec)

  if (ownership.launched) {
    throw new Error('cannot use unlaunched cleanup after the owned desktop child started')
  }

  removeGeneratedRoot(spec)
}

export function parseDevToolsActivePort(contents) {
  const [portLine, browserWebSocketPath] = String(contents).trim().split(/\r?\n/, 2)

  if (!/^\d+$/.test(portLine ?? '')) {
    throw new Error('invalid DevToolsActivePort: first line is not a numeric port')
  }

  const port = Number.parseInt(portLine, 10)

  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new Error('invalid DevToolsActivePort: port must be between 1 and 65535')
  }

  if (!browserWebSocketPath?.startsWith('/devtools/browser/')) {
    throw new Error('invalid DevToolsActivePort: browser endpoint is not a local path')
  }

  return {
    port,
    browserWebSocketPath,
    address: LOOPBACK_ADDRESS
  }
}

export async function waitForDevToolsActivePort({
  userDataDir,
  child,
  timeoutMs = DEFAULT_PORT_TIMEOUT_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  signal
}) {
  const portFile = join(userDataDir, 'DevToolsActivePort')
  const deadline = Date.now() + timeoutMs
  let lastParseError = null

  while (Date.now() <= deadline) {
    throwIfAborted(signal)

    if (childHasExited(child)) {
      throw new Error(
        `owned desktop child exited with ${childExitDescription(child)} before DevToolsActivePort was ready`
      )
    }

    if (existsSync(portFile)) {
      try {
        return parseDevToolsActivePort(readFileSync(portFile, 'utf8'))
      } catch (error) {
        lastParseError = error
      }
    }

    await sleep(Math.min(pollIntervalMs, Math.max(1, deadline - Date.now())), signal)
  }

  const detail = lastParseError ? `; last read failed: ${lastParseError.message}` : ''
  throw new Error(`timed out waiting for this run's DevToolsActivePort at ${portFile}${detail}`)
}

async function fetchJsonWithTimeout(fetchImpl, url, timeoutMs, signal) {
  const controller = new AbortController()
  const onAbort = () => controller.abort(signal.reason)
  const timer = setTimeout(
    () => controller.abort(new Error(`CDP request timed out after ${timeoutMs}ms`)),
    timeoutMs
  )

  signal?.addEventListener('abort', onAbort, { once: true })

  try {
    const response = await fetchImpl(url, { signal: controller.signal })

    if (!response.ok) {
      throw new Error(`CDP endpoint returned HTTP ${response.status}`)
    }

    return await response.json()
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

export async function waitForCdpPage({
  port,
  child,
  timeoutMs = DEFAULT_PAGE_TIMEOUT_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  fetchImpl = fetch,
  signal
}) {
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535) {
    throw new Error('CDP page discovery requires a nonzero loopback port')
  }

  const endpoint = `http://${LOOPBACK_ADDRESS}:${port}/json/list`
  const deadline = Date.now() + timeoutMs
  let lastError = null

  while (Date.now() <= deadline) {
    throwIfAborted(signal)

    if (childHasExited(child)) {
      throw new Error(
        `owned desktop child exited with ${childExitDescription(child)} before a CDP page was ready`
      )
    }

    try {
      const targets = await fetchJsonWithTimeout(
        fetchImpl,
        endpoint,
        Math.min(1_000, Math.max(1, deadline - Date.now())),
        signal
      )
      const page = Array.isArray(targets)
        ? targets.find(target =>
          target?.type === 'page' &&
          typeof target.webSocketDebuggerUrl === 'string'
        )
        : null

      if (page) {
        return page
      }

      lastError = new Error('CDP target list did not contain a page')
    } catch (error) {
      lastError = error
    }

    await sleep(Math.min(pollIntervalMs, Math.max(1, deadline - Date.now())), signal)
  }

  const detail = lastError ? `; last probe failed: ${lastError.message}` : ''
  throw new Error(`timed out waiting for a CDP page on ${endpoint}${detail}`)
}

export function buildWindowsCleanupPlan(ownedPid, requestedPid = ownedPid) {
  if (!isPositivePid(ownedPid)) {
    throw new Error('Windows desktop cleanup requires a valid owned PID')
  }

  if (!isPositivePid(requestedPid)) {
    throw new Error('Windows desktop cleanup rejected an invalid requested PID')
  }

  if (requestedPid !== ownedPid) {
    throw new Error(`Windows desktop cleanup rejected unowned PID ${requestedPid}`)
  }

  return {
    command: 'taskkill.exe',
    args: ['/PID', String(ownedPid), '/T', '/F']
  }
}

export function discoverWindowsDescendantPids(ownedPid, {
  spawnSyncImpl = nodeSpawnSync,
  timeoutMs = DEFAULT_TERMINATION_TIMEOUT_MS
} = {}) {
  if (!isPositivePid(ownedPid)) {
    throw new Error('Windows descendant discovery requires a valid owned PID')
  }

  const script = [
    'Get-CimInstance Win32_Process',
    'Select-Object ProcessId,ParentProcessId',
    'ConvertTo-Json -Compress'
  ].join(' | ')
  const result = spawnSyncImpl(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-Command', script],
    {
      encoding: 'utf8',
      timeout: timeoutMs,
      windowsHide: true
    }
  )

  if (result.error) {
    throw new Error(`owned Windows descendant discovery failed: ${result.error.message}`)
  }

  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || '').trim()
    throw new Error(
      `owned Windows descendant discovery exited ${result.status}` +
      (detail ? `: ${detail}` : '')
    )
  }

  let records

  try {
    const parsed = JSON.parse(String(result.stdout || '[]'))
    records = Array.isArray(parsed) ? parsed : parsed ? [parsed] : []
  } catch (error) {
    throw new Error('owned Windows descendant discovery returned invalid JSON', { cause: error })
  }

  const childrenByParent = new Map()

  for (const record of records) {
    const pid = Number(record?.ProcessId)
    const parentPid = Number(record?.ParentProcessId)

    if (!isPositivePid(pid) || !isPositivePid(parentPid) || pid === ownedPid) {
      continue
    }

    const children = childrenByParent.get(parentPid) ?? []
    children.push(pid)
    childrenByParent.set(parentPid, children)
  }

  const descendants = []
  const visited = new Set([ownedPid])
  const pending = [ownedPid]

  while (pending.length) {
    const parentPid = pending.shift()

    for (const pid of childrenByParent.get(parentPid) ?? []) {
      if (visited.has(pid)) {
        continue
      }

      visited.add(pid)
      descendants.push(pid)
      pending.push(pid)
    }
  }

  return descendants
}

async function terminateOwnedChild({
  child,
  discoverWindowsDescendantPidsImpl,
  killImpl,
  ownedPid,
  platform,
  spawnSyncImpl,
  terminationTimeoutMs
}) {
  if (platform === 'win32') {
    if (childHasExited(child)) {
      const discover = discoverWindowsDescendantPidsImpl ??
        (pid => discoverWindowsDescendantPids(pid, {
          spawnSyncImpl,
          timeoutMs: terminationTimeoutMs
        }))
      const descendants = await discover(ownedPid)

      for (const descendantPid of [...descendants].reverse()) {
        if (!isPositivePid(descendantPid) || descendantPid === ownedPid) {
          throw new Error(`Windows cleanup rejected invalid descendant PID ${descendantPid}`)
        }

        const plan = buildWindowsCleanupPlan(descendantPid, descendantPid)
        spawnSyncImpl(plan.command, plan.args, {
          encoding: 'utf8',
          timeout: terminationTimeoutMs,
          windowsHide: true
        })
      }

      const remaining = await discover(ownedPid)

      if (remaining.length) {
        throw new Error(
          `owned Windows cleanup left descendant PIDs: ${remaining.join(', ')}`
        )
      }

      return
    }

    const plan = buildWindowsCleanupPlan(ownedPid, child.pid)
    const result = spawnSyncImpl(plan.command, plan.args, {
      encoding: 'utf8',
      timeout: terminationTimeoutMs,
      windowsHide: true
    })

    if (result.error) {
      throw new Error(`owned Windows cleanup failed for PID ${ownedPid}: ${result.error.message}`)
    }

    if (result.status !== 0) {
      const detail = String(result.stderr || result.stdout || '').trim()

      try {
        await waitForChildExit(child, terminationTimeoutMs)
      } catch (error) {
        throw new Error(
          `owned Windows cleanup failed for PID ${ownedPid} with status ${result.status}` +
          (detail ? `: ${detail}` : ''),
          { cause: error }
        )
      }

      return
    }

    await waitForChildExit(child, terminationTimeoutMs)

    return
  }

  await terminateOwnedProcessGroup({
    child,
    killImpl,
    ownedPid,
    terminationTimeoutMs
  })
}

async function terminateOwnedProcessGroup({
  child,
  killImpl,
  ownedPid,
  terminationTimeoutMs
}) {
  const groupId = -ownedPid
  const groupExists = () => {
    try {
      killImpl(groupId, 0)

      return true
    } catch (error) {
      if (error?.code === 'ESRCH') {
        return false
      }

      throw error
    }
  }
  const waitForGroupExit = async () => {
    const deadline = Date.now() + terminationTimeoutMs

    while (Date.now() <= deadline) {
      if (!groupExists()) {
        return
      }

      await sleep(Math.min(25, Math.max(1, deadline - Date.now())))
    }

    throw new Error(`owned POSIX process group ${ownedPid} did not exit within ${terminationTimeoutMs}ms`)
  }
  const signalGroup = signalName => {
    try {
      killImpl(groupId, signalName)
    } catch (error) {
      if (error?.code !== 'ESRCH') {
        throw error
      }
    }
  }

  signalGroup('SIGTERM')

  try {
    await Promise.all([
      waitForChildExit(child, terminationTimeoutMs),
      waitForGroupExit()
    ])

    return
  } catch (gracefulError) {
    signalGroup('SIGKILL')

    try {
      await Promise.all([
        waitForChildExit(child, terminationTimeoutMs),
        waitForGroupExit()
      ])
    } catch (forcedError) {
      throw new AggregateError(
        [gracefulError, forcedError],
        `owned POSIX cleanup failed for process group ${ownedPid}`
      )
    }
  }
}

function createOwnedCleanup({
  spec,
  child,
  discoverWindowsDescendantPidsImpl,
  killImpl,
  ownedPid,
  platform,
  spawnSyncImpl,
  terminationTimeoutMs
}) {
  let cleanupPromise = null

  return () => {
    if (!cleanupPromise) {
      cleanupPromise = (async () => {
        await terminateOwnedChild({
          child,
          discoverWindowsDescendantPidsImpl,
          killImpl,
          ownedPid,
          platform,
          spawnSyncImpl,
          terminationTimeoutMs
        })

        if (!childHasExited(child)) {
          throw new Error(`owned desktop child PID ${ownedPid} exit was not confirmed`)
        }

        removeGeneratedRoot(spec)
      })()
    }

    return cleanupPromise
  }
}

function waitForSpawn(child) {
  if (isPositivePid(child.pid)) {
    return Promise.resolve(child.pid)
  }

  return new Promise((resolve, reject) => {
    const cleanupListeners = () => {
      child.removeListener('spawn', onSpawn)
      child.removeListener('error', onError)
    }
    const onSpawn = () => {
      cleanupListeners()

      if (isPositivePid(child.pid)) {
        resolve(child.pid)
      } else {
        reject(new Error('desktop launch emitted spawn without an owned PID'))
      }
    }
    const onError = error => {
      cleanupListeners()
      reject(new Error(`desktop launch failed: ${error.message}`, { cause: error }))
    }

    child.once('spawn', onSpawn)
    child.once('error', onError)
  })
}

export async function launchOwnedDesktop(spec, {
  spawnImpl = nodeSpawn,
  discoverWindowsDescendantPidsImpl,
  killImpl = nodeKill,
  platform = process.platform,
  spawnSyncImpl = nodeSpawnSync,
  terminationTimeoutMs = DEFAULT_TERMINATION_TIMEOUT_MS
} = {}) {
  const ownership = assertOwnedSpec(spec)

  if (ownership.launched) {
    throw new Error('desktop verifier launch spec may only be launched once')
  }

  let child

  try {
    child = spawnImpl(spec.executable, spec.args, spec.spawnOptions)
  } catch (error) {
    throw new Error(`desktop launch failed: ${error.message}`, { cause: error })
  }

  const ownedPid = await waitForSpawn(child)
  ownership.launched = true

  return {
    child,
    ownedPid,
    cleanup: createOwnedCleanup({
      spec,
      child,
      discoverWindowsDescendantPidsImpl,
      killImpl,
      ownedPid,
      platform,
      spawnSyncImpl,
      terminationTimeoutMs
    })
  }
}

export async function runDesktopSmoke(spec, {
  spawnImpl = nodeSpawn,
  discoverWindowsDescendantPidsImpl,
  killImpl = nodeKill,
  platform = process.platform,
  spawnSyncImpl = nodeSpawnSync,
  terminationTimeoutMs = DEFAULT_TERMINATION_TIMEOUT_MS,
  portTimeoutMs = DEFAULT_PORT_TIMEOUT_MS,
  pageTimeoutMs = DEFAULT_PAGE_TIMEOUT_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  fetchImpl = fetch,
  signal,
  onOwned,
  onReceipt
} = {}) {
  let owned = null
  let receipt = null
  let primaryError = null

  try {
    owned = await launchOwnedDesktop(spec, {
      spawnImpl,
      discoverWindowsDescendantPidsImpl,
      killImpl,
      platform,
      spawnSyncImpl,
      terminationTimeoutMs
    })
    onOwned?.(owned)

    const portRecord = await waitForDevToolsActivePort({
      userDataDir: spec.paths.userDataDir,
      child: owned.child,
      timeoutMs: portTimeoutMs,
      pollIntervalMs,
      signal
    })
    await waitForCdpPage({
      port: portRecord.port,
      child: owned.child,
      timeoutMs: pageTimeoutMs,
      pollIntervalMs,
      fetchImpl,
      signal
    })

    receipt = {
      pid: owned.ownedPid,
      port: portRecord.port,
      paths: {
        root: spec.paths.root,
        userDataDir: spec.paths.userDataDir,
        hermesHome: spec.paths.hermesHome,
        workspace: spec.paths.workspace
      }
    }
    await onReceipt?.(receipt)
  } catch (error) {
    primaryError = error
  }

  let cleanupError = null

  try {
    if (owned) {
      await owned.cleanup()
    } else {
      cleanupUnlaunchedDesktopSpec(spec)
    }
  } catch (error) {
    cleanupError = error
  }

  if (primaryError && cleanupError) {
    throw new AggregateError(
      [primaryError, cleanupError],
      'desktop verification and owned cleanup both failed'
    )
  }

  if (primaryError) {
    throw primaryError
  }

  if (cleanupError) {
    throw cleanupError
  }

  return receipt
}
