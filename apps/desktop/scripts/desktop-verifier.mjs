#!/usr/bin/env node

import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  assertDesktopExecutableProvenance,
  createDesktopLaunchSpec,
  runDesktopSmoke
} from './desktop-verifier-lib.mjs'

function usage() {
  return [
    'Usage:',
    '  npm run verify:desktop:isolated -- --exe <path> --smoke [--fake-boot]',
    '',
    'Options:',
    '  --exe <path>  Worktree-built Desktop executable to verify.',
    "  --smoke       Wait for this run's CDP page, print a JSON receipt, then clean up.",
    '  --fake-boot   Enable deterministic fake boot (credentials are always stripped).',
    '  --help        Show this help.'
  ].join('\n')
}

export function parseDesktopVerifierArgs(argv) {
  const options = {
    executable: null,
    fakeBoot: false,
    help: false,
    smoke: false
  }

  for (let index = 0; index < argv.length; index++) {
    const argument = argv[index]

    if (argument === '--exe') {
      const value = argv[++index]

      if (!value || value.startsWith('--')) {
        throw new Error('--exe requires a path')
      }

      options.executable = resolve(value)
    } else if (argument === '--smoke') {
      options.smoke = true
    } else if (argument === '--fake-boot') {
      options.fakeBoot = true
    } else if (argument === '--help' || argument === '-h') {
      options.help = true
    } else {
      throw new Error(`unknown desktop verifier option: ${argument}`)
    }
  }

  return options
}

function formatError(error, indent = '') {
  const lines = [`${indent}${error instanceof Error ? error.message : String(error)}`]

  if (error instanceof AggregateError) {
    for (const nested of error.errors) {
      lines.push(formatError(nested, `${indent}  `))
    }
  }

  return lines.join('\n')
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseDesktopVerifierArgs(argv)

  if (options.help) {
    console.log(usage())

    return
  }

  if (!options.executable) {
    throw new Error(`--exe is required\n${usage()}`)
  }

  if (!options.smoke) {
    throw new Error(`--smoke is required\n${usage()}`)
  }

  options.executable = assertDesktopExecutableProvenance(options.executable)

  const spec = createDesktopLaunchSpec({
    executable: options.executable,
    fakeBoot: options.fakeBoot
  })
  const controller = new AbortController()
  let activeCleanup = null
  let interruptedSignal = null

  const interrupt = signalName => {
    if (interruptedSignal) {
      return
    }

    interruptedSignal = signalName
    controller.abort(new Error(`desktop verification interrupted by ${signalName}`))

    if (activeCleanup) {
      activeCleanup().catch(error => {
        console.error(`Desktop verifier owned cleanup failed:\n${formatError(error)}`)
      })
    }
  }
  const onSigint = () => interrupt('SIGINT')
  const onSigterm = () => interrupt('SIGTERM')

  process.once('SIGINT', onSigint)
  process.once('SIGTERM', onSigterm)

  try {
    try {
      await runDesktopSmoke(spec, {
        signal: controller.signal,
        onOwned: owned => {
          activeCleanup = owned.cleanup
        },
        onReceipt: receipt => {
          console.log(JSON.stringify(receipt))
        }
      })
    } catch (error) {
      if (!interruptedSignal) {
        throw error
      }

      console.error(formatError(error))
    }
  } finally {
    process.removeListener('SIGINT', onSigint)
    process.removeListener('SIGTERM', onSigterm)
  }

  if (interruptedSignal) {
    process.exitCode = interruptedSignal === 'SIGINT' ? 130 : 143
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`Desktop verifier failed:\n${formatError(error)}`)
    process.exitCode = 1
  })
}
