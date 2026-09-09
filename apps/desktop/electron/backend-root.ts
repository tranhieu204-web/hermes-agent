interface InterpreterCandidate {
  python: string
  venvRoot: string
}

interface PinnedBackendSelection {
  root: string
  python: string
  venvRoot: string | null
}

interface BackendRootOptions<T> {
  rootOverride?: string | null
  commandOverride?: string | null
  pythonOverride?: string | null
  resolvePath: (value: string) => string
  isSourceRoot: (root: string) => boolean
  fileExists: (filePath: string) => boolean
  interpreterCandidates: (root: string) => InterpreterCandidate[]
  venvRootForPython: (python: string, root: string) => string | null
  createPinnedBackend: (selection: PinnedBackendSelection) => T
  resolveAutomatic: () => T
}

function nonempty(value?: string | null) {
  const normalized = String(value || '').trim()
  return normalized || null
}

export function resolveBackendWithOptionalRootPin<T>(options: BackendRootOptions<T>): T {
  const requestedRoot = nonempty(options.rootOverride)
  if (!requestedRoot) return options.resolveAutomatic()
  const root = options.resolvePath(requestedRoot)
  if (nonempty(options.commandOverride)) {
    throw new Error(`HERMES_DESKTOP_HERMES_ROOT ${root} conflicts with HERMES_DESKTOP_HERMES`)
  }
  if (!options.isSourceRoot(root)) {
    throw new Error(`HERMES_DESKTOP_HERMES_ROOT ${root} is not a Hermes source root`)
  }
  const explicitPython = nonempty(options.pythonOverride)
  if (explicitPython) {
    const python = options.resolvePath(explicitPython)
    if (!options.fileExists(python)) throw new Error(`HERMES_DESKTOP_PYTHON ${python} does not exist`)
    return options.createPinnedBackend({ python, root, venvRoot: options.venvRootForPython(python, root) })
  }
  const candidate = options.interpreterCandidates(root).find(item => options.fileExists(item.python))
  if (!candidate) throw new Error(`HERMES_DESKTOP_HERMES_ROOT ${root} has no usable interpreter`)
  return options.createPinnedBackend({ root, python: candidate.python, venvRoot: candidate.venvRoot })
}
