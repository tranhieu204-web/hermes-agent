import assert from 'node:assert/strict'
import { test } from 'vitest'

import { resolveBackendWithOptionalRootPin } from './backend-root'

function resolve(overrides: Record<string, string | undefined>) {
  let automatic = 0
  const result = resolveBackendWithOptionalRootPin({
    rootOverride: overrides.root,
    commandOverride: overrides.command,
    pythonOverride: overrides.python,
    resolvePath: value => `ABS:${value}`,
    isSourceRoot: value => value === 'ABS:root',
    fileExists: value => value === 'ABS:python' || value === 'root/.venv/python',
    interpreterCandidates: () => [{ python: 'root/.venv/python', venvRoot: 'root/.venv' }],
    venvRootForPython: python => python === 'ABS:python' ? 'external-venv' : null,
    createPinnedBackend: selection => ({ kind: 'pinned', ...selection }),
    resolveAutomatic: () => { automatic += 1; return { kind: 'automatic' } }
  })
  return { automatic, result }
}

test('an explicit canonical root never falls through to automatic resolution', () => {
  const value = resolve({ root: 'root' })
  assert.equal(value.automatic, 0)
  assert.deepEqual(value.result, {
    kind: 'pinned', root: 'ABS:root', python: 'root/.venv/python', venvRoot: 'root/.venv'
  })
})

test('invalid pins fail closed', () => {
  assert.throws(() => resolve({ root: 'wrong' }), /not a Hermes source root/)
  assert.throws(() => resolve({ root: 'root', command: 'other' }), /conflicts/)
  assert.throws(() => resolve({ root: 'root', python: 'missing' }), /does not exist/)
})

test('automatic resolution remains available only without a root pin', () => {
  const value = resolve({})
  assert.equal(value.automatic, 1)
  assert.deepEqual(value.result, { kind: 'automatic' })
})
