import { spawn as nodeSpawn } from 'node:child_process'
import { once } from 'node:events'

const DEFAULT_SHUTDOWN_TIMEOUT_MS = 1_000

function assertPositiveTimeout(timeoutMs, label) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error(`${label} must be a positive integer`)
  }
}

async function waitForDirectChildExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return child.exitCode
  }

  let timer

  try {
    const [code] = await Promise.race([
      once(child, 'exit'),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          `owned verifier test child ${child.pid} did not exit within ${timeoutMs}ms`
        )), timeoutMs)
      })
    ])

    return code
  } finally {
    clearTimeout(timer)
  }
}

async function waitForDirectChildSpawn(child) {
  if (Number.isSafeInteger(child.pid) && child.pid > 0) {
    return
  }

  await new Promise((resolve, reject) => {
    const cleanupListeners = () => {
      child.removeListener('spawn', onSpawn)
      child.removeListener('error', onError)
    }
    const onSpawn = () => {
      cleanupListeners()
      resolve()
    }
    const onError = error => {
      cleanupListeners()
      reject(error)
    }

    child.once('spawn', onSpawn)
    child.once('error', onError)
  })
}

// This test-only helper owns exactly the direct child it creates. It never
// discovers, signals, or claims authority over descendants or unrelated PIDs.
// Tests that need descendant semantics must exercise the production Job/PGID
// verifier path instead.
export async function launchDirectOwnedVerifierTestProcess(command, args, {
  shutdownTimeoutMs = DEFAULT_SHUTDOWN_TIMEOUT_MS,
  spawnOptions = {}
} = {}) {
  if (typeof command !== 'string' || command.trim() === '') {
    throw new Error('owned verifier test process command must be non-empty')
  }
  if (!Array.isArray(args) || args.some(argument => typeof argument !== 'string')) {
    throw new Error('owned verifier test process args must be strings')
  }
  assertPositiveTimeout(shutdownTimeoutMs, 'owned verifier test shutdown timeout')

  const child = nodeSpawn(command, args, spawnOptions)
  await waitForDirectChildSpawn(child)
  let cleanupPromise

  return {
    child,
    waitForExit: timeoutMs => {
      assertPositiveTimeout(timeoutMs, 'owned verifier test exit timeout')
      return waitForDirectChildExit(child, timeoutMs)
    },
    cleanup: () => {
      if (!cleanupPromise) {
        cleanupPromise = (async () => {
          if (child.exitCode === null && child.signalCode === null) {
            child.kill()
          }
          await waitForDirectChildExit(child, shutdownTimeoutMs)
        })()
      }

      return cleanupPromise
    }
  }
}
