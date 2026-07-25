import assert from 'node:assert/strict'
import { spawnSync as testSpawnSync } from 'node:child_process'
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
import { EventEmitter, once } from 'node:events'
import { test } from 'node:test'

import * as verifierLib from './desktop-verifier-lib.mjs'
import {
  assertDesktopExecutableProvenance,
  cleanupUnlaunchedDesktopSpec,
  createDesktopLaunchSpec,
  discoverWindowsDescendantPids,
  launchOwnedDesktop,
  parseDevToolsActivePort,
  runDesktopSmoke
} from './desktop-verifier-lib.mjs'

function createNodeProcessSpec(options = {}) {
  return createDesktopLaunchSpec({
    executable: process.execPath,
    executableArgs: ['-e', options.script ?? 'setInterval(() => {}, 1_000)', '--'],
    tempBaseDir: options.tempBaseDir
  })
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

async function waitForWindowsPidExit(pid, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() <= deadline) {
    if (!discoverWindowsDescendantPids(pid, { includeOwnedPid: true }).includes(pid)) {
      return
    }

    await new Promise(resolve => setTimeout(resolve, 20))
  }

  throw new Error(`owned Windows PID ${pid} did not exit`)
}

function terminateExactOwnedTestPid(pid) {
  testSpawnSync('taskkill.exe', ['/PID', String(pid), '/T', '/F'], {
    encoding: 'utf8',
    windowsHide: true
  })
}

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

test('launch spec exact 12-name probe strips credentials, profiles, routes, and provider URLs', () => {
  const sensitiveNames = [
    'API_KEY',
    'PASSWORD',
    'SECRET',
    'ACCESS_KEY',
    'PRIVATE_KEY',
    'CREDENTIALS',
    'AWS_PROFILE',
    'AWS_SHARED_CREDENTIALS_FILE',
    'HERMES_DESKTOP_REMOTE_URL',
    'HERMES_DESKTOP_REMOTE_TOKEN',
    'OPENAI_BASE_URL',
    'ANTHROPIC_BASE_URL'
  ]
  const sensitiveEnv = Object.fromEntries(sensitiveNames.map(name => [name, `secret-${name}`]))
  const spec = createDesktopLaunchSpec({
    executable: 'Hermes.exe',
    baseEnv: {
      PATH: 'safe-path',
      SYSTEMROOT: 'safe-system-root',
      ...sensitiveEnv
    }
  })

  try {
    assert.equal(spec.env.PATH, 'safe-path')
    assert.equal(spec.env.SYSTEMROOT, 'safe-system-root')
    assert.deepEqual(
      sensitiveNames.filter(name => Object.hasOwn(spec.env, name)),
      []
    )
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

test('Windows descendant census binds strict creation identity and rejects PID reuse or late children', () => {
  const identityLedger = new Map()
  const parentIdentity = '/Date(1700000000000)/'
  const childIdentity = '/Date(1700000001000)/'
  const initial = discoverWindowsDescendantPids(4100, {
    allowNewIdentities: true,
    identityLedger,
    includeOwnedPid: true,
    spawnSyncImpl: () => ({
      status: 0,
      stdout: JSON.stringify([
        { ProcessId: 4100, ParentProcessId: 1, CreationDate: parentIdentity },
        { ProcessId: 4101, ParentProcessId: 4100, CreationDate: childIdentity }
      ]),
      stderr: ''
    })
  })

  assert.deepEqual(initial, [4100, 4101])
  assert.deepEqual([...identityLedger], [
    [4100, parentIdentity],
    [4101, childIdentity]
  ])
  assert.throws(
    () => discoverWindowsDescendantPids(4101, {
      identityLedger,
      includeOwnedPid: true,
      spawnSyncImpl: () => ({
        status: 0,
        stdout: JSON.stringify({
          ProcessId: 4101,
          ParentProcessId: 9999,
          CreationDate: '/Date(1700000002000)/'
        }),
        stderr: ''
      })
    }),
    /changed creation identity/
  )
  assert.throws(
    () => discoverWindowsDescendantPids(4101, {
      identityLedger,
      includeOwnedPid: true,
      spawnSyncImpl: () => ({
        status: 0,
        stdout: JSON.stringify([
          { ProcessId: 4101, ParentProcessId: 9999, CreationDate: childIdentity },
          { ProcessId: 4102, ParentProcessId: 4101, CreationDate: '/Date(1700000003000)/' }
        ]),
        stderr: ''
      })
    }),
    /unverified descendant PID 4102/
  )

  for (const CreationDate of [
    undefined,
    null,
    1700000000000,
    {},
    [],
    '',
    '   ',
    'parent-created',
    '/Date(not-a-number)/'
  ]) {
    assert.throws(
      () => discoverWindowsDescendantPids(4200, {
        allowNewIdentities: true,
        identityLedger: new Map(),
        includeOwnedPid: true,
        spawnSyncImpl: () => ({
          status: 0,
          stdout: JSON.stringify({
            ProcessId: 4200,
            ParentProcessId: 1,
            CreationDate
          }),
          stderr: ''
        })
      }),
      /invalid creation identity|has no creation identity/
    )
  }
})

test('Windows cleanup kills only the exact owned child handle and proves ledger descendants exited', async () => {
  const spec = createNodeProcessSpec()
  let parentExited = false
  let descendantsAlive = true
  let handleKillCalls = 0
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill() {
      handleKillCalls += 1
      setImmediate(() => {
        parentExited = true
        descendantsAlive = false
        fakeChild.exitCode = 1
        fakeChild.emit('exit', 1, null)
      })
      return true
    }
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: (pid, { includeOwnedPid = false } = {}) => {
      if (parentExited || !descendantsAlive) {
        return []
      }
      if (pid === 4100) {
        return includeOwnedPid ? [4100, 4101, 4102] : [4101, 4102]
      }
      return includeOwnedPid ? [pid] : []
    },
    spawnSyncImpl: () => {
      throw new Error('Windows production cleanup must not invoke taskkill')
    }
  })

  await owned.cleanup()

  assert.equal(handleKillCalls, 1)
  assert.equal(existsSync(spec.paths.root), false)
})

test('Windows cleanup requires the parent inside the census snapshot itself', async () => {
  const spec = createNodeProcessSpec()
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: (_pid, { includeOwnedPid = false } = {}) => {
      assert.equal(includeOwnedPid, true)
      fakeChild.exitCode = 0
      return []
    },
    spawnSyncImpl: () => {
      throw new Error('taskkill must not run without a parent-present census')
    }
  })

  try {
    await assert.rejects(owned.cleanup(), /exited during the pre-termination descendant census/)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup rejects when the child exits after its row was observed', async () => {
  const spec = createNodeProcessSpec()
  let handleKillCalls = 0
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill() {
      handleKillCalls += 1
      return true
    }
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: () => {
      fakeChild.exitCode = 0
      return [4100]
    },
    spawnSyncImpl: () => {
      throw new Error('taskkill must not run after parent exit')
    }
  })

  try {
    await assert.rejects(owned.cleanup(), /exited after the pre-termination descendant census/)
    assert.equal(handleKillCalls, 0)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup fails closed when the owned child handle refuses termination', async () => {
  const spec = createNodeProcessSpec()
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill() {
      fakeChild.exitCode = 0
      return false
    }
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: () => [4100],
    spawnSyncImpl: () => {
      throw new Error('taskkill must never substitute for the owned child handle')
    }
  })

  try {
    await assert.rejects(owned.cleanup(), /child handle refused termination/)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup rejects an unledgered late child without killing it', async () => {
  const spec = createNodeProcessSpec()
  let parentExited = false
  let lateChildAlive = true
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill() {
      setImmediate(() => {
        parentExited = true
        fakeChild.exitCode = 0
        fakeChild.emit('exit', 0, null)
      })
      return true
    }
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: (_pid, { includeOwnedPid = false } = {}) => {
      if (!parentExited) {
        return includeOwnedPid ? [4100] : []
      }
      return lateChildAlive ? [4101] : []
    },
    spawnSyncImpl: () => {
      lateChildAlive = false
      throw new Error('unledgered late child must not be killed')
    }
  })

  try {
    await assert.rejects(owned.cleanup(), /unverified descendant PID 4101/)
    assert.equal(lateChildAlive, true)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup preserves the root when a ledger descendant remains alive', async () => {
  const spec = createNodeProcessSpec()
  let parentExited = false
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill() {
      setImmediate(() => {
        parentExited = true
        fakeChild.exitCode = 1
        fakeChild.emit('exit', 1, null)
      })
      return true
    }
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: (pid, { includeOwnedPid = false } = {}) => {
      if (pid === 4100) {
        if (parentExited) {
          return []
        }
        return includeOwnedPid ? [4100, 4101] : [4101]
      }
      return pid === 4101 && includeOwnedPid ? [4101] : []
    },
    spawnSyncImpl: () => {
      throw new Error('ledger descendants must not be killed by PID')
    }
  })

  try {
    await assert.rejects(owned.cleanup(), /left descendant PIDs: 4101/)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup fails closed when handle termination does not produce child exit', async () => {
  const spec = createNodeProcessSpec()
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: null,
    signalCode: null,
    kill: () => true
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild,
    discoverWindowsDescendantPidsImpl: (pid, { includeOwnedPid = false } = {}) =>
      includeOwnedPid ? [pid] : [],
    spawnSyncImpl: () => {
      throw new Error('taskkill fallback is forbidden')
    },
    terminationTimeoutMs: 5
  })

  try {
    await assert.rejects(owned.cleanup(), /did not exit within 5ms/)
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup fails closed if the parent exits before its descendant census', async () => {
  const spec = createNodeProcessSpec()
  const fakeChild = Object.assign(new EventEmitter(), {
    pid: 4100,
    exitCode: 0,
    signalCode: null
  })
  const owned = await launchOwnedDesktop(spec, {
    platform: 'win32',
    spawnImpl: () => fakeChild
  })

  try {
    await assert.rejects(
      owned.cleanup(),
      /exited before the pre-termination descendant census/
    )
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    rmSync(spec.paths.root, { recursive: true, force: true })
  }
})

test('Windows cleanup fails closed for a multi-level orphan without a pre-exit census', {
  skip: process.platform !== 'win32'
}, async () => {
  const parent = mkdtempSync(join(tmpdir(), 'desktop-verifier-win-early-exit-'))
  const grandchildPidFile = join(parent, 'grandchild.pid')
  const intermediateScript = [
    "const fs = require('node:fs')",
    "const { spawn } = require('node:child_process')",
    "const grandchild = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' })",
    `fs.writeFileSync(${JSON.stringify(grandchildPidFile)}, String(grandchild.pid))`,
    'grandchild.unref()'
  ].join(';')
  const parentScript = [
    "const { spawn } = require('node:child_process')",
    `const child = spawn(process.execPath, ['-e', ${JSON.stringify(intermediateScript)}], { detached: true, stdio: 'ignore' })`,
    'child.unref()'
  ].join(';')
  const spec = createNodeProcessSpec({ script: parentScript, tempBaseDir: parent })
  const owned = await launchOwnedDesktop(spec)
  let grandchildPid

  try {
    await waitForFile(grandchildPidFile)
    grandchildPid = Number(readFileSync(grandchildPidFile, 'utf8'))
    await waitForExit(owned.child)

    const deadline = Date.now() + 5000
    while (Date.now() <= deadline &&
           discoverWindowsDescendantPids(owned.ownedPid).includes(grandchildPid)) {
      await new Promise(resolve => setTimeout(resolve, 20))
    }

    assert.ok(
      discoverWindowsDescendantPids(grandchildPid, { includeOwnedPid: true })
        .includes(grandchildPid)
    )
    assert.ok(!discoverWindowsDescendantPids(owned.ownedPid).includes(grandchildPid))
    await assert.rejects(
      owned.cleanup(),
      /exited before the pre-termination descendant census/
    )
    assert.equal(existsSync(spec.paths.root), true)
  } finally {
    if (Number.isSafeInteger(grandchildPid) &&
        discoverWindowsDescendantPids(grandchildPid, { includeOwnedPid: true })
          .includes(grandchildPid)) {
      terminateExactOwnedTestPid(grandchildPid)
      await waitForWindowsPidExit(grandchildPid)
    }

    rmSync(parent, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 })
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
  const sentinel = join(parent, 'unrelated-sentinel.txt')
  writeFileSync(sentinel, 'keep', 'utf8')
  const spec = createNodeProcessSpec({ tempBaseDir: parent })
  const owned = await launchOwnedDesktop(spec)

  await owned.cleanup()
  await owned.cleanup()

  assert.equal(existsSync(spec.paths.root), false)
  assert.equal(readFileSync(sentinel, 'utf8'), 'keep')
  rmSync(parent, { recursive: true, force: true })
})

test('startup failure removes the unlaunched owned root', async () => {
  const spec = createDesktopLaunchSpec({
    executable: join(tmpdir(), `missing-hermes-${Date.now()}.exe`)
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
    runDesktopSmoke(spec, {
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

  const receipt = await runDesktopSmoke(spec, {
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
  const sentinel = await launchOwnedDesktop(sentinelSpec)
  const spec = createNodeProcessSpec()
  const owned = await launchOwnedDesktop(spec)

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
  const parentTerminatedFile = join(parent, 'parent-terminated')
  const grandchildTerminatedFile = join(parent, 'grandchild-terminated')
  const grandchildScript = [
    "const fs = require('node:fs')",
    `process.on('SIGTERM', () => { fs.writeFileSync(${JSON.stringify(grandchildTerminatedFile)}, 'yes'); process.exit(0) })`,
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
