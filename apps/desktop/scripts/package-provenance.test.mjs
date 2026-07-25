import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, test } from 'vitest'

import { readProvenanceManifest, validatePackageProvenance, writeProvenanceManifest } from './package-provenance.mjs'

const HEAD = 'a'.repeat(40)
const MARKER = 'Antigravity · Gemini 3.1 Pro High'
const RENDERER_RELATIVE_PATH = 'resources/app.asar.unpacked/dist/assets/antigravity.js'
const roots = []

function fixture(patch = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-provenance-'))
  roots.push(root)
  const repoRoot = path.join(root, 'worktree')
  const candidateRoot = path.join(repoRoot, 'apps', 'desktop', 'candidate', HEAD, 'win-unpacked')
  fs.mkdirSync(path.join(candidateRoot, 'resources'), { recursive: true })
  fs.writeFileSync(path.join(candidateRoot, 'Hermes.exe'), 'matched-exe')
  fs.writeFileSync(path.join(candidateRoot, 'resources', 'app.asar'), 'matched-asar')
  fs.mkdirSync(path.dirname(path.join(candidateRoot, RENDERER_RELATIVE_PATH)), { recursive: true })
  fs.writeFileSync(path.join(candidateRoot, RENDERER_RELATIVE_PATH), `renderer:${MARKER}`)
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
    ...patch
  })
}

function manifestPath(current) {
  return path.join(current.candidateRoot, 'provenance-manifest.json')
}

function readManifest(current, manifest = manifestPath(current)) {
  return readProvenanceManifest({
    manifestPath: manifest,
    candidateRoot: current.candidateRoot,
    expectedCandidateRoot: current.candidateRoot,
    expectedHead: HEAD,
    expectedRepoRoot: current.repoRoot,
    requiredMarker: MARKER
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

test('binds the unpacked renderer artifact satisfying the required marker by path and SHA-256', () => {
  const current = fixture()
  const rendererBytes = `renderer:${MARKER}`

  const result = validate(current)

  assert.deepEqual(result.files.rendererArtifacts, [
    {
      path: RENDERER_RELATIVE_PATH,
      sha256: crypto.createHash('sha256').update(rendererBytes).digest('hex')
    }
  ])
})

test('writes and independently reads back all package and renderer hash bindings', () => {
  const current = fixture()
  const provenance = validate(current)
  const written = writeProvenanceManifest(manifestPath(current), provenance)

  const readback = readManifest(current)

  assert.equal(readback.manifest.sha256, written.sha256)
  assert.deepEqual(readback.provenance.files, provenance.files)
})

test('manifest readback rejects renderer tampering after provenance generation', () => {
  const current = fixture()
  writeProvenanceManifest(manifestPath(current), validate(current))
  fs.appendFileSync(path.join(current.candidateRoot, RENDERER_RELATIVE_PATH), ':tampered')

  assert.throws(() => readManifest(current), /renderer artifact hash mismatch|tamper/i)
})

test('manifest readback preserves and verifies the existing core package hash bindings', () => {
  const current = fixture()
  writeProvenanceManifest(manifestPath(current), validate(current))
  fs.appendFileSync(path.join(current.candidateRoot, 'resources', 'app.asar'), ':tampered')

  assert.throws(() => readManifest(current), /app\.asar hash mismatch/i)
})

test('manifest readback rejects deletion of a bound renderer artifact', () => {
  const current = fixture()
  writeProvenanceManifest(manifestPath(current), validate(current))
  fs.rmSync(path.join(current.candidateRoot, RENDERER_RELATIVE_PATH))

  assert.throws(() => readManifest(current), /missing renderer artifact|renderer artifact.*missing/i)
})

test('rejects ambiguous packages with more than one marker-bearing renderer artifact', () => {
  const current = fixture()
  const duplicate = path.join(current.candidateRoot, 'resources', 'app.asar.unpacked', 'dist', 'duplicate.html')
  fs.writeFileSync(duplicate, `<script>${MARKER}</script>`)

  assert.throws(() => validate(current), /ambiguous.*renderer|renderer.*ambiguous/i)
})

test('manifest readback rejects a renderer artifact path escaping the candidate root', () => {
  const current = fixture()
  const target = manifestPath(current)
  writeProvenanceManifest(target, validate(current))
  const payload = JSON.parse(fs.readFileSync(target, 'utf8'))
  payload.files.rendererArtifacts[0].path = '../outside.js'
  fs.writeFileSync(target, `${JSON.stringify(payload, null, 2)}\n`)

  assert.throws(() => readManifest(current), /renderer artifact path|escape|outside/i)
})

test('rejects a stale or mismatched packaged HEAD', () => {
  assert.throws(() => validate(fixture({ commit: 'b'.repeat(40) })), /expected HEAD/)
})

test('rejects a dirty packaged stamp', () => {
  assert.throws(() => validate(fixture({ dirty: true })), /dirty/)
})

test('rejects fallback package provenance', () => {
  assert.throws(() => validate(fixture({ commit: '0'.repeat(40), source: 'fallback' })), /fallback|source/)
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
  const current = fixture()
  fs.writeFileSync(path.join(current.candidateRoot, RENDERER_RELATIVE_PATH), 'renderer without feature')

  assert.throws(() => validate(current), /marker/)
})

test('rejects an unmatched candidate pair when either executable or app.asar is absent', () => {
  const current = fixture()
  fs.rmSync(path.join(current.candidateRoot, 'resources', 'app.asar'))

  assert.throws(() => validate(current), /matched candidate pair|app\.asar/)
})
