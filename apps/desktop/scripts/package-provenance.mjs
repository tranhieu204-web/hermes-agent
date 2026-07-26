import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { extractFile, listPackage } from '@electron/asar'

import { isMain } from './utils.mjs'

const SHA40 = /^[a-f0-9]{40}$/i
const SHA256 = /^[a-f0-9]{64}$/
const RENDERER_FILE = /\.(?:html|js)$/i
const PROVENANCE_SCHEMA_VERSION = 2
const MANIFEST_NAME = 'provenance-manifest.json'
const UNPACKED_RENDERER_PREFIX = 'resources/app.asar.unpacked/dist/'
const ASAR_RENDERER_PREFIX = 'resources/app.asar!/'

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

function sha256Bytes(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex')
}

function requireNonemptyFile(filePath, label) {
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile() || fs.statSync(filePath).size === 0) {
    throw new Error(`matched candidate pair is incomplete: missing ${label} at ${filePath}`)
  }
}

function requirePathInside(root, target, label) {
  const resolvedRoot = path.resolve(root)
  const resolvedTarget = path.resolve(target)
  const relative = path.relative(resolvedRoot, resolvedTarget)
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) {
    throw new Error(`${label} escapes its required root: ${target}`)
  }
  return relative.replace(/\\/g, '/')
}

function requireRealPathInside(root, target, label) {
  const realRoot = fs.realpathSync.native(root)
  const realTarget = fs.realpathSync.native(target)
  requirePathInside(realRoot, realTarget, label)
}

function unpackedRendererFiles(candidateRoot) {
  const distRoot = path.join(candidateRoot, 'resources', 'app.asar.unpacked', 'dist')
  if (!fs.existsSync(distRoot)) return []
  if (!fs.statSync(distRoot).isDirectory()) {
    throw new Error(`unpacked renderer dist is not a directory: ${distRoot}`)
  }
  requireRealPathInside(candidateRoot, distRoot, 'unpacked renderer dist path')

  const files = []
  const pending = [distRoot]
  while (pending.length > 0) {
    const current = pending.pop()
    const entries = fs
      .readdirSync(current, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))
    for (const entry of entries) {
      const target = path.join(current, entry.name)
      if (entry.isSymbolicLink()) {
        throw new Error(`renderer artifact path escape is not allowed through symlinks: ${target}`)
      }
      if (entry.isDirectory()) {
        requireRealPathInside(distRoot, target, 'renderer artifact directory')
        pending.push(target)
      } else if (RENDERER_FILE.test(entry.name)) {
        if (!entry.isFile()) {
          throw new Error(`renderer artifact is not a regular file: ${target}`)
        }
        requireRealPathInside(distRoot, target, 'renderer artifact path')
        const bytes = fs.readFileSync(target)
        files.push({
          path: requirePathInside(candidateRoot, target, 'renderer artifact path'),
          bytes
        })
      }
    }
  }
  files.sort((left, right) => left.path.localeCompare(right.path))
  return files
}

function normalizeAsarEntry(entry) {
  const slashPath = String(entry).replace(/\\/g, '/').replace(/^\/+/, '')
  const normalized = path.posix.normalize(slashPath)
  if (
    !normalized.startsWith('dist/') ||
    normalized === 'dist/' ||
    normalized.startsWith('../') ||
    path.posix.isAbsolute(normalized)
  ) {
    throw new Error(`renderer artifact path escape or invalid asar entry: ${entry}`)
  }
  return normalized
}

function asarRendererFiles(candidateRoot) {
  const asarPath = path.join(candidateRoot, 'resources', 'app.asar')
  const files = []
  const seen = new Set()
  for (const entry of listPackage(asarPath)) {
    const slashPath = String(entry).replace(/\\/g, '/').replace(/^\/+/, '')
    if (!slashPath.startsWith('dist/') || !RENDERER_FILE.test(slashPath)) continue
    const normalized = normalizeAsarEntry(entry)
    const comparisonKey = process.platform === 'win32' ? normalized.toLowerCase() : normalized
    if (seen.has(comparisonKey)) {
      throw new Error(`ambiguous renderer artifact path in app.asar: ${normalized}`)
    }
    seen.add(comparisonKey)
    files.push({
      path: `${ASAR_RENDERER_PREFIX}${normalized}`,
      bytes: extractFile(asarPath, normalized)
    })
  }
  files.sort((left, right) => left.path.localeCompare(right.path))
  return files
}

function rendererFiles(candidateRoot) {
  const unpacked = unpackedRendererFiles(candidateRoot)
  return unpacked.length > 0 ? unpacked : asarRendererFiles(candidateRoot)
}

function markerBearingRendererArtifacts(candidateRoot, requiredMarker) {
  if (!requiredMarker) {
    throw new Error(`packaged renderer is missing required marker: ${requiredMarker}`)
  }
  const markerFiles = rendererFiles(candidateRoot).filter(file => file.bytes.toString('utf8').includes(requiredMarker))
  if (markerFiles.length === 0) {
    throw new Error(`packaged renderer is missing required marker: ${requiredMarker}`)
  }
  if (markerFiles.length !== 1) {
    throw new Error(`ambiguous marker-bearing renderer artifacts: expected exactly one, received ${markerFiles.length}`)
  }
  return markerFiles.map(file => ({
    path: file.path,
    sha256: sha256Bytes(file.bytes)
  }))
}

export function readPackagedRendererText(candidateRoot) {
  return rendererFiles(candidateRoot)
    .map(file => file.bytes.toString('utf8'))
    .join('\n')
}

export function validatePackageProvenance({
  candidateRoot,
  expectedCandidateRoot,
  expectedHead,
  expectedRepoRoot,
  requiredMarker
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

  const rendererArtifacts = markerBearingRendererArtifacts(candidateRoot, requiredMarker)

  return {
    schemaVersion: PROVENANCE_SCHEMA_VERSION,
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
      },
      rendererArtifacts
    }
  }
}

export function writeProvenanceManifest(manifestPath, provenance) {
  requireExactPath(manifestPath, path.join(provenance.candidateRoot, MANIFEST_NAME), 'provenance manifest path')
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

function requireManifestFileBinding(candidateRoot, entry, expectedPath, label) {
  if (!entry || entry.path !== expectedPath || !SHA256.test(String(entry.sha256 || ''))) {
    throw new Error(`${label} manifest binding is invalid`)
  }
  const target = path.join(candidateRoot, ...expectedPath.split('/'))
  requireNonemptyFile(target, label)
  const actual = sha256File(target)
  if (actual !== entry.sha256) {
    throw new Error(`${label} hash mismatch: ${actual} != ${entry.sha256}`)
  }
}

function rendererBytesFromManifestPath(candidateRoot, artifactPath) {
  if (typeof artifactPath !== 'string' || artifactPath.includes('\\')) {
    throw new Error(`renderer artifact path is invalid: ${artifactPath}`)
  }

  if (artifactPath.startsWith(UNPACKED_RENDERER_PREFIX)) {
    if (path.posix.normalize(artifactPath) !== artifactPath) {
      throw new Error(`renderer artifact path escape or ambiguity: ${artifactPath}`)
    }
    const target = path.join(candidateRoot, ...artifactPath.split('/'))
    requirePathInside(candidateRoot, target, 'renderer artifact path')
    if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
      throw new Error(`missing renderer artifact at ${artifactPath}`)
    }
    if (fs.lstatSync(target).isSymbolicLink()) {
      throw new Error(`renderer artifact path escape is not allowed through symlinks: ${artifactPath}`)
    }
    requireRealPathInside(
      path.join(candidateRoot, 'resources', 'app.asar.unpacked', 'dist'),
      target,
      'renderer artifact path'
    )
    return fs.readFileSync(target)
  }

  if (artifactPath.startsWith(ASAR_RENDERER_PREFIX)) {
    const entry = artifactPath.slice(ASAR_RENDERER_PREFIX.length)
    const normalized = normalizeAsarEntry(entry)
    if (entry !== normalized || !RENDERER_FILE.test(normalized)) {
      throw new Error(`renderer artifact path is invalid: ${artifactPath}`)
    }
    return extractFile(path.join(candidateRoot, 'resources', 'app.asar'), normalized)
  }

  throw new Error(`renderer artifact path escapes the approved renderer roots: ${artifactPath}`)
}

function requireRendererManifestBindings(candidateRoot, entries, requiredMarker) {
  if (!Array.isArray(entries) || entries.length !== 1) {
    throw new Error(
      `ambiguous renderer artifact manifest: expected exactly one binding, received ${
        Array.isArray(entries) ? entries.length : 'invalid'
      }`
    )
  }
  const seen = new Set()
  for (const entry of entries) {
    if (!entry || !SHA256.test(String(entry.sha256 || ''))) {
      throw new Error('renderer artifact manifest binding is invalid')
    }
    const comparisonKey = process.platform === 'win32' ? String(entry.path).toLowerCase() : String(entry.path)
    if (seen.has(comparisonKey)) {
      throw new Error(`ambiguous duplicate renderer artifact path: ${entry.path}`)
    }
    seen.add(comparisonKey)
    const bytes = rendererBytesFromManifestPath(candidateRoot, entry.path)
    if (!bytes.toString('utf8').includes(requiredMarker)) {
      throw new Error(`bound renderer artifact is missing required marker: ${entry.path}`)
    }
    const actual = sha256Bytes(bytes)
    if (actual !== entry.sha256) {
      throw new Error(`renderer artifact hash mismatch: ${entry.path} ${actual} != ${entry.sha256}`)
    }
  }
}

export function readProvenanceManifest({
  manifestPath,
  candidateRoot,
  expectedCandidateRoot,
  expectedHead,
  expectedRepoRoot,
  requiredMarker
}) {
  requireExactPath(manifestPath, path.join(candidateRoot, MANIFEST_NAME), 'provenance manifest path')
  requireNonemptyFile(manifestPath, MANIFEST_NAME)

  let provenance
  try {
    provenance = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
  } catch (error) {
    throw new Error(`provenance manifest is not valid JSON: ${error.message}`)
  }

  if (provenance.schemaVersion !== PROVENANCE_SCHEMA_VERSION) {
    throw new Error(`provenance manifest schema mismatch: ${provenance.schemaVersion}`)
  }
  if (provenance.commit !== expectedHead) {
    throw new Error(`provenance manifest commit does not equal expected HEAD: ${provenance.commit}`)
  }
  if (!['local', 'ci'].includes(provenance.source)) {
    throw new Error(`provenance manifest source is fallback or unsupported: ${provenance.source}`)
  }
  requireExactPath(provenance.repoRoot, expectedRepoRoot, 'provenance worktree root')
  requireExactPath(provenance.candidateRoot, expectedCandidateRoot, 'provenance candidate root')
  requireExactPath(candidateRoot, expectedCandidateRoot, 'candidate root')
  if (provenance.requiredMarker !== requiredMarker) {
    throw new Error('provenance manifest required marker mismatch')
  }
  if (!provenance.files || typeof provenance.files !== 'object') {
    throw new Error('provenance manifest files are missing')
  }

  requireManifestFileBinding(candidateRoot, provenance.files.hermesExe, 'Hermes.exe', 'Hermes.exe')
  requireManifestFileBinding(candidateRoot, provenance.files.appAsar, 'resources/app.asar', 'resources/app.asar')
  requireManifestFileBinding(
    candidateRoot,
    provenance.files.installStamp,
    'resources/install-stamp.json',
    'resources/install-stamp.json'
  )
  requireRendererManifestBindings(candidateRoot, provenance.files.rendererArtifacts, requiredMarker)

  const current = validatePackageProvenance({
    candidateRoot,
    expectedCandidateRoot,
    expectedHead,
    expectedRepoRoot,
    requiredMarker
  })
  if (JSON.stringify(current.files) !== JSON.stringify(provenance.files)) {
    throw new Error('provenance manifest file bindings do not match the current package')
  }

  return {
    provenance,
    manifest: {
      path: path.resolve(manifestPath),
      sha256: sha256File(manifestPath)
    }
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
  let manifest = null
  if (manifestPath) {
    const written = writeProvenanceManifest(manifestPath, provenance)
    const readback = readProvenanceManifest({
      manifestPath,
      candidateRoot,
      expectedCandidateRoot,
      expectedHead,
      expectedRepoRoot,
      requiredMarker
    })
    manifest = {
      ...written,
      readbackSha256: readback.manifest.sha256
    }
  }
  process.stdout.write(`${JSON.stringify({ ...provenance, manifest }, null, 2)}\n`)
}

if (isMain(import.meta.url)) {
  main()
}
