import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { EventEmitter, once } from 'node:events'
import { PassThrough } from 'node:stream'
import { test } from 'node:test'

import * as verifierLib from './desktop-verifier-lib.mjs'
import { launchDirectOwnedVerifierTestProcess } from './owned-verifier-test-process.mjs'
import {
  assertDesktopExecutableProvenance,
  cleanupUnlaunchedDesktopSpec,
  createDesktopLaunchSpec,
  launchOwnedDesktop,
  parseDevToolsActivePort,
  runDesktopSmoke
} from './desktop-verifier-lib.mjs'

function createNodeProcessSpec(options = {}) {
  return createDesktopLaunchSpec({
    executable: process.execPath,
    executableArgs: ['-e', options.script ?? 'setInterval(() => {}, 1_000)', '--'],
    baseEnv: options.baseEnv,
    tempBaseDir: options.tempBaseDir
  })
}

function createIsolatedWindowsNodeProcessSpec(options = {}) {
  const parent = mkdtempSync(join(tmpdir(), 'desktop-verifier-windows-'))
  const localAppData = join(parent, 'local-app-data')
  const temporaryDirectory = join(parent, 'temporary')
  mkdirSync(localAppData)
  mkdirSync(temporaryDirectory)

  return {
    parent,
    spec: createNodeProcessSpec({
      ...options,
      baseEnv: {
        ...process.env,
        ...options.baseEnv,
        LOCALAPPDATA: localAppData,
        TEMP: temporaryDirectory,
        TMP: temporaryDirectory
      },
      tempBaseDir: parent
    })
  }
}

let nextSyntheticWindowsTargetPid = 420_000

function createSyntheticWindowsController(spec, { launchError } = {}) {
  const controller = new EventEmitter()
  const stdin = new PassThrough()
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  const targetPid = nextSyntheticWindowsTargetPid++
  let buffered = ''
  let exited = false

  Object.assign(controller, {
    pid: nextSyntheticWindowsTargetPid++,
    exitCode: null,
    signalCode: null,
    stdin,
    stdout,
    stderr
  })

  const finish = code => {
    if (exited) {
      return
    }
    exited = true
    controller.exitCode = code
    stdin.end()
    stdout.end()
    stderr.end()
    controller.emit('exit', code, null)
  }

  controller.kill = () => {
    finish(0)
    return true
  }
  stdin.setEncoding('utf8')
  stdin.on('data', chunk => {
    buffered += chunk
    while (buffered.includes('\n')) {
      const newline = buffered.indexOf('\n')
      const record = JSON.parse(buffered.slice(0, newline))
      buffered = buffered.slice(newline + 1)

      if (record.type === 'launch') {
        const payload = launchError
          ? {
              v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
              type: 'error',
              nonce: record.nonce,
              stage: 'controller',
              message: launchError
            }
          : {
              v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
              type: 'launched',
              nonce: record.nonce,
              target: {
                pid: targetPid,
                creationTime100ns: '1',
                executable: spec.executable
              }
            }
        stdout.write(`${JSON.stringify(payload)}\n`)
      } else if (record.type === 'cleanup') {
        stdout.write(`${JSON.stringify({
          v: verifierLib.WINDOWS_JOB_PROTOCOL_VERSION,
          type: 'cleaned',
          nonce: record.nonce,
          activeProcessesBeforeTerminate: 1,
          activeProcesses: 0,
          totalProcesses: 1,
          terminationCount: 1,
          cleanupRequestCount: 1
        })}\n`)
      }
    }
  })
  stdin.once('finish', () => setImmediate(() => finish(0)))
  queueMicrotask(() => controller.emit('spawn'))
  return controller
}

function launchGenericOwnedDesktop(spec, options = {}) {
  if (process.platform !== 'win32') {
    return launchOwnedDesktop(spec)
  }
  const controller = createSyntheticWindowsController(spec, options)
  return launchOwnedDesktop(spec, { spawnImpl: () => controller })
}

function runGenericDesktopSmoke(spec, options = {}) {
  if (process.platform !== 'win32') {
    return runDesktopSmoke(spec, options)
  }
  const controller = createSyntheticWindowsController(spec, options)
  return runDesktopSmoke(spec, { ...options, spawnImpl: () => controller })
}

test('Windows Node process fixtures isolate cache and compiler temporary state', () => {
  const first = createIsolatedWindowsNodeProcessSpec()
  const second = createIsolatedWindowsNodeProcessSpec()

  try {
    for (const fixture of [first, second]) {
      assert.equal(fixture.spec.env.LOCALAPPDATA.startsWith(fixture.parent), true)
      assert.equal(fixture.spec.env.TEMP.startsWith(fixture.parent), true)
      assert.equal(fixture.spec.env.TMP.startsWith(fixture.parent), true)
      assert.equal(fixture.spec.env.HERMES_HOME.startsWith(fixture.parent), true)
      assert.equal(fixture.spec.paths.root.startsWith(fixture.parent), true)
    }
    assert.notEqual(first.spec.env.LOCALAPPDATA, second.spec.env.LOCALAPPDATA)
    assert.notEqual(first.spec.env.TEMP, second.spec.env.TEMP)
  } finally {
    cleanupUnlaunchedDesktopSpec(first.spec)
    cleanupUnlaunchedDesktopSpec(second.spec)
    rmSync(first.parent, { recursive: true, force: true })
    rmSync(second.parent, { recursive: true, force: true })
  }
})

const WINDOWS_JOB_SOURCE = fileURLToPath(
  new URL('./windows-verifier-job-host.cs', import.meta.url)
)

function windowsJobSourceSha256() {
  return createHash('sha256').update(readFileSync(WINDOWS_JOB_SOURCE)).digest('hex')
}

async function waitForExit(child, timeoutMs = 5000) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }

  let timer

  try {
    await Promise.race([
      once(child, 'exit'),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`child ${child.pid} did not exit`)), timeoutMs)
      })
    ])
  } finally {
    clearTimeout(timer)
  }
}

async function waitForFile(filePath, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() <= deadline) {
    if (existsSync(filePath)) {
      return
    }

    await new Promise(resolve => setTimeout(resolve, 20))
  }

  throw new Error(`timed out waiting for ${filePath}`)
}

test('Windows Job preparation is bounded and terminates only its preparer', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const keepEventLoopAlive = setInterval(() => {}, 10)
  preparer.killCalls = 0
  preparer.kill = () => {
    preparer.killCalls += 1
    preparer.emit('exit', null, 'SIGTERM')
    return true
  }

  try {
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 10,
        spawnImpl: () => preparer
      }),
      /preparation timed out after 10ms/i
    )
    assert.equal(preparer.killCalls, 1)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    clearInterval(keepEventLoopAlive)
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('owned verifier test helper rejects a missing executable without an unhandled child error', async () => {
  await assert.rejects(
    launchDirectOwnedVerifierTestProcess('missing-verifier-test-child.exe', [], {
      spawnOptions: { stdio: 'ignore', windowsHide: true }
    }),
    /missing-verifier-test-child\.exe/i
  )
})

test('Windows Job preparation timeout waits for its owned preparer and preserves an unrelated sentinel', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const sentinel = await launchDirectOwnedVerifierTestProcess(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1_000)'],
    { spawnOptions: { stdio: 'ignore', windowsHide: true } }
  )
  const preparer = await launchDirectOwnedVerifierTestProcess(
    process.execPath,
    ['-e', 'setInterval(() => {}, 1_000)'],
    { spawnOptions: { stdio: 'ignore', windowsHide: true } }
  )
  let ownedPreparerExited = false

  try {
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 25,
        spawnImpl: () => {
          preparer.child.once('exit', () => {
            ownedPreparerExited = true
          })
          return preparer.child
        }
      }),
      /preparation timed out after 25ms/i
    )
    assert.equal(ownedPreparerExited, true, 'owned preparer must exit before timeout rejects')
    assert.equal(sentinel.child.exitCode, null, 'unrelated sentinel must remain alive')
  } finally {
    await preparer.cleanup()
    await sentinel.cleanup()
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation accepts a bounded successful preparer', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }

  try {
    queueMicrotask(() => preparer.emit('exit', 0, null))
    const result = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.equal(result, undefined, 'the default production contract remains void')
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation returns only source-bound compile diagnostics when requested', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  const forgedSourceSha256 = 'a'.repeat(64)
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `HermesVerifierJobHost diagnostic compile_start source_sha256=${sourceSha256}\n` +
        `HermesVerifierJobHost diagnostic compile_end source_sha256=${sourceSha256} output_sha256=${'b'.repeat(64)}\n` +
        `HermesVerifierJobHost diagnostic compile_start source_sha256=${forgedSourceSha256}\n` +
        'C:\\never-log-controlled-root\n'
      )
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, new RegExp(`compile_start source_sha256=${sourceSha256}`))
    assert.match(diagnostics, new RegExp(`compile_end source_sha256=${sourceSha256} output_sha256=${'b'.repeat(64)}`))
    assert.match(diagnostics, /lifecycle=spawn_requested elapsed_ms=\d+/)
    assert.match(diagnostics, /lifecycle=exit code=0 elapsed_ms=\d+/)
    assert.match(diagnostics, /lifecycle=stderr_end elapsed_ms=\d+/)
    assert.match(diagnostics, /classification=FALSIFIED/)
    assert.equal(diagnostics.includes(forgedSourceSha256), false)
    assert.equal(diagnostics.includes('C:\\never-log-controlled-root'), false)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation retains only valid source-bound native lock-open diagnostics', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  const forgedSourceSha256 = 'd'.repeat(64)
  const pathBearingSuffix = ' C:\\never-log-controlled-root'
  const validEnter = `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=1 source_sha256=${sourceSha256} sequence=7`
  const validRetry = `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=1 outcome=io_exception_retry source_sha256=${sourceSha256} sequence=8`
  const validAcquire = `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=2 outcome=acquired_after_retry source_sha256=${sourceSha256} sequence=10`
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `${validEnter}\n` +
        `${validRetry}\n` +
        `HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=2 source_sha256=${forgedSourceSha256} sequence=9${pathBearingSuffix}\n` +
        `HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=2 outcome=untrusted source_sha256=${sourceSha256} sequence=10${pathBearingSuffix}\n` +
        `${validAcquire}\n`
      )
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, new RegExp(`event=lock_open_enter attempt_seq=1 source_sha256=${sourceSha256} sequence=7`))
    assert.match(diagnostics, new RegExp(`event=lock_open_outcome attempt_seq=1 outcome=io_exception_retry source_sha256=${sourceSha256} sequence=8`))
    assert.match(diagnostics, new RegExp(`event=lock_open_outcome attempt_seq=2 outcome=acquired_after_retry source_sha256=${sourceSha256} sequence=10`))
    assert.equal(diagnostics.includes(pathBearingSuffix), false)
    assert.equal(diagnostics.includes(forgedSourceSha256), false)
    assert.equal(diagnostics.includes('outcome=untrusted'), false)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation retains only fixed source-bound postcompile boundaries', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  const forgedSourceSha256 = 'f'.repeat(64)
  const validBegin = `HermesVerifierJobHost diagnostic event=postcompile_boundary boundary=artifact_hash state=begin source_sha256=${sourceSha256} sequence=11`
  const validEnd = `HermesVerifierJobHost diagnostic event=postcompile_boundary boundary=artifact_hash state=end source_sha256=${sourceSha256} sequence=12`
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `${validBegin}\n` +
        `${validEnd}\n` +
        `HermesVerifierJobHost diagnostic event=postcompile_boundary boundary=artifact_hash state=unknown source_sha256=${sourceSha256} sequence=13 C:\\never-log-controlled-root\n` +
        `HermesVerifierJobHost diagnostic event=postcompile_boundary boundary=dll_publish state=begin source_sha256=${forgedSourceSha256} sequence=14\n`
      )
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, new RegExp(`event=postcompile_boundary boundary=artifact_hash state=begin source_sha256=${sourceSha256} sequence=11`))
    assert.match(diagnostics, new RegExp(`event=postcompile_boundary boundary=artifact_hash state=end source_sha256=${sourceSha256} sequence=12`))
    assert.match(diagnostics, /lifecycle=invalid_sequence/)
    assert.match(diagnostics, /classification=DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP/)
    assert.equal(diagnostics.includes('state=unknown'), false)
    assert.equal(diagnostics.includes('C:\\never-log-controlled-root'), false)
    assert.equal(diagnostics.includes(forgedSourceSha256), false)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation fails closed on malformed source-bound lock evidence', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('successful preparer must not be terminated')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `HermesVerifierJobHost diagnostic event=lock_retry_budget attempt_seq=1 state=bogus source_sha256=${sourceSha256} sequence=7\n`
      )
      preparer.emit('exit', 0, null)
    })
    const diagnostics = await verifierLib.prepareWindowsJobHost(spec, {
      prepareTimeoutMs: 100,
      spawnImpl: () => preparer
    })
    assert.match(diagnostics, /lifecycle=invalid_sequence/)
    assert.match(diagnostics, /classification=DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP/)
    assert.equal(diagnostics.includes('state=bogus'), false)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation appends only a source-bound precompile failure diagnostic', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  const forgedSourceSha256 = 'c'.repeat(64)
  const marker = `HermesVerifierJobHost diagnostic precompile_failure source_sha256=${sourceSha256} class=cache_root_unavailable`
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('failed preparer must not be terminated after exit')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `${marker}\n` +
        `HermesVerifierJobHost diagnostic precompile_failure source_sha256=${forgedSourceSha256} class=cache_root_unavailable\n` +
        'C:\\never-log-controlled-root\n'
      )
      preparer.emit('exit', 1, null)
    })
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 100,
        spawnImpl: () => preparer
      }),
      error => {
        assert.match(error.message, new RegExp(`Windows Job controller preparation failed; ${marker}`))
        assert.match(error.message, /lifecycle=exit code=1 elapsed_ms=\d+/)
        assert.match(error.message, /classification=DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP/)
        assert.equal(error.message.includes(forgedSourceSha256), false)
        assert.equal(error.message.includes('C:\\never-log-controlled-root'), false)
        return true
      }
    )
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation appends only a source-bound Add-Type exception outcome', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  const forgedSourceSha256 = 'e'.repeat(64)
  const marker = `HermesVerifierJobHost diagnostic compile_outcome outcome=exception source_sha256=${sourceSha256}`
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('failed preparer must not be terminated after exit')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `HermesVerifierJobHost diagnostic compile_start source_sha256=${sourceSha256}\n` +
        `${marker}\n` +
        `HermesVerifierJobHost diagnostic compile_outcome outcome=exception source_sha256=${forgedSourceSha256}\n` +
        `HermesVerifierJobHost diagnostic compile_outcome outcome=exception source_sha256=${sourceSha256} C:\\never-log-controlled-root\n` +
        'synthetic compiler exception at C:\\never-log-controlled-root\n'
      )
      preparer.emit('exit', 1, null)
    })
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 100,
        spawnImpl: () => preparer
      }),
      error => {
        assert.match(error.message, new RegExp(`Windows Job controller preparation failed; .*${marker}`))
        assert.match(error.message, /classification=ADD_TYPE_COMPILE_EXCEPTION/)
        assert.equal(error.message.includes(forgedSourceSha256), false)
        assert.equal(error.message.includes('never-log-controlled-root'), false)
        assert.equal(error.message.includes('synthetic compiler exception'), false)
        return true
      }
    )
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation rejects malformed Add-Type exception markers', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  const sourceSha256 = windowsJobSourceSha256()
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('failed preparer must not be terminated after exit')
  }
  spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS = '1'

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        `HermesVerifierJobHost diagnostic compile_start source_sha256=${sourceSha256}\n` +
        `HermesVerifierJobHost diagnostic compile_outcome outcome=exception source_sha256=${sourceSha256} C:\\never-log-controlled-root\n`
      )
      preparer.emit('exit', 1, null)
    })
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 100,
        spawnImpl: () => preparer
      }),
      error => {
        assert.match(error.message, /classification=ADD_TYPE_COMPILE_STALL/)
        assert.equal(error.message.includes('compile_outcome'), false)
        assert.equal(error.message.includes('never-log-controlled-root'), false)
        return true
      }
    )
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation preserves ordered source-bound lock budget evidence', () => {
  const sourceSha256 = windowsJobSourceSha256()
  const prefix = 'HermesVerifierJobHost diagnostic'
  const lifecycle = `${prefix} lifecycle=deadline elapsed_ms=29000; ${prefix} lifecycle=termination_request result=1 elapsed_ms=29000; ${prefix} lifecycle=stderr_end elapsed_ms=29001; ${prefix} lifecycle=exit code=-1 elapsed_ms=29020`
  const retry = `${prefix} event=lock_open_enter attempt_seq=1 source_sha256=${sourceSha256} sequence=7; ${prefix} event=lock_open_outcome attempt_seq=1 outcome=io_exception_retry source_sha256=${sourceSha256} sequence=8`

  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${retry}; ${prefix} event=lock_retry_budget attempt_seq=1 state=exhausted source_sha256=${sourceSha256} sequence=9; ${lifecycle}`
    ),
    'LOCK_RETRY_BUDGET_EXHAUSTED'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${retry}; ${prefix} event=lock_retry_budget attempt_seq=1 state=remaining source_sha256=${sourceSha256} sequence=9; ${lifecycle}`
    ),
    'OUTER_DEADLINE_BEFORE_LOCK_BUDGET'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${prefix} event=lock_open_enter attempt_seq=1 source_sha256=${sourceSha256} sequence=7; ${lifecycle}`
    ),
    'LOCK_OPEN_NONRETURNING'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${retry}; ${prefix} event=lock_retry_budget attempt_seq=1 state=remaining source_sha256=${sourceSha256} sequence=8; ${lifecycle}`
    ),
    'DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${retry}; ${prefix} event=lock_retry_budget attempt_seq=1 state=bogus source_sha256=${sourceSha256} sequence=9; ${lifecycle}`
    ),
    'DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP'
  )
  assert.equal(
    verifierLib.classifyWindowsJobHostPreparationDiagnostics(
      `${retry}; ${prefix} event=lock_open_outcome attempt_seq=1 source_sha256=${sourceSha256} sequence=9; ${lifecycle}`
    ),
    'DIAGNOSTIC_CAPTURE_OR_ALLOWLIST_GAP'
  )
})

test('Windows Job preparation exposes only the recognized cache-lock failure class', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('nonzero preparer must not be terminated')
  }

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        'Windows verifier Job host bootstrap failed: verifier Job host cache lock timed out after 20000 ms\n'
      )
      preparer.emit('exit', 1, null)
    })
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 100,
        spawnImpl: () => preparer
      }),
      error => {
        assert.match(error.message, /preparation failed; cache lock timeout/i)
        assert.doesNotMatch(error.message, /never-log-secret-path/i)
        return true
      }
    )
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('Windows Job preparation does not classify untrusted stderr substrings as a cache-lock failure', async () => {
  const spec = createDesktopLaunchSpec({ executable: process.execPath })
  const preparer = new EventEmitter()
  preparer.stderr = new PassThrough()
  preparer.kill = () => {
    throw new Error('nonzero preparer must not be terminated')
  }

  try {
    queueMicrotask(() => {
      preparer.stderr.end(
        'tool text: verifier Job host cache lock timed out after 20000 ms at C:\\never-log-secret-path\n'
      )
      preparer.emit('exit', 1, null)
    })
    await assert.rejects(
      verifierLib.prepareWindowsJobHost(spec, {
        prepareTimeoutMs: 100,
        spawnImpl: () => preparer
      }),
      error => {
        assert.equal(error.message, 'Windows Job controller preparation failed')
        assert.doesNotMatch(error.message, /never-log-secret-path/i)
        return true
      }
    )
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})


test('two launch specs own unique roots, state paths, and app identities', () => {
  const first = createDesktopLaunchSpec({ executable: 'Hermes.exe' })
  const second = createDesktopLaunchSpec({ executable: 'Hermes.exe' })

  try {
    assert.notEqual(first.paths.root, second.paths.root)
    assert.notEqual(first.paths.userDataDir, second.paths.userDataDir)
    assert.notEqual(first.paths.hermesHome, second.paths.hermesHome)
    assert.notEqual(first.paths.workspace, second.paths.workspace)
    assert.notEqual(first.env.HERMES_DESKTOP_APP_NAME, second.env.HERMES_DESKTOP_APP_NAME)
  } finally {
    cleanupUnlaunchedDesktopSpec(first)
    cleanupUnlaunchedDesktopSpec(second)
  }
})

test('launch spec pins exact isolation args/env and platform-owned process-group policy', () => {
  const spec = createDesktopLaunchSpec({
    executable: 'Hermes.exe',
    fakeBoot: true,
    baseEnv: {
      PATH: 'safe-path',
      CUSTOM_API_KEY: 'custom-secret',
      OPENAI_API_KEY: 'openai-secret',
      OPENAI_BASE_URL: 'https://credential-bearing.example',
      HERMES_DESKTOP_BOOT_FAKE_ERROR: 'inherited failure',
      HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS: '1',
      HERMES_HOME: 'live-home',
      HERMES_DESKTOP_APP_NAME: 'Hermes',
      HERMES_DESKTOP_USER_DATA_DIR: 'live-user-data'
    }
  })

  try {
    assert.deepEqual(spec.args, [
      `--user-data-dir=${spec.paths.userDataDir}`,
      '--remote-debugging-address=127.0.0.1',
      '--remote-debugging-port=0'
    ])
    assert.equal(spec.env.PATH, 'safe-path')
    assert.equal(spec.env.HERMES_DESKTOP_USER_DATA_DIR, spec.paths.userDataDir)
    assert.equal(spec.env.HERMES_HOME, spec.paths.hermesHome)
    assert.equal(spec.env.HERMES_DESKTOP_CWD, spec.paths.workspace)
    assert.equal(spec.env.HERMES_DESKTOP_IGNORE_EXISTING, '1')
    assert.match(spec.env.HERMES_DESKTOP_APP_NAME, /^HermesDesktopVerifier-/)
    assert.equal(spec.env.HERMES_DESKTOP_BOOT_FAKE, '1')
    assert.equal(spec.env.HERMES_DESKTOP_BOOT_FAKE_STEP_MS, '120')
    assert.equal(spec.env.HERMES_DESKTOP_BOOT_FAKE_ERROR, undefined)
    assert.equal(spec.env.HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS, undefined)
    assert.equal(spec.env.CUSTOM_API_KEY, undefined)
    assert.equal(spec.env.OPENAI_API_KEY, undefined)
    assert.equal(spec.env.OPENAI_BASE_URL, undefined)
    assert.equal(spec.spawnOptions.cwd, spec.paths.workspace)
    assert.deepEqual(spec.spawnOptions.stdio, ['ignore', 'inherit', 'inherit'])
    assert.equal(spec.spawnOptions.detached, process.platform === 'win32' ? undefined : true)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('launch spec strips credential and configuration routes while preserving benign runtime paths', () => {
  const sensitiveNames = [
    'SSH_AUTH_SOCK',
    'SSH_AGENT_PID',
    'SSH_ASKPASS',
    'GNUPGHOME',
    'KUBECONFIG',
    'DOCKER_CONFIG',
    'DOCKER_AUTH_CONFIG',
    'NETRC',
    'AWS_SHARED_CREDENTIALS_FILE',
    'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_CONFIG_FILE',
    'GOOGLE_APPLICATION_CREDENTIALS',
    'CLOUDSDK_CONFIG',
    'AZURE_CONFIG_DIR',
    'GIT_ASKPASS',
    'GIT_CONFIG_GLOBAL',
    'GIT_CONFIG_SYSTEM',
    'GIT_CONFIG_KEY_0',
    'GIT_SSH_COMMAND',
    'NPM_CONFIG_USERCONFIG',
    'NPM_CONFIG_REGISTRY',
    'NODE_AUTH_TOKEN',
    'NODE_OPTIONS',
    'NODE_PATH',
    'HTTP_PROXY',
    'HTTPS_PROXY',
    'NO_PROXY',
    'CURL_CA_BUNDLE',
    'NODE_TLS_REJECT_UNAUTHORIZED',
    'DATABASE_URL',
    'PIP_INDEX_URL',
    'UV_INDEX_URL',
    'YARN_NPM_AUTH_TOKEN',
    'NPM_TOKEN',
    'API_KEY',
    'PASSWORD',
    'HERMES_DESKTOP_REMOTE_URL',
    'HERMES_UNRELATED_CONFIG_ROUTE',
    'OPENAI_BASE_URL',
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
  const benignEnv = {
    PATH: 'safe-path',
    PATHEXT: '.COM;.EXE;.BAT;.CMD',
    SystemRoot: 'safe-system-root',
    WINDIR: 'safe-windir',
    COMSPEC: 'safe-comspec',
    USERPROFILE: 'safe-user-profile',
    HOMEDRIVE: 'safe-home-drive',
    HOMEPATH: 'safe-home-path',
    HOME: 'safe-home',
    APPDATA: 'safe-appdata',
    LOCALAPPDATA: 'safe-localappdata',
    PROGRAMDATA: 'safe-programdata',
    TEMP: 'safe-temp',
    TMP: 'safe-tmp',
    TMPDIR: 'safe-tmpdir'
  }
  const secretMarker = 'never-log-credential-route-value'
  const sensitiveEnv = Object.fromEntries(sensitiveNames.map(name => [name, secretMarker]))
  const spec = createDesktopLaunchSpec({
    executable: 'Hermes.exe',
    baseEnv: {
      ...benignEnv,
      ...sensitiveEnv
    }
  })

  try {
    for (const [name, value] of Object.entries(benignEnv)) {
      assert.equal(spec.env[name], value, name)
    }
    assert.deepEqual(
      sensitiveNames.filter(name => Object.hasOwn(spec.env, name)),
      []
    )
    assert.doesNotMatch(JSON.stringify(spec.env), new RegExp(secretMarker))
    assert.equal(spec.env.HERMES_DESKTOP_BOOT_FAKE, undefined)
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('launch spec strips suffix variants and all provider base URLs', () => {
  const spec = createDesktopLaunchSpec({
    executable: 'Hermes.exe',
    baseEnv: {
      PATH: 'safe-path',
      CUSTOM_PASSWORD: 'secret',
      CUSTOM_SECRET: 'secret',
      CUSTOM_ACCESS_KEY: 'secret',
      CUSTOM_PRIVATE_KEY: 'secret',
      CUSTOM_CREDENTIALS: 'secret',
      CUSTOM_CREDENTIALS_FILE: 'secret',
      HERMES_PROFILE: 'production',
      HERMES_PROFILE_NAME: 'production',
      NVIDIA_BASE_URL: 'https://production.example',
      NOUS_INFERENCE_BASE_URL: 'https://production.example'
    }
  })

  try {
    assert.deepEqual(Object.keys(spec.env), [
      'PATH',
      'HERMES_DESKTOP_USER_DATA_DIR',
      'HERMES_HOME',
      'HERMES_DESKTOP_APP_NAME',
      'HERMES_DESKTOP_CWD',
      'HERMES_DESKTOP_IGNORE_EXISTING'
    ])
  } finally {
    cleanupUnlaunchedDesktopSpec(spec)
  }
})

test('existing Desktop inspection requires explicit operator opt-in', () => {
  assert.throws(
    () => verifierLib.assertExistingDesktopInspectionAllowed({}),
    /HERMES_DESKTOP_ALLOW_EXISTING=1/
  )
  assert.throws(
    () => verifierLib.assertExistingDesktopInspectionAllowed({
      HERMES_DESKTOP_ALLOW_EXISTING: 'true'
    }),
    /HERMES_DESKTOP_ALLOW_EXISTING=1/
  )
  assert.doesNotThrow(
    () => verifierLib.assertExistingDesktopInspectionAllowed({
      HERMES_DESKTOP_ALLOW_EXISTING: '1'
    })
  )
})

test('DevToolsActivePort parser accepts only a nonzero loopback port record', () => {
  assert.deepEqual(parseDevToolsActivePort('43891\n/devtools/browser/run-id\n'), {
    port: 43891,
    browserWebSocketPath: '/devtools/browser/run-id',
    address: '127.0.0.1'
  })

  for (const contents of [
    '',
    '0\n/devtools/browser/run-id\n',
    '65536\n/devtools/browser/run-id\n',
    'not-a-port\n/devtools/browser/run-id\n',
    '43891\nhttp://0.0.0.0/devtools/browser/run-id\n'
  ]) {
    assert.throws(() => parseDevToolsActivePort(contents), /DevToolsActivePort/)
  }
})


test('executable provenance accepts only current-worktree dist/release artifacts and rejects outside paths', () => {
  const repoRoot = mkdtempSync(join(tmpdir(), 'desktop-verifier-provenance-'))
  const releaseDir = join(repoRoot, 'apps', 'desktop', 'release', 'win-unpacked')
  const distDir = join(repoRoot, 'apps', 'desktop', 'dist')
  const productionDir = join(repoRoot, '..', `installed-hermes-${Date.now()}`)
  const releaseExecutable = join(releaseDir, 'Hermes.exe')
  const distExecutable = join(distDir, 'Hermes')
  const productionExecutable = join(productionDir, 'Hermes.exe')

  try {
    mkdirSync(releaseDir, { recursive: true })
    mkdirSync(distDir, { recursive: true })
    mkdirSync(productionDir, { recursive: true })
    writeFileSync(releaseExecutable, 'release', 'utf8')
    writeFileSync(distExecutable, 'dist', 'utf8')
    writeFileSync(productionExecutable, 'production', 'utf8')

    assert.equal(
      assertDesktopExecutableProvenance(releaseExecutable, { repoRoot }),
      releaseExecutable
    )
    assert.equal(
      assertDesktopExecutableProvenance(distExecutable, { repoRoot }),
      distExecutable
    )
    assert.throws(
      () => assertDesktopExecutableProvenance(productionExecutable, { repoRoot }),
      /outside the current Git worktree Desktop build output roots/
    )
  } finally {
    rmSync(repoRoot, { recursive: true, force: true })
    rmSync(productionDir, { recursive: true, force: true })
  }
})

test('executable provenance rejects a symlink escape from a worktree build root', () => {
  const repoRoot = mkdtempSync(join(tmpdir(), 'desktop-verifier-symlink-'))
  const releaseDir = join(repoRoot, 'apps', 'desktop', 'release')
  const outsideDir = join(repoRoot, '..', `outside-desktop-${Date.now()}`)
  const outsideExecutable = join(outsideDir, 'Hermes.exe')
  const linkedDir = join(releaseDir, 'linked-output')

  try {
    mkdirSync(releaseDir, { recursive: true })
    mkdirSync(outsideDir, { recursive: true })
    writeFileSync(outsideExecutable, 'outside', 'utf8')
    symlinkSync(outsideDir, linkedDir, process.platform === 'win32' ? 'junction' : 'dir')

    assert.throws(
      () => assertDesktopExecutableProvenance(join(linkedDir, 'Hermes.exe'), { repoRoot }),
      /outside the current Git worktree Desktop build output roots/
    )
  } finally {
    rmSync(repoRoot, { recursive: true, force: true })
    rmSync(outsideDir, { recursive: true, force: true })
  }
})

test('owned cleanup is idempotent and removes only its generated root', async () => {
  const parent = mkdtempSync(join(tmpdir(), 'desktop-verifier-parent-'))
  const spec = createNodeProcessSpec({ tempBaseDir: parent })
  const sentinel = join(parent, 'unrelated-sentinel.txt')
  writeFileSync(sentinel, 'keep', 'utf8')

  try {
    const owned = await launchGenericOwnedDesktop(spec)

    await owned.cleanup()
    await owned.cleanup()

    assert.equal(existsSync(spec.paths.root), false)
    assert.equal(readFileSync(sentinel, 'utf8'), 'keep')
  } finally {
    rmSync(parent, { recursive: true, force: true })
  }
})

test('synthetic Windows controller error retains its generated root and reports the exact path', {
  skip: process.platform !== 'win32'
}, async () => {
  const spec = createNodeProcessSpec({
    executable: join(tmpdir(), `missing-hermes-${Date.now()}.exe`)
  })

  try {
    await assert.rejects(
      launchGenericOwnedDesktop(spec, { launchError: 'synthetic missing target' }),
      error => {
        assert.match(error.message, /controller|protocol|launch|cleanup/i)
        assert.match(error.message, new RegExp(spec.paths.root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
        return true
      }
    )
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('POSIX startup failure removes the unlaunched owned root', {
  skip: process.platform === 'win32'
}, async () => {
  const spec = createDesktopLaunchSpec({
    executable: join(tmpdir(), `missing-hermes-${Date.now()}`)
  })

  await assert.rejects(
    runDesktopSmoke(spec, { portTimeoutMs: 50, pollIntervalMs: 10 }),
    /launch|ENOENT/i
  )
  assert.equal(existsSync(spec.paths.root), false)
})

test('readiness failure cleans up the owned child and generated root', async () => {
  const spec = createNodeProcessSpec()
  let child

  await assert.rejects(
    runGenericDesktopSmoke(spec, {
      onOwned: owned => {
        child = owned.child
      },
      portTimeoutMs: 50,
      pollIntervalMs: 10
    }),
    /DevToolsActivePort/
  )

  assert.equal(existsSync(spec.paths.root), false)
  await waitForExit(child)
})

test('successful smoke reads its owned port file, observes a CDP page, prints receipt before cleanup', async () => {
  const spec = createNodeProcessSpec()
  const portFile = join(spec.paths.userDataDir, 'DevToolsActivePort')
  writeFileSync(portFile, '43891\n/devtools/browser/owned-run\n', 'utf8')
  let child
  let observedReceipt = null

  const receipt = await runGenericDesktopSmoke(spec, {
    onOwned: owned => {
      child = owned.child
    },
    fetchImpl: async url => {
      assert.equal(url, 'http://127.0.0.1:43891/json/list')

      return {
        ok: true,
        status: 200,
        json: async () => [{
          type: 'page',
          webSocketDebuggerUrl: 'ws://127.0.0.1:43891/devtools/page/owned-run'
        }]
      }
    },
    onReceipt: value => {
      assert.equal(existsSync(spec.paths.root), true)
      assert.equal(child.exitCode, null)
      observedReceipt = value
    }
  })

  assert.deepEqual(receipt, observedReceipt)
  assert.equal(receipt.pid, child.pid)
  assert.equal(receipt.port, 43891)
  assert.deepEqual(receipt.paths, spec.paths)
  assert.equal(existsSync(spec.paths.root), false)
  await waitForExit(child)
})

test('owned-PID cleanup leaves an unrelated sentinel process alive', async () => {
  const sentinelSpec = createNodeProcessSpec()
  const sentinel = await launchGenericOwnedDesktop(sentinelSpec)
  const spec = createNodeProcessSpec()
  const owned = await launchGenericOwnedDesktop(spec)

  try {
    await owned.cleanup()
    assert.equal(sentinel.child.exitCode, null)
    assert.equal(sentinel.child.signalCode, null)
  } finally {
    await sentinel.cleanup()
  }
})

test('POSIX cleanup terminates the owned parent and grandchild group but not an unrelated sentinel', {
  skip: process.platform === 'win32'
}, async () => {
  const parent = mkdtempSync(join(tmpdir(), 'desktop-verifier-posix-tree-'))
  const grandchildPidFile = join(parent, 'grandchild.pid')
  const grandchildReadyFile = join(parent, 'grandchild.ready')
  const parentTerminatedFile = join(parent, 'parent-terminated')
  const grandchildTerminatedFile = join(parent, 'grandchild-terminated')
  const grandchildScript = [
    "const fs = require('node:fs')",
    `process.on('SIGTERM', () => { fs.writeFileSync(${JSON.stringify(grandchildTerminatedFile)}, 'yes'); process.exit(0) })`,
    `fs.writeFileSync(${JSON.stringify(grandchildReadyFile)}, 'ready')`,
    'setInterval(() => {}, 1_000)'
  ].join(';')
  const parentScript = [
    "const fs = require('node:fs')",
    "const { spawn } = require('node:child_process')",
    `const child = spawn(process.execPath, ['-e', ${JSON.stringify(grandchildScript)}], { stdio: 'ignore' })`,
    `fs.writeFileSync(${JSON.stringify(grandchildPidFile)}, String(child.pid))`,
    `process.on('SIGTERM', () => { fs.writeFileSync(${JSON.stringify(parentTerminatedFile)}, 'yes'); process.exit(0) })`,
    'setInterval(() => {}, 1_000)'
  ].join(';')
  const sentinelSpec = createNodeProcessSpec({ tempBaseDir: parent })
  const sentinel = await launchOwnedDesktop(sentinelSpec)
  const treeSpec = createNodeProcessSpec({ script: parentScript, tempBaseDir: parent })
  const tree = await launchOwnedDesktop(treeSpec)

  try {
    await waitForFile(grandchildPidFile)
    await waitForFile(grandchildReadyFile)
    await tree.cleanup()
    assert.equal(readFileSync(parentTerminatedFile, 'utf8'), 'yes')
    assert.equal(readFileSync(grandchildTerminatedFile, 'utf8'), 'yes')
    assert.equal(sentinel.child.exitCode, null)
    assert.equal(sentinel.child.signalCode, null)
  } finally {
    await sentinel.cleanup()
    rmSync(parent, { recursive: true, force: true })
  }
})
