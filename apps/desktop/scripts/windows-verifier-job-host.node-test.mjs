import assert from 'node:assert/strict'
import { spawnSync as testSpawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PassThrough } from 'node:stream'
import { test } from 'node:test'

import * as verifierLib from './desktop-verifier-lib.mjs'

const JOB_HOST_BOOTSTRAP = fileURLToPath(
  new URL('./windows-verifier-job-host.ps1', import.meta.url)
)

function createControllerRunRoot() {
  const root = mkdtempSync(join(tmpdir(), 'windows-job-controller-run-'))
  const hermesHome = join(root, 'hermes-home')
  const workspace = join(root, 'workspace')
  mkdirSync(hermesHome)
  mkdirSync(workspace)

  return { hermesHome, root, workspace }
}

function runRealController(fixture, input = '') {
  return testSpawnSync(
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
      cwd: fixture.workspace,
      encoding: 'utf8',
      env: {
        ...verifierLib.stripCredentialEnvironment(process.env),
        HERMES_HOME: fixture.hermesHome
      },
      input,
      timeout: 20_000,
      windowsHide: true
    }
  )
}

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

test('checked-in Windows Job bootstrap compiles and reports launch EOF', {
  skip: process.platform !== 'win32'
}, () => {
  const fixture = createControllerRunRoot()

  try {
    const result = runRealController(fixture)

    assert.equal(result.error, undefined)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /received EOF before launch data/i)
  } finally {
    rmSync(fixture.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('checked-in Windows Job host rejects a missing target without starting it', {
  skip: process.platform !== 'win32'
}, () => {
  const fixture = createControllerRunRoot()

  try {
    const request = realLaunchRecord(fixture, {
      executable: join(fixture.root, 'missing-target.exe')
    })
    const result = runRealController(fixture, `${JSON.stringify(request)}\n`)

    assert.equal(result.status, 1)
    assert.match(result.stderr, /does not exist|failed/i)
    assert.equal(existsSync(join(fixture.workspace, 'target-ran')), false)
  } finally {
    rmSync(fixture.root, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
  }
})

test('checked-in Windows Job host fails closed on malformed launch fields before target start', {
  skip: process.platform !== 'win32'
}, () => {
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
      const result = runRealController(fixture, `${JSON.stringify(request)}\n`)

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
