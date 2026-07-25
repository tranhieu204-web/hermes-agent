import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, test } from 'vitest'

import { validatePackageProvenance } from './package-provenance.mjs'

const HEAD = 'a'.repeat(40)
const MARKER = 'Antigravity · Gemini 3.1 Pro High'
const roots = []

function fixture(patch = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-provenance-'))
  roots.push(root)
  const repoRoot = path.join(root, 'worktree')
  const candidateRoot = path.join(repoRoot, 'apps', 'desktop', 'candidate', HEAD, 'win-unpacked')
  fs.mkdirSync(path.join(candidateRoot, 'resources'), { recursive: true })
  fs.writeFileSync(path.join(candidateRoot, 'Hermes.exe'), 'matched-exe')
  fs.writeFileSync(path.join(candidateRoot, 'resources', 'app.asar'), 'matched-asar')
  fs.writeFileSync(
    path.join(candidateRoot, 'resources', 'install-stamp.json'),
    JSON.stringify({
      schemaVersion: 1,
      commit: HEAD,
      branch: 'fix/antigravity',
      builtAt: '2026-07-25T00:00:00.000Z',
      dirty: false,
      source: 'local',
      repoRoot,
      ...patch
    })
  )
  return { candidateRoot, repoRoot }
}

function validate({ candidateRoot, repoRoot }, patch = {}) {
  return validatePackageProvenance({
    candidateRoot,
    expectedCandidateRoot: candidateRoot,
    expectedHead: HEAD,
    expectedRepoRoot: repoRoot,
    requiredMarker: MARKER,
    rendererText: `renderer:${MARKER}`,
    ...patch
  })
}

afterEach(() => {
  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('accepts one clean matched candidate pair from the intended worktree', () => {
  const result = validate(fixture())

  assert.equal(result.commit, HEAD)
  assert.match(result.files.hermesExe.sha256, /^[a-f0-9]{64}$/)
  assert.match(result.files.appAsar.sha256, /^[a-f0-9]{64}$/)
})

test('rejects a stale or mismatched packaged HEAD', () => {
  assert.throws(() => validate(fixture({ commit: 'b'.repeat(40) })), /expected HEAD/)
})

test('rejects a dirty packaged stamp', () => {
  assert.throws(() => validate(fixture({ dirty: true })), /dirty/)
})

test('rejects fallback package provenance', () => {
  assert.throws(
    () => validate(fixture({ commit: '0'.repeat(40), source: 'fallback' })),
    /fallback|source/
  )
})

test('rejects a candidate outside the exact intended candidate root', () => {
  const current = fixture()
  assert.throws(
    () => validate(current, { expectedCandidateRoot: path.join(current.repoRoot, 'wrong-candidate') }),
    /candidate root/
  )
})

test('rejects a stamp built from the wrong worktree root', () => {
  const current = fixture({ repoRoot: 'C:/wrong/worktree' })
  assert.throws(() => validate(current), /worktree root/)
})

test('rejects a package whose renderer lacks the required Antigravity marker', () => {
  assert.throws(() => validate(fixture(), { rendererText: 'renderer without feature' }), /marker/)
})

test('rejects an unmatched candidate pair when either executable or app.asar is absent', () => {
  const current = fixture()
  fs.rmSync(path.join(current.candidateRoot, 'resources', 'app.asar'))

  assert.throws(() => validate(current), /matched candidate pair|app\.asar/)
})
