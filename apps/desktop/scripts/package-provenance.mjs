import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { extractFile, listPackage } from '@electron/asar'

import { isMain } from './utils.mjs'

const SHA40 = /^[a-f0-9]{40}$/i
const RENDERER_FILE = /\.(?:html|js)$/i

function comparablePath(value) {
  const resolved = path.resolve(String(value || ''))
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved
}

function requireExactPath(actual, expected, label) {
  if (!actual || !expected || comparablePath(actual) !== comparablePath(expected)) {
    throw new Error(`${label} mismatch: expected ${expected}, received ${actual}`)
  }
}

function sha256File(filePath) {
  const hash = crypto.createHash('sha256')
  hash.update(fs.readFileSync(filePath))
  return hash.digest('hex')
}

function requireNonemptyFile(filePath, label) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile() || fs.statSync(filePath).size === 0) {
    throw new Error(`matched candidate pair is incomplete: missing ${label} at ${filePath}`)
  }
}

function rendererTextFromUnpacked(resourcesRoot) {
  const distRoot = path.join(resourcesRoot, 'app.asar.unpacked', 'dist')
  if (!fs.existsSync(distRoot)) return ''
  const chunks = []
  const pending = [distRoot]
  while (pending.length > 0) {
    const current = pending.pop()
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const target = path.join(current, entry.name)
      if (entry.isDirectory()) {
        pending.push(target)
      } else if (RENDERER_FILE.test(entry.name)) {
        chunks.push(fs.readFileSync(target, 'utf8'))
      }
    }
  }
  return chunks.join('\n')
}

export function readPackagedRendererText(candidateRoot) {
  const resourcesRoot = path.join(candidateRoot, 'resources')
  const unpacked = rendererTextFromUnpacked(resourcesRoot)
  if (unpacked) return unpacked

  const asarPath = path.join(resourcesRoot, 'app.asar')
  const chunks = []
  for (const entry of listPackage(asarPath)) {
    const normalized = entry.replace(/\\/g, '/').replace(/^\/+/, '')
    if (!normalized.startsWith('dist/') || !RENDERER_FILE.test(normalized)) continue
    chunks.push(extractFile(asarPath, normalized).toString('utf8'))
  }
  return chunks.join('\n')
}

export function validatePackageProvenance({
  candidateRoot,
  expectedCandidateRoot,
  expectedHead,
  expectedRepoRoot,
  requiredMarker,
  rendererText
}) {
  requireExactPath(candidateRoot, expectedCandidateRoot, 'candidate root')
  if (!SHA40.test(String(expectedHead || '')) || /^0{40}$/.test(expectedHead)) {
    throw new Error(`expected HEAD must be one non-fallback 40-character commit: ${expectedHead}`)
  }

  const hermesExe = path.join(candidateRoot, 'Hermes.exe')
  const appAsar = path.join(candidateRoot, 'resources', 'app.asar')
  const stampPath = path.join(candidateRoot, 'resources', 'install-stamp.json')
  requireNonemptyFile(hermesExe, 'Hermes.exe')
  requireNonemptyFile(appAsar, 'resources/app.asar')
  requireNonemptyFile(stampPath, 'resources/install-stamp.json')

  let stamp
  try {
    stamp = JSON.parse(fs.readFileSync(stampPath, 'utf8'))
  } catch (error) {
    throw new Error(`install stamp is not valid JSON: ${error.message}`)
  }
  if (!['local', 'ci'].includes(stamp.source) || /^0{40}$/.test(stamp.commit)) {
    throw new Error(`packaged stamp source is fallback or unsupported: ${stamp.source}`)
  }
  if (stamp.commit !== expectedHead) {
    throw new Error(`packaged stamp does not equal expected HEAD: ${stamp.commit} != ${expectedHead}`)
  }
  if (stamp.dirty !== false) {
    throw new Error('packaged stamp is dirty')
  }
  requireExactPath(stamp.repoRoot, expectedRepoRoot, 'worktree root')

  const packagedRenderer =
    rendererText === undefined ? readPackagedRendererText(candidateRoot) : String(rendererText)
  if (!requiredMarker || !packagedRenderer.includes(requiredMarker)) {
    throw new Error(`packaged renderer is missing required marker: ${requiredMarker}`)
  }

  return {
    schemaVersion: 1,
    commit: stamp.commit,
    source: stamp.source,
    repoRoot: path.resolve(stamp.repoRoot),
    candidateRoot: path.resolve(candidateRoot),
    requiredMarker,
    files: {
      hermesExe: {
        path: 'Hermes.exe',
        sha256: sha256File(hermesExe)
      },
      appAsar: {
        path: 'resources/app.asar',
        sha256: sha256File(appAsar)
      },
      installStamp: {
        path: 'resources/install-stamp.json',
        sha256: sha256File(stampPath)
      }
    }
  }
}

export function writeProvenanceManifest(manifestPath, provenance) {
  const payload = {
    ...provenance,
    validatedAt: new Date().toISOString()
  }
  fs.writeFileSync(manifestPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
  return {
    path: path.resolve(manifestPath),
    sha256: sha256File(manifestPath)
  }
}

function option(name) {
  const index = process.argv.indexOf(`--${name}`)
  return index >= 0 ? process.argv[index + 1] : undefined
}

function main() {
  const candidateRoot = option('candidate-root')
  const expectedCandidateRoot = option('expected-candidate-root')
  const expectedHead = option('expected-head')
  const expectedRepoRoot = option('expected-repo-root')
  const requiredMarker = option('required-marker')
  const manifestPath = option('write-manifest')
  const provenance = validatePackageProvenance({
    candidateRoot,
    expectedCandidateRoot,
    expectedHead,
    expectedRepoRoot,
    requiredMarker
  })
  const manifest = manifestPath ? writeProvenanceManifest(manifestPath, provenance) : null
  process.stdout.write(`${JSON.stringify({ ...provenance, manifest }, null, 2)}\n`)
}

if (isMain(import.meta.url)) {
  main()
}
