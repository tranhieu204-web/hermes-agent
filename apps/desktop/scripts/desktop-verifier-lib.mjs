import { randomUUID } from 'node:crypto'
import { spawn as nodeSpawn } from 'node:child_process'
import { EventEmitter } from 'node:events'
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
const WINDOWS_JOB_BOOTSTRAP = fileURLToPath(
  new URL('./windows-verifier-job-host.ps1', import.meta.url)
)
export const WINDOWS_JOB_PROTOCOL_VERSION = 1
const WINDOWS_MAX_UINT32 = 0xFFFFFFFF
const WINDOWS_MAX_UINT64 = 18_446_744_073_709_551_615n
const WINDOWS_JOB_ERROR_STAGES = new Set(['cleanup', 'controller', 'protocol'])
const WINDOWS_JOB_RECORD_SCHEMAS = {
  cleaned: {
    fields: new Set([
      'activeProcesses',
      'activeProcessesBeforeTerminate',
      'cleanupRequestCount',
      'nonce',
      'terminationCount',
      'totalProcesses',
      'type',
      'v'
    ]),
    validate(record) {
      for (const field of [
        'activeProcesses',
        'activeProcessesBeforeTerminate',
        'cleanupRequestCount',
        'terminationCount',
        'totalProcesses'
      ]) {
        requireWindowsUint32(record[field], field)
      }
      if (record.activeProcesses !== 0 ||
          record.terminationCount !== 1 ||
          record.cleanupRequestCount !== 1 ||
          record.totalProcesses < 1 ||
          record.totalProcesses < record.activeProcessesBeforeTerminate) {
        throw new Error(
          'Windows Job controller cleanup acknowledgement was not zero-active, bounded, and idempotent'
        )
      }
    }
  },
  error: {
    fields: new Set(['message', 'nonce', 'stage', 'type', 'v']),
    validate(record) {
      requireWindowsString(record.message, 'message', { maxLength: 8192 })
      if (typeof record.stage !== 'string' || !WINDOWS_JOB_ERROR_STAGES.has(record.stage)) {
        throw new Error('Windows Job controller error stage is invalid')
      }
    }
  },
  launched: {
    fields: new Set(['nonce', 'target', 'type', 'v']),
    validate(record) {
      validateWindowsTargetRecord(record.target)
    }
  },
  status: {
    fields: new Set(['activeProcesses', 'nonce', 'target', 'type', 'v']),
    validate(record) {
      requireWindowsUint32(record.activeProcesses, 'activeProcesses')
      validateWindowsTargetRecord(record.target)
    }
  },
  target_exit: {
    fields: new Set(['nonce', 'targetPid', 'type', 'v']),
    validate(record) {
      requireWindowsPid(record.targetPid, 'targetPid')
    }
  }
}
const DEFAULT_PORT_TIMEOUT_MS = 30_000
const DEFAULT_PAGE_TIMEOUT_MS = 30_000
const DEFAULT_POLL_INTERVAL_MS = 100
const DEFAULT_TERMINATION_TIMEOUT_MS = 5_000

const CREDENTIAL_ENV_SUFFIXES = [
  '_API_KEY',
  '_BASE_URL',
  '_TOKEN',
  '_TOKEN_FILE',
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
  'ALL_PROXY',
  'AZURE_CONFIG_DIR',
  'BASH_ENV',
  'BASE_URL',
  'CLOUDSDK_CONFIG',
  'CREDENTIALS',
  'CURL_CA_BUNDLE',
  'CURL_HOME',
  'DATABASE_URL',
  'DOCKER_CONFIG',
  'DOCKER_AUTH_CONFIG',
  'ELECTRON_RUN_AS_NODE',
  'ENV',
  'GH_CONFIG_DIR',
  'GIT_ASKPASS',
  'GIT_CONFIG_GLOBAL',
  'GIT_CONFIG_SYSTEM',
  'GIT_SSH',
  'GIT_SSH_COMMAND',
  'GNUPGHOME',
  'GOOGLE_APPLICATION_CREDENTIALS',
  'HERMES_CONFIG',
  'HERMES_DESKTOP_CONNECTION_MODE',
  'HERMES_DESKTOP_REMOTE_TOKEN',
  'HERMES_DESKTOP_REMOTE_URL',
  'HERMES_PROFILE',
  'HERMES_PROFILE_NAME',
  'HERMES_ENV',
  'HTTPS_PROXY',
  'HTTP_PROXY',
  'KUBECONFIG',
  'KRB5CCNAME',
  'KRB5_CONFIG',
  'LD_PRELOAD',
  'NETRC',
  'NODE_EXTRA_CA_CERTS',
  'NODE_OPTIONS',
  'NODE_PATH',
  'NODE_TLS_REJECT_UNAUTHORIZED',
  'NO_PROXY',
  'NPM_CONFIG_USERCONFIG',
  'PIP_CONFIG_FILE',
  'PIP_EXTRA_INDEX_URL',
  'PIP_INDEX_URL',
  'PGPASSFILE',
  'PROMPT_COMMAND',
  'REDIS_URL',
  'REGISTRY_AUTH_FILE',
  'REQUESTS_CA_BUNDLE',
  'SSL_CERT_DIR',
  'SSL_CERT_FILE',
  'SSLKEYLOGFILE',
  'PASSWORD',
  'PRIVATE_KEY',
  'PROFILE',
  'SECRET',
  'SSH_ASKPASS',
  'SSH_ASKPASS_REQUIRE',
  'SSH_AUTH_SOCK',
  'SSH_AGENT_PID',
  'UV_CONFIG_FILE',
  'WGETRC',
  'XDG_CONFIG_HOME',
  'YARN_RC_FILENAME'
])
const CREDENTIAL_ENV_PREFIXES = [
  'DYLD_',
  'GIT_CONFIG_',
  'HERMES_',
  'NPM_CONFIG_',
  'UV_INDEX_',
  'YARN_NPM_'
]
const ISOLATED_RUNTIME_ENV_NAMES = new Set([
  'APPDATA',
  'COMSPEC',
  'HOME',
  'HOMEDRIVE',
  'HOMEPATH',
  'LOCALAPPDATA',
  'PATH',
  'PATHEXT',
  'PROGRAMDATA',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'USERPROFILE',
  'WINDIR'
])
const nodeKill = process.kill.bind(process)

function isPositivePid(pid) {
  return Number.isSafeInteger(pid) && pid > 0
}

function requireWindowsUint32(value, field) {
  if (!Number.isSafeInteger(value) || value < 0 || value > WINDOWS_MAX_UINT32) {
    throw new Error(
      `Windows Job controller emitted malformed protocol field ${field}: ` +
      `expected an unsigned 32-bit integer`
    )
  }

  return value
}

function requireWindowsPid(value, field) {
  requireWindowsUint32(value, field)
  if (value === 0) {
    throw new Error(
      `Windows Job controller emitted invalid process identity field ${field}: expected a positive PID`
    )
  }

  return value
}

function requireWindowsString(value, field, { maxLength = 32_767 } = {}) {
  if (typeof value !== 'string' || value.length === 0 ||
      value.length > maxLength || value.includes('\0')) {
    throw new Error(
      `Windows Job controller emitted malformed protocol field ${field}: ` +
      `expected a bounded non-empty NUL-free string`
    )
  }

  return value
}

function validateWindowsTargetRecord(target) {
  const expectedFields = ['creationTime100ns', 'executable', 'pid']

  if (!target || typeof target !== 'object' || Array.isArray(target) ||
      Object.keys(target).length !== expectedFields.length ||
      !expectedFields.every(field => Object.hasOwn(target, field))) {
    throw new Error('Windows Job controller target identity must use the exact schema')
  }

  requireWindowsPid(target.pid, 'target.pid')
  requireWindowsString(target.executable, 'target.executable')
  requireWindowsString(target.creationTime100ns, 'target.creationTime100ns', { maxLength: 20 })

  if (!/^[1-9]\d*$/.test(target.creationTime100ns) ||
      BigInt(target.creationTime100ns) > WINDOWS_MAX_UINT64) {
    throw new Error('Windows Job controller creation-time identity is invalid')
  }
}

function assertOwnedSpec(spec) {
  const ownership = spec?.[OWNED_SPEC]

  if (!ownership || ownership.root !== spec.paths?.root) {
    throw new Error('desktop verifier cleanup requires a repository-owned launch spec')
  }

  return ownership
}

function childHasExited(child) {
  return child.exited === true || child.exitCode !== null || child.signalCode !== null
}

function childExitDescription(child) {
  if (child.exitCode !== null) {
    return `exit code ${child.exitCode}`
  }

  if (child.signalCode !== null) {
    return `signal ${child.signalCode}`
  }

  if (child.exited === true) {
    return 'an authenticated target-exit notification'
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
    CREDENTIAL_ENV_PREFIXES.some(prefix => normalized.startsWith(prefix)) ||
    CREDENTIAL_ENV_SUFFIXES.some(suffix => normalized.endsWith(suffix))
}

export function stripCredentialEnvironment(baseEnv) {
  const env = {}
  const included = new Set()

  for (const [name, value] of Object.entries(baseEnv ?? {})) {
    const normalized = name.toUpperCase()

    if (value !== undefined && value !== null &&
        ISOLATED_RUNTIME_ENV_NAMES.has(normalized) && !included.has(normalized)) {
      env[name] = String(value)
      included.add(normalized)
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
      launchAttempted: false,
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

  if (ownership.launchAttempted) {
    throw new Error(
      `desktop verifier launch outcome is uncertain; retained generated root: ${ownership.root}`
    )
  }

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

function createWindowsTarget(receipt) {
  const target = new EventEmitter()

  target.pid = receipt.target.pid
  target.exitCode = null
  target.signalCode = null
  target.exited = false

  return target
}

function markWindowsTargetExited(target) {
  if (target.exited) {
    return
  }

  target.exited = true
  target.exitCode = 0
  target.emit('exit', 0, null)
}

function windowsControllerError(record, root) {
  const stage = typeof record.stage === 'string' ? ` ${record.stage}` : ''
  const detail = typeof record.message === 'string' ? record.message : 'unknown controller error'

  return new Error(
    `Windows Job controller${stage} failed: ${detail}; retained generated root: ${root}`
  )
}

function createWindowsProtocolSession(controller, nonce, root) {
  const records = []
  const waiters = []
  const terminalWaiters = []
  const targetHolder = { target: null }
  const pendingTargetExitPids = []
  let buffer = ''
  let cleanupAcknowledgement = null
  let cleanupAcknowledgementCount = 0
  let controllerExit = null
  let failure = null
  let launchRecordSeen = false
  let stderr = ''
  let stderrClosed = false
  let stderrEnded = false
  let stdoutClosed = false
  let stdoutEnded = false
  let targetExitSeen = false
  let terminalState = 'open'

  const retainedError = (message, cause) => new Error(
    message.includes(root) ? message : `${message}; retained generated root: ${root}`,
    cause ? { cause } : undefined
  )
  const closeInput = () => {
    if (!controller.stdin.destroyed && !controller.stdin.writableEnded) {
      controller.stdin.end()
    }
  }
  const rejectWaiters = error => {
    while (waiters.length) {
      const waiter = waiters.shift()
      clearTimeout(waiter.timer)
      waiter.reject(error)
    }
  }
  const rejectTerminalWaiters = error => {
    while (terminalWaiters.length) {
      const waiter = terminalWaiters.shift()
      clearTimeout(waiter.timer)
      waiter.reject(error)
    }
  }
  const fail = error => {
    if (failure) {
      return failure
    }

    const source = error instanceof Error ? error : new Error(String(error))
    failure = retainedError(source.message, source)
    terminalState = 'failed'
    rejectWaiters(failure)
    rejectTerminalWaiters(failure)
    closeInput()

    return failure
  }
  const settleTerminal = () => {
    if (failure || terminalState !== 'ack-provisional') {
      return
    }

    if (controllerExit &&
        (controllerExit.code !== 0 || controllerExit.signal !== null)) {
      fail(new Error(
        `Windows Job controller cleanup acknowledgement was invalidated by ` +
        `controller exit (code=${controllerExit.code}, signal=${controllerExit.signal ?? 'none'})`
      ))
      return
    }

    if (!controllerExit || !stdoutEnded || !stdoutClosed ||
        !stderrEnded || !stderrClosed) {
      return
    }

    if (buffer.length !== 0 || cleanupAcknowledgementCount !== 1 ||
        !cleanupAcknowledgement) {
      fail(new Error(
        'Windows Job controller cleanup acknowledgement did not reach one clean terminal state'
      ))
      return
    }

    terminalState = 'closed'
    while (terminalWaiters.length) {
      const waiter = terminalWaiters.shift()
      clearTimeout(waiter.timer)
      waiter.resolve(cleanupAcknowledgement)
    }
  }
  const validateRecord = record => {
    if (!record || typeof record !== 'object' || Array.isArray(record)) {
      throw new Error('Windows Job controller protocol record must be a JSON object')
    }

    if (record.v !== WINDOWS_JOB_PROTOCOL_VERSION || typeof record.type !== 'string') {
      throw new Error('Windows Job controller emitted a malformed protocol record')
    }

    if (typeof record.nonce !== 'string' || record.nonce !== nonce) {
      throw new Error('Windows Job controller emitted a record with an unauthenticated nonce')
    }

    const schema = WINDOWS_JOB_RECORD_SCHEMAS[record.type]
    const actualFields = Object.keys(record)
    if (!schema ||
        actualFields.length !== schema.fields.size ||
        actualFields.some(field => !schema.fields.has(field))) {
      throw new Error('Windows Job controller emitted a malformed protocol record with unsupported fields')
    }

    schema.validate(record)

    if (record.type === 'launched' && launchRecordSeen) {
      throw new Error('Windows Job controller emitted a duplicate launch receipt')
    }
    if (record.type === 'launched' && terminalState !== 'open') {
      throw new Error('Windows Job controller emitted a launch receipt in a terminal state')
    }
    if (['cleaned', 'status', 'target_exit'].includes(record.type) && !launchRecordSeen) {
      throw new Error(`Windows Job controller emitted ${record.type} before launch identity`)
    }
    if (record.type === 'target_exit' && targetExitSeen) {
      throw new Error('Windows Job controller emitted a duplicate target-exit record')
    }
    if (record.type === 'cleaned' && cleanupAcknowledgementCount !== 0) {
      throw new Error('Windows Job controller emitted a duplicate cleanup acknowledgement')
    }
  }
  const deliverToWaiter = record => {
    if (!waiters.length) {
      records.push(record)
      return
    }

    const waiter = waiters.shift()
    clearTimeout(waiter.timer)
    if (!waiter.types.includes(record.type)) {
      const error = fail(new Error(
        `Windows Job controller emitted unexpected ${record.type} while waiting for ${waiter.label}`
      ))
      waiter.reject(error)
      return
    }
    waiter.resolve(record)
  }
  const deliver = record => {
    if (record.type === 'launched') {
      launchRecordSeen = true
    } else if (record.type === 'target_exit') {
      targetExitSeen = true
      const target = targetHolder.target

      if (!target) {
        pendingTargetExitPids.push(record.targetPid)
        return
      }

      if (record.targetPid !== target.pid) {
        throw new Error('Windows Job controller emitted an invalid target-exit identity')
      }

      markWindowsTargetExited(target)
      return
    } else if (record.type === 'cleaned') {
      cleanupAcknowledgement = record
      cleanupAcknowledgementCount++
      terminalState = 'ack-provisional'
    }

    deliverToWaiter(record)
  }
  const processLine = line => {
    if (line === '') {
      throw new Error('Windows Job controller emitted an empty protocol line')
    }

    let record

    try {
      record = JSON.parse(line)
    } catch (error) {
      throw new Error(`Windows Job controller emitted malformed JSON: ${error.message}`, {
        cause: error
      })
    }

    validateRecord(record)
    deliver(record)
  }

  controller.stdout.setEncoding('utf8')
  controller.stderr.setEncoding('utf8')
  controller.stdout.on('data', chunk => {
    if (failure) {
      return
    }

    if (terminalState === 'ack-provisional' && chunk.length !== 0) {
      fail(new Error('Windows Job controller emitted trailing bytes after cleanup acknowledgement'))
      return
    }

    buffer += chunk

    while (buffer.includes('\n')) {
      const newline = buffer.indexOf('\n')
      const line = buffer.slice(0, newline).replace(/\r$/, '')
      buffer = buffer.slice(newline + 1)

      try {
        processLine(line)
      } catch (error) {
        fail(error)
        return
      }

      if (terminalState === 'ack-provisional' && buffer.length !== 0) {
        fail(new Error('Windows Job controller emitted trailing bytes after cleanup acknowledgement'))
        return
      }
    }
  })
  controller.stdout.once('end', () => {
    stdoutEnded = true
    if (buffer.length !== 0 && !failure) {
      fail(new Error('Windows Job controller closed stdout with an incomplete protocol line'))
      return
    }
    if (terminalState === 'open' && !failure) {
      fail(new Error('Windows Job controller closed stdout before authenticated cleanup'))
      return
    }
    settleTerminal()
  })
  controller.stdout.once('close', () => {
    stdoutClosed = true
    if (!stdoutEnded && !failure) {
      fail(new Error('Windows Job controller closed stdout before the protocol stream ended'))
      return
    }
    settleTerminal()
  })
  controller.stderr.on('data', chunk => {
    stderr = `${stderr}${chunk}`.slice(-8192)
    if (`${chunk}`.length !== 0 && !failure) {
      fail(new Error('Windows Job controller emitted a stderr protocol error'))
    }
  })
  controller.stderr.once('end', () => {
    stderrEnded = true
    settleTerminal()
  })
  controller.stderr.once('close', () => {
    stderrClosed = true
    if (!stderrEnded && !failure) {
      fail(new Error('Windows Job controller closed stderr before the protocol stream ended'))
      return
    }
    settleTerminal()
  })
  controller.once('error', error => {
    fail(new Error(`Windows Job controller failed to start: ${error.message}`, { cause: error }))
  })
  controller.once('exit', (code, signal) => {
    controllerExit = { code, signal }
    if (terminalState === 'open' && !failure) {
      const detail = stderr.trim()
      fail(new Error(
        `Windows Job controller exited before authenticated cleanup ` +
        `(code=${code}, signal=${signal ?? 'none'})` +
        (detail ? `: ${detail}` : '')
      ))
      return
    }
    settleTerminal()
  })

  return {
    bindTarget(target) {
      if (targetHolder.target) {
        throw retainedError('Windows Job controller target identity was already bound')
      }

      targetHolder.target = target
      for (const targetPid of pendingTargetExitPids) {
        if (targetPid !== target.pid) {
          throw fail(new Error('Windows Job controller emitted an invalid target-exit identity'))
        }
        markWindowsTargetExited(target)
      }
      pendingTargetExitPids.length = 0
    },
    closeInput,
    async waitFor(types, timeoutMs, label) {
      if (failure) {
        throw failure
      }

      const record = records.shift()

      if (record) {
        if (!types.includes(record.type)) {
          throw fail(new Error(
            `Windows Job controller emitted unexpected ${record.type} while waiting for ${label}`
          ))
        }

        return record
      }

      return await new Promise((resolve, reject) => {
        const waiter = { types, label, resolve, reject, timer: null }
        waiter.timer = setTimeout(() => {
          fail(new Error(`timed out waiting for Windows Job controller ${label}`))
        }, timeoutMs)
        waiters.push(waiter)
      })
    },
    async waitForCleanTermination(timeoutMs) {
      if (failure) {
        throw failure
      }
      if (terminalState === 'closed') {
        return cleanupAcknowledgement
      }
      if (terminalState !== 'ack-provisional' || cleanupAcknowledgementCount !== 1) {
        throw fail(new Error(
          'Windows Job controller terminal wait requires exactly one provisional cleanup acknowledgement'
        ))
      }

      return await new Promise((resolve, reject) => {
        const waiter = { resolve, reject, timer: null }
        waiter.timer = setTimeout(() => {
          fail(new Error(
            `timed out waiting for Windows Job controller clean exit and closed streams after ${timeoutMs}ms`
          ))
        }, timeoutMs)
        terminalWaiters.push(waiter)
        settleTerminal()
      })
    }
  }
}

function validateWindowsTargetIdentity(target, spec, root) {
  try {
    validateWindowsTargetRecord(target)
  } catch (error) {
    throw new Error(
      `Windows Job controller target identity is malformed; retained generated root: ${root}`,
      { cause: error }
    )
  }

  if (resolve(target.executable).toLowerCase() !== resolve(spec.executable).toLowerCase()) {
    throw new Error(
      `Windows Job controller launch receipt executable identity does not match; ` +
      `retained generated root: ${root}`
    )
  }
}

function validateWindowsLaunchReceipt(record, spec, root) {
  if (record?.type !== 'launched') {
    throw new Error(
      `Windows Job controller launch receipt is malformed; retained generated root: ${root}`
    )
  }

  validateWindowsTargetIdentity(record.target, spec, root)
}

function createWindowsIdentitySampler({
  controller,
  nonce,
  ownership,
  protocol,
  receipt,
  spec,
  timeoutMs
}) {
  let samplingPromise = null

  return () => {
    if (!samplingPromise) {
      samplingPromise = (async () => {
        controller.stdin.write(`${JSON.stringify({
          v: WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'status',
          nonce
        })}\n`)
        const record = await protocol.waitFor(
          ['status', 'error'],
          timeoutMs,
          'target identity status'
        )

        if (record.type === 'error') {
          throw windowsControllerError(record, ownership.root)
        }

        validateWindowsTargetIdentity(record.target, spec, ownership.root)
        if (record.target.pid !== receipt.target.pid ||
            record.target.creationTime100ns !== receipt.target.creationTime100ns) {
          throw new Error(
            `Windows Job controller target identity changed; retained generated root: ${ownership.root}`
          )
        }

        return record.target
      })().finally(() => {
        samplingPromise = null
      })
    }

    return samplingPromise
  }
}

function createWindowsOwnedCleanup({
  controller,
  nonce,
  ownership,
  protocol,
  spec,
  target,
  terminationTimeoutMs
}) {
  let cleanupPromise = null

  return () => {
    if (!cleanupPromise) {
      cleanupPromise = (async () => {
        try {
          controller.stdin.write(`${JSON.stringify({
            v: WINDOWS_JOB_PROTOCOL_VERSION,
            type: 'cleanup',
            nonce
          })}\n`)

          const record = await protocol.waitFor(
            ['cleaned', 'error'],
            terminationTimeoutMs + 2_000,
            'zero-active cleanup acknowledgement'
          )

          if (record.type === 'error') {
            throw windowsControllerError(record, ownership.root)
          }

          const terminal = protocol.waitForCleanTermination(terminationTimeoutMs)
          protocol.closeInput()
          const authenticatedRecord = await terminal
          markWindowsTargetExited(target)
          removeGeneratedRoot(spec)

          return authenticatedRecord
        } catch (error) {
          protocol.closeInput()
          try {
            await waitForChildExit(
              controller,
              terminationTimeoutMs,
              'Windows Job controller after failed cleanup'
            )
          } catch (closeError) {
            throw new AggregateError(
              [error, closeError],
              `Windows Job controller cleanup failed and retained authority did not close; ` +
              `retained generated root: ${ownership.root}`
            )
          }
          throw error
        }
      })()
    }

    return cleanupPromise
  }
}

async function launchWindowsOwnedDesktop(spec, {
  controllerTimeoutMs,
  spawnImpl,
  terminationTimeoutMs
}) {
  const ownership = assertOwnedSpec(spec)

  if (!isAbsolute(spec.executable)) {
    throw new Error(
      `Windows Job controller requires an exact absolute target executable; ` +
      `retained generated root: ${ownership.root}`
    )
  }

  ownership.launchAttempted = true
  const nonce = randomUUID()
  let controller

  try {
    controller = spawnImpl(
      'powershell.exe',
      [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        WINDOWS_JOB_BOOTSTRAP
      ],
      {
        cwd: spec.spawnOptions.cwd,
        env: spec.env,
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true
      }
    )
  } catch (error) {
    throw new Error(
      `Windows Job controller launch failed: ${error.message}; ` +
      `retained generated root: ${ownership.root}`,
      { cause: error }
    )
  }

  const protocol = createWindowsProtocolSession(controller, nonce, ownership.root)

  try {
    await waitForSpawn(controller)
    controller.stdin.write(`${JSON.stringify({
      v: WINDOWS_JOB_PROTOCOL_VERSION,
      type: 'launch',
      nonce,
      executable: spec.executable,
      args: spec.args,
      cwd: spec.spawnOptions.cwd,
      environment: spec.env,
      terminationTimeoutMs
    })}\n`)

    const receipt = await protocol.waitFor(
      ['launched', 'error'],
      controllerTimeoutMs,
      'authenticated launch receipt'
    )

    if (receipt.type === 'error') {
      throw windowsControllerError(receipt, ownership.root)
    }

    validateWindowsLaunchReceipt(receipt, spec, ownership.root)
    const target = createWindowsTarget(receipt)
    protocol.bindTarget(target)
    ownership.launched = true

    return {
      child: target,
      nonce,
      ownedPid: receipt.target.pid,
      receipt,
      isControllerRunning: () => !childHasExited(controller),
      sampleIdentity: createWindowsIdentitySampler({
        controller,
        nonce,
        ownership,
        protocol,
        receipt,
        spec,
        timeoutMs: controllerTimeoutMs
      }),
      cleanup: createWindowsOwnedCleanup({
        controller,
        nonce,
        ownership,
        protocol,
        spec,
        target,
        terminationTimeoutMs
      })
    }
  } catch (error) {
    protocol.closeInput()
    try {
      await waitForChildExit(
        controller,
        terminationTimeoutMs,
        'Windows Job controller after failed launch'
      )
    } catch (closeError) {
      throw new AggregateError(
        [error, closeError],
        `Windows Job controller launch failed and retained authority did not close; ` +
        `retained generated root: ${ownership.root}`
      )
    }
    throw error
  }
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

function createPosixOwnedCleanup({
  spec,
  child,
  killImpl,
  ownedPid,
  terminationTimeoutMs
}) {
  let cleanupPromise = null

  return () => {
    if (!cleanupPromise) {
      cleanupPromise = (async () => {
        await terminateOwnedProcessGroup({
          child,
          killImpl,
          ownedPid,
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
  controllerTimeoutMs = 30_000,
  spawnImpl = nodeSpawn,
  killImpl = nodeKill,
  platform = process.platform,
  terminationTimeoutMs = DEFAULT_TERMINATION_TIMEOUT_MS
} = {}) {
  const ownership = assertOwnedSpec(spec)

  if (ownership.launched || ownership.launchAttempted) {
    throw new Error('desktop verifier launch spec may only be launched once')
  }

  if (platform === 'win32') {
    return await launchWindowsOwnedDesktop(spec, {
      controllerTimeoutMs,
      spawnImpl,
      terminationTimeoutMs
    })
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
    cleanup: createPosixOwnedCleanup({
      spec,
      child,
      killImpl,
      ownedPid,
      terminationTimeoutMs
    })
  }
}

export async function runDesktopSmoke(spec, {
  controllerTimeoutMs = 30_000,
  spawnImpl = nodeSpawn,
  killImpl = nodeKill,
  platform = process.platform,
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
      controllerTimeoutMs,
      spawnImpl,
      killImpl,
      platform,
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
      `desktop verification and owned cleanup both failed; ` +
      `retained generated root: ${spec.paths.root}`
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
