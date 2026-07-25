import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = resolve(fileURLToPath(new URL('.', import.meta.url)))
const REPO_ROOT = resolve(SCRIPT_DIR, '..', '..', '..')
const SELF_PATH = 'apps/desktop/scripts/check-verifier-process-safety.mjs'
const SELF_TEST_PATH = 'apps/desktop/scripts/check-verifier-process-safety.node-test.mjs'
const VERIFIER_LIB_PATH = 'apps/desktop/scripts/desktop-verifier-lib.mjs'
const VERIFIER_LIB_TEST_PATH = 'apps/desktop/scripts/desktop-verifier-lib.node-test.mjs'
const FIXTURE_PREFIX = 'apps/desktop/scripts/fixtures/process-safety/'
const DESKTOP_PACKAGE_JSON = 'apps/desktop/package.json'
const DESKTOP_CODE_ROOTS = [
  'apps/desktop/electron/',
  'apps/desktop/e2e/',
  'apps/desktop/scripts/',
  'apps/desktop/src/'
]
const DESKTOP_EXCLUDED_ROOTS = [
  'apps/desktop/assets/',
  'apps/desktop/coverage/',
  'apps/desktop/dist/',
  'apps/desktop/node_modules/',
  'apps/desktop/playwright-report/',
  'apps/desktop/public/',
  'apps/desktop/release/',
  'apps/desktop/test-results/'
]
const EXECUTABLE_EXTENSIONS = new Set([
  '.bash',
  '.bat',
  '.cjs',
  '.cmd',
  '.html',
  '.js',
  '.mjs',
  '.ps1',
  '.py',
  '.sh',
  '.ts',
  '.tsx'
])
const DESKTOP_TSCONFIG_PATTERN = /^apps\/desktop\/tsconfig(?:\.[^/]+)?\.json$/i
export const ROOT_SCRIPT_RELEVANCE_PATTERN = /(?:desktop|verif|^dev(?:[-_.]|$))/i
const RAW_PROCESS_REASON =
  'verifier harness uses raw process API outside canonical owned infrastructure'
const SEMANTIC_PROCESS_VOCABULARY =
  /taskkill|Stop-Process|spps|Get-Process|\bgps\b|Get-CimInstance|Get-WmiObject|Win32_Process|Remove-CimInstance|\bwmic\b|\bpkill\b|\bkillall\b/i

const REASONS = {
  getProcess: 'process-name termination via Get-Process collection',
  killall: 'process-name termination via killall',
  pkill: 'process-name termination via pkill',
  stopProcess: 'process-name termination via Stop-Process -Name',
  taskkill: 'process-name termination via taskkill image selector',
  wmi: 'process-name termination via WMI/CIM'
}

// These are the only raw process APIs allowed in verifier-owned code. Each
// entry is constrained to a path + named function so adding another call fails
// closed. The rationale is part of the policy, not a blanket file exemption.
const RAW_PROCESS_API_ALLOWLIST = {
  [SELF_PATH]: {
    imports: new Set(['child_process']),
    functions: {
      // Git is used only to enumerate tracked + nonignored untracked files.
      scanRepository: new Set(['spawnSync', 'spawnSyncImpl'])
    }
  },
  [VERIFIER_LIB_PATH]: {
    imports: new Set(['child_process']),
    functions: {
      // Launches exactly the candidate executable and records its owned PID.
      launchOwnedDesktop: new Set(['nodeSpawn', 'spawnImpl']),
      // Reads PID/parent-PID metadata only to recover descendants of an owned exited root.
      discoverWindowsDescendantPids: new Set(['nodeSpawnSync', 'spawnSyncImpl']),
      // Windows cleanup executes exact taskkill /PID <owned> /T /F.
      terminateOwnedChild: new Set(['nodeSpawnSync', 'spawnSyncImpl']),
      // POSIX cleanup signals only the negative PGID created for this launch.
      terminateOwnedProcessGroup: new Set(['killImpl'])
    }
  },
  [VERIFIER_LIB_TEST_PATH]: {
    // The integration test imports spawnSync only for exact-PID failure cleanup.
    imports: new Set(['child_process']),
    functions: {
      // Test-only liveness probe; signal 0 never terminates a process.
      windowsPidExists: new Set(['process.kill'])
    }
  }
}

function normalizeRelativePath(relativePath) {
  return relativePath.replaceAll('\\', '/').replace(/^\.\//, '')
}

function fileName(relativePath) {
  return relativePath.slice(relativePath.lastIndexOf('/') + 1)
}

function isRelevantRootScript(normalized) {
  return normalized.startsWith('scripts/') &&
    EXECUTABLE_EXTENSIONS.has(extname(normalized).toLowerCase()) &&
    ROOT_SCRIPT_RELEVANCE_PATTERN.test(fileName(normalized))
}

function isRelevantWorkflow(normalized) {
  return normalized.startsWith('.github/workflows/') &&
    ['.yaml', '.yml'].includes(extname(normalized).toLowerCase())
}

export function shouldScanRepositoryPath(relativePath) {
  const normalized = normalizeRelativePath(relativePath)

  if (normalized.startsWith(FIXTURE_PREFIX)) {
    return false
  }

  if (isRelevantRootScript(normalized) || isRelevantWorkflow(normalized)) {
    return true
  }

  if (!normalized.startsWith('apps/desktop/') ||
      DESKTOP_EXCLUDED_ROOTS.some(root => normalized.startsWith(root))) {
    return false
  }

  if (normalized === DESKTOP_PACKAGE_JSON || DESKTOP_TSCONFIG_PATTERN.test(normalized)) {
    return true
  }

  const extension = extname(normalized).toLowerCase()
  const isDesktopRootFile = normalized.slice('apps/desktop/'.length).includes('/') === false

  return EXECUTABLE_EXTENSIONS.has(extension) &&
    (isDesktopRootFile || DESKTOP_CODE_ROOTS.some(root => normalized.startsWith(root)))
}

function isVerifierHarnessPath(relativePath) {
  const normalized = normalizeRelativePath(relativePath)

  return normalized === SELF_PATH ||
    normalized === SELF_TEST_PATH ||
    normalized === VERIFIER_LIB_PATH ||
    (isRelevantRootScript(normalized) && /verif/i.test(fileName(normalized))) ||
    (normalized.startsWith('apps/desktop/scripts/') && /verif/i.test(fileName(normalized)))
}

function maskRange(characters, start, end) {
  for (let index = start; index < end; index++) {
    if (characters[index] !== '\n' && characters[index] !== '\r') {
      characters[index] = ' '
    }
  }
}

function maskHereDocuments(source, relativePath) {
  const extension = extname(relativePath).toLowerCase()
  const characters = [...source]
  const lines = [...source.matchAll(/[^\r\n]*(?:\r?\n|$)/g)].filter(match => match[0])
  let closingMarker = null
  let blockStart = null

  for (const line of lines) {
    const contents = line[0].replace(/\r?\n$/, '')

    if (closingMarker !== null) {
      if (contents.trim() === closingMarker) {
        maskRange(characters, blockStart, line.index + line[0].length)
        closingMarker = null
        blockStart = null
      }

      continue
    }

    if (extension === '.ps1') {
      const opening = contents.match(/@(['"])\s*$/)

      if (opening) {
        closingMarker = `${opening[1]}@`
        blockStart = line.index
      }
    } else if (['.bash', '.sh'].includes(extension)) {
      const opening = contents.match(/<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1/)

      if (opening) {
        closingMarker = opening[2]
        blockStart = line.index
      }
    }
  }

  if (blockStart !== null) {
    maskRange(characters, blockStart, characters.length)
  }

  return characters.join('')
}

function maskComments(source, relativePath) {
  const extension = extname(relativePath).toLowerCase()
  const isJavaScript = ['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)
  const isHtml = extension === '.html'
  const isPython = extension === '.py'
  const isPowerShell = extension === '.ps1'
  const isShell = ['.bash', '.sh', '.yaml', '.yml'].includes(extension)
  const isCmd = ['.bat', '.cmd'].includes(extension)
  const withoutHereDocuments = maskHereDocuments(source, relativePath)
  const characters = [...withoutHereDocuments]
  let quote = null

  for (let index = 0; index < characters.length; index++) {
    const current = characters[index]
    const next = characters[index + 1]

    if (quote) {
      if (current === '\\' && (isJavaScript || isPython || isShell)) {
        index++
      } else if (current === quote) {
        quote = null
      }

      continue
    }

    if (isHtml && withoutHereDocuments.slice(index, index + 4) === '<!--') {
      const close = withoutHereDocuments.indexOf('-->', index + 4)
      const end = close === -1 ? characters.length : close + 3
      maskRange(characters, index, end)
      index = end - 1
      continue
    }

    if (current === "'" || current === '"' || (isJavaScript && current === '`')) {
      quote = current
      continue
    }

    if (isJavaScript && current === '/' && next === '/') {
      const end = withoutHereDocuments.indexOf('\n', index)
      maskRange(characters, index, end === -1 ? characters.length : end)
      index = end === -1 ? characters.length : end - 1
      continue
    }

    if (isJavaScript && current === '/' && next === '*') {
      const close = withoutHereDocuments.indexOf('*/', index + 2)
      const end = close === -1 ? characters.length : close + 2
      maskRange(characters, index, end)
      index = end - 1
      continue
    }

    if (isPowerShell && current === '<' && next === '#') {
      const close = withoutHereDocuments.indexOf('#>', index + 2)
      const end = close === -1 ? characters.length : close + 2
      maskRange(characters, index, end)
      index = end - 1
      continue
    }

    if ((isPowerShell || isPython || isShell) && current === '#') {
      const end = withoutHereDocuments.indexOf('\n', index)
      maskRange(characters, index, end === -1 ? characters.length : end)
      index = end === -1 ? characters.length : end - 1
    }
  }

  let masked = characters.join('')

  if (isCmd) {
    masked = masked.replace(
      /^[ \t]*(?:rem(?:[ \t]|$)|::).*$/gim,
      match => match.replace(/[^\r\n]/g, ' ')
    )
  }

  return masked
}

function maskStringContents(source, relativePath) {
  const extension = extname(relativePath).toLowerCase()
  const supportsBackslashEscapes = ['.cjs', '.js', '.mjs', '.py', '.ts', '.tsx'].includes(extension)
  const supportsTemplateStrings = ['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)
  const characters = [...source]
  let mode = 'code'
  const templateExpressionDepths = []

  for (let index = 0; index < characters.length; index++) {
    const current = characters[index]

    if (mode === 'single' || mode === 'double') {
      if (current === '\\' && supportsBackslashEscapes) {
        maskRange(characters, index, Math.min(index + 2, characters.length))
        index++
      } else if ((mode === 'single' && current === "'") ||
                 (mode === 'double' && current === '"')) {
        mode = 'code'
      } else if (current !== '\n' && current !== '\r') {
        characters[index] = ' '
      }

      continue
    }

    if (mode === 'template') {
      if (current === '\\') {
        maskRange(characters, index, Math.min(index + 2, characters.length))
        index++
      } else if (current === '`') {
        mode = 'code'
      } else if (current === '$' && characters[index + 1] === '{') {
        templateExpressionDepths.push(1)
        mode = 'code'
        index++
      } else if (current !== '\n' && current !== '\r') {
        characters[index] = ' '
      }

      continue
    }

    if (templateExpressionDepths.length) {
      const top = templateExpressionDepths.length - 1

      if (current === '{') {
        templateExpressionDepths[top]++
      } else if (current === '}') {
        templateExpressionDepths[top]--

        if (templateExpressionDepths[top] === 0) {
          templateExpressionDepths.pop()
          mode = 'template'
          continue
        }
      }
    }

    if (current === "'") {
      mode = 'single'
    } else if (current === '"') {
      mode = 'double'
    } else if (supportsTemplateStrings && current === '`') {
      mode = 'template'
    }
  }

  return characters.join('')
}

function lineNumberAt(source, index) {
  return source.slice(0, index).split('\n').length
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function functionRanges(codeSource) {
  const ranges = []
  const pattern = /(?:^|\s)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/g

  for (const match of codeSource.matchAll(pattern)) {
    const openIndex = match.index + match[0].lastIndexOf('{')
    let depth = 0

    for (let index = openIndex; index < codeSource.length; index++) {
      if (codeSource[index] === '{') {
        depth++
      } else if (codeSource[index] === '}') {
        depth--

        if (depth === 0) {
          ranges.push({ name: match[1], start: match.index, end: index + 1 })
          break
        }
      }
    }
  }

  return ranges
}

function allowlistedRawApi(relativePath, index, api, ranges) {
  const policy = RAW_PROCESS_API_ALLOWLIST[relativePath]

  if (!policy) {
    return false
  }

  if (api === 'child_process') {
    return policy.imports?.has(api) === true
  }

  const owner = ranges.find(range => range.start <= index && index < range.end)

  return owner ? policy.functions?.[owner.name]?.has(api) === true : false
}

function rawProcessApiViolations(masked, relativePath) {
  if (!isVerifierHarnessPath(relativePath)) {
    return []
  }

  const extension = extname(relativePath).toLowerCase()
  const codeSource = maskStringContents(masked, relativePath)
  const ranges = functionRanges(codeSource)
  const candidates = []

  if (['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)) {
    const patterns = [
      [/\b(?:import\s+)?\{[^}]+\}\s+from\s+['"]/g, 'child_process'],
      [/\bimport\s+\*\s+as\s+\w+\s+from\s+['"]/g, 'child_process'],
      [/\{[^}]+\}\s*=\s*require\s*\(/g, 'child_process'],
      [/(?<![.$\w])(?:exec|execFile|execFileSync|execSync|nodeSpawn|nodeSpawnSync|spawn|spawnImpl|spawnSync|spawnSyncImpl)\s*\(/g, null],
      [/\b(process\.kill|killImpl)\s*\(/g, null],
      [/\b[A-Za-z_$][\w$]*\.kill\s*\(/g, 'member.kill']
    ]

    for (const [pattern, fixedApi] of patterns) {
      for (const match of codeSource.matchAll(pattern)) {
        if (fixedApi === 'child_process' &&
            !masked
              .slice(match.index, match.index + match[0].length + 100)
              .includes('node:child_process')) {
          continue
        }

        const api = fixedApi ?? match[0].slice(0, match[0].indexOf('(')).trim()
        candidates.push({ index: match.index, api })
      }
    }
  } else if (extension === '.py') {
    for (const pattern of [
      /\b(?:from\s+subprocess\s+import|import\s+subprocess\b)/g,
      /\bsubprocess\.(?:run|call|check_call|check_output|Popen)\s*\(/g,
      /\bos\.(?:kill|popen|system)\s*\(/g
    ]) {
      for (const match of codeSource.matchAll(pattern)) {
        candidates.push({ index: match.index, api: match[0] })
      }
    }
  } else if (['.bash', '.sh'].includes(extension)) {
    for (const [pattern, patternSource] of [
      [/\btrap\b/g, codeSource],
      [/\$\(/g, codeSource],
      [/(?:^|[;&|]\s*)["']?\$[A-Za-z_][A-Za-z0-9_]*["']?\s/gm, masked],
      [/\b(?:exec|xargs)\b/g, codeSource],
      [/\b(?:bash|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh|sh)\b\s+(?:\/c|-c|-Command)\b/gi, codeSource]
    ]) {
      for (const match of patternSource.matchAll(pattern)) {
        candidates.push({ index: match.index, api: match[0].trim() })
      }
    }

    for (const line of masked.matchAll(/[^\r\n]*(?:\r?\n|$)/g)) {
      const command = line[0].trim()
      const isCanonicalRoute =
        /^node\s+(?:\.\/)?apps\/desktop\/scripts\/desktop-verifier\.mjs\b/.test(command)
      const isNonProcessSyntax =
        command === '' ||
        /^#!\//.test(command) ||
        /^(?:set\s+-|(?:export|readonly)\s+[A-Za-z_][A-Za-z0-9_]*=)/.test(command) ||
        /^(?:[A-Za-z_][A-Za-z0-9_]*=|(?:if|then|elif|else|fi|case|esac|for|while|until|do|done)\b|[{}]\s*$)/.test(command)

      if (!isCanonicalRoute && !isNonProcessSyntax) {
        candidates.push({ index: line.index, api: 'shell command' })
      }
    }
  } else if (extension === '.ps1') {
    for (const pattern of [
      /\b(?:Start-Process|Stop-Process|spps|gps|Get-Process|Get-CimInstance|Get-WmiObject|Remove-CimInstance|Invoke-CimMethod)\b/gi,
      /\b(?:cmd(?:\.exe)?)\s+\/c\b/gi,
      /\.\s*(?:Kill|Terminate)\s*\(/gi
    ]) {
      for (const match of codeSource.matchAll(pattern)) {
        const commandEnd = codeSource.indexOf('\n', match.index)
        const command = codeSource.slice(
          match.index,
          commandEnd === -1 ? codeSource.length : commandEnd
        )

        if (/\b(?:Get-Process|Stop-Process|gps|spps)\b/i.test(match[0]) &&
            /\s-Id\b/i.test(command) &&
            !/\s-Name\b/i.test(command)) {
          continue
        }

        candidates.push({ index: match.index, api: match[0] })
      }
    }

    for (const match of codeSource.matchAll(
      /(?:^|[;|])\s*(?:&\s*)?(?:git|node|npm|npx|python|bash|sh|cmd|pwsh|powershell)(?:\.exe)?\b/gim
    )) {
      const command = masked.slice(match.index, masked.indexOf('\n', match.index))

      if (!/\bnode\s+(?:\.\\|\.\/)?apps[\\/]desktop[\\/]scripts[\\/]desktop-verifier\.mjs\b/i.test(command)) {
        candidates.push({ index: match.index, api: match[0] })
      }
    }
  } else if (['.bat', '.cmd'].includes(extension)) {
    for (const line of masked.matchAll(/[^\r\n]*(?:\r?\n|$)/g)) {
      const command = line[0].trim().replace(/^@/, '')
      const isCanonicalRoute =
        /^node(?:\.exe)?\s+(?:\.\\|\.\/)?apps[\\/]desktop[\\/]scripts[\\/]desktop-verifier\.mjs\b/i.test(command)
      const isProcessNameTermination = invocationReason(command) !== null
      const isNonProcessSyntax =
        command === '' ||
        /^(?:echo\b|set\s+[A-Za-z_][A-Za-z0-9_]*=|if\b|for\b|goto\b|exit\b|:[A-Za-z_])/.test(command)

      if (!isCanonicalRoute && !isProcessNameTermination && !isNonProcessSyntax) {
        candidates.push({ index: line.index, api: 'CMD command' })
      }
    }
  }

  return candidates
    .filter(candidate =>
      !allowlistedRawApi(relativePath, candidate.index, candidate.api, ranges)
    )
    .map(candidate => ({
      file: relativePath,
      line: lineNumberAt(masked, candidate.index),
      reason: RAW_PROCESS_REASON
    }))
}

function collectLiteralAssignments(source) {
  const values = new Map()
  const pattern = /\b(?:const|let|var)?\s*([A-Za-z_$][\w$]*)\s*=\s*((?:(["'`])(?:\\[\s\S]|(?!\3)[\s\S])*?\3)(?:\s*\+\s*(?:(["'`])(?:\\[\s\S]|(?!\4)[\s\S])*?\4))*)/g

  for (const match of source.matchAll(pattern)) {
    values.set(match[1], normalizeAdjacentQuotedFragments(match[2]))
  }

  for (const object of source.matchAll(
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\{([\s\S]{0,1200}?)\}/g
  )) {
    for (const property of object[2].matchAll(
      /\b([A-Za-z_$][\w$]*)\s*:\s*((?:(["'`])(?:\\[\s\S]|(?!\3)[\s\S])*?\3)(?:\s*\+\s*(?:(["'`])(?:\\[\s\S]|(?!\4)[\s\S])*?\4))*)/g
    )) {
      values.set(
        `${object[1]}.${property[1]}`,
        normalizeAdjacentQuotedFragments(property[2])
      )
    }
  }

  return values
}

function normalizeAdjacentQuotedFragments(source) {
  let normalized = String(source)
  const concatenation = /(["'`])((?:\\[\s\S]|(?!\1)[\s\S])*?)\1\s*\+\s*(["'`])((?:\\[\s\S]|(?!\3)[\s\S])*?)\3/g

  while (concatenation.test(normalized)) {
    concatenation.lastIndex = 0
    normalized = normalized.replace(concatenation, (_, _q1, left, _q2, right) => `${left}${right}`)
  }

  return normalized.replace(/(["'`])((?:\\[\s\S]|(?!\1)[\s\S])*?)\1/g, '$2')
}

function invocationReason(commandSource) {
  if (/\btaskkill(?:\.exe)?\b[\s\S]*?(?:\/IM\b|\/FI\b[\s\S]{0,240}?\bIMAGENAME\b)/i.test(commandSource)) {
    return REASONS.taskkill
  }

  if (/\b(?:Stop-Process|spps)\b[\s\S]{0,240}?\s-Name\b/i.test(commandSource)) {
    return REASONS.stopProcess
  }

  for (const match of commandSource.matchAll(/\b(?:Get-Process|gps)\b/gi)) {
    const commandEnd = commandSource.indexOf('\n', match.index)
    const selector = commandSource.slice(
      match.index,
      commandEnd === -1 ? commandSource.length : commandEnd
    )

    if (/\s-Id\b/i.test(selector) && !/\s-Name\b/i.test(selector)) {
      continue
    }

    const nearby = commandSource.slice(match.index, match.index + 900)

    if (/(?:\b(?:Stop-Process|spps)\b|\.(?:Kill|Terminate)\s*\()/i.test(nearby)) {
      return REASONS.getProcess
    }
  }

  if (/\bWin32_Process\b/i.test(commandSource) &&
      /\b(?:Get-CimInstance|Get-WmiObject)\b/i.test(commandSource) &&
      /(?:\b(?:Remove-CimInstance|Invoke-CimMethod)\b|\.(?:Delete|Kill|Terminate)\s*\()/i.test(commandSource) ||
      /\bwmic(?:\.exe)?\b[\s\S]{0,500}?\bprocess\b[\s\S]{0,500}?\bname\b[\s\S]{0,500}?(?:call\s+terminate|delete)\b/i.test(commandSource)) {
    return REASONS.wmi
  }

  if (/\bpkill\b/i.test(commandSource)) {
    return REASONS.pkill
  }

  if (/\bkillall\b/i.test(commandSource)) {
    return REASONS.killall
  }

  return null
}

function processCallAliases(source, extension) {
  const aliases = new Set(['exec', 'execFile', 'execFileSync', 'execSync', 'spawn', 'spawnSync'])

  if (['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)) {
    for (const match of source.matchAll(/\{([^}]+)\}\s*(?:=\s*require\s*\(\s*['"]node:child_process['"]\s*\)|from\s*['"]node:child_process['"])/g)) {
      for (const binding of match[1].split(',')) {
        const parts = binding.trim().split(/\s+(?:as|:)\s+/)
        aliases.add(parts.at(-1))
      }
    }

    const namespaces = new Set()

    for (const match of source.matchAll(
      /\bimport\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+['"]node:child_process['"]/g
    )) {
      namespaces.add(match[1])
    }

    for (const match of source.matchAll(
      /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['"]node:child_process['"]\s*\)/g
    )) {
      namespaces.add(match[1])
    }

    if (namespaces.size) {
      const namespacePattern = [...namespaces].map(escapeRegExp).join('|')
      const memberAliasPattern = new RegExp(
        `\\b(?:const|let|var)\\s+([A-Za-z_$][\\w$]*)\\s*=\\s*(?:${namespacePattern})` +
        `(?:\\.\\s*(?:exec|execFile|execFileSync|execSync|spawn|spawnSync)\\b|` +
        `\\[\\s*['"](?:exec|execFile|execFileSync|execSync|spawn|spawnSync)['"]\\s*\\])`,
        'g'
      )

      for (const match of source.matchAll(memberAliasPattern)) {
        aliases.add(match[1])
      }
    }
  } else if (extension === '.py') {
    for (const match of source.matchAll(/from\s+subprocess\s+import\s+([^\r\n]+)/g)) {
      for (const binding of match[1].split(',')) {
        const parts = binding.trim().split(/\s+as\s+/)
        aliases.add(parts.at(-1))
      }
    }

    for (const match of source.matchAll(/\bimport\s+subprocess\s+as\s+([A-Za-z_]\w*)/g)) {
      for (const api of ['run', 'call', 'check_call', 'check_output', 'Popen']) {
        aliases.add(`${match[1]}.${api}`)
      }
    }

    const moduleNames = ['subprocess']
    for (const match of source.matchAll(/\bimport\s+subprocess\s+as\s+([A-Za-z_]\w*)/g)) {
      moduleNames.push(match[1])
    }
    const modulePattern = moduleNames.map(escapeRegExp).join('|')
    const localAliasPattern = new RegExp(
      `\\b([A-Za-z_]\\w*)\\s*=\\s*(?:${modulePattern})\\.` +
      '(?:run|call|check_call|check_output|Popen)\\b',
      'g'
    )
    for (const match of source.matchAll(localAliasPattern)) {
      aliases.add(match[1])
    }
  }

  return [...aliases].filter(Boolean)
}

function findProcessInvocations(source, relativePath) {
  const extension = extname(relativePath).toLowerCase()
  const isJavaScript = ['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)

  if (isJavaScript && !/['"]node:child_process['"]/.test(source)) {
    return []
  }

  const codeSource = maskStringContents(source, relativePath)
  const aliases = processCallAliases(source, extension)
  const aliasPattern = aliases.map(escapeRegExp).join('|')
  const pattern = extension === '.py'
    ? new RegExp(`\\b(?:subprocess\\.(?:run|call|check_call|check_output|Popen)|os\\.(?:system|popen)|${aliasPattern})\\s*\\(`, 'g')
    : new RegExp(`\\b(?:${aliasPattern})\\s*\\(`, 'g')
  const invocations = []

  for (const match of codeSource.matchAll(pattern)) {
    const openIndex = codeSource.indexOf('(', match.index)
    let depth = 0
    let endIndex = source.length

    for (let index = openIndex; index < codeSource.length; index++) {
      if (codeSource[index] === '(') {
        depth++
      } else if (codeSource[index] === ')') {
        depth--

        if (depth === 0) {
          endIndex = index + 1
          break
        }
      }
    }

    invocations.push({
      index: match.index,
      source: source.slice(match.index, endIndex)
    })
  }

  return invocations
}

function addInvocationViolations(violations, masked, relativePath) {
  const literals = collectLiteralAssignments(masked)

  for (const invocation of findProcessInvocations(masked, relativePath)) {
    let resolved = normalizeAdjacentQuotedFragments(invocation.source)

    for (const [name, value] of literals) {
      const [owner, property] = name.split('.', 2)
      const reference = property
        ? new RegExp(
          `\\b${escapeRegExp(owner)}(?:\\.${escapeRegExp(property)}\\b|` +
          `\\[\\s*['"]${escapeRegExp(property)}['"]\\s*\\])`
        )
        : new RegExp(`\\b${escapeRegExp(name)}\\b`)

      if (reference.test(invocation.source)) {
        resolved = `${value} ${resolved}`
      }
    }

    const reason = invocationReason(resolved)

    if (reason) {
      violations.push({
        file: relativePath,
        line: lineNumberAt(masked, invocation.index),
        reason
      })
    }
  }
}

function isInertOutputCommand(command) {
  return (/^\s*(?:echo|printf|Write-(?:Host|Output))\b/i.test(command) ||
    /^\s*(?:powershell|pwsh)(?:\.exe)?\s+-Command\s+["']?Write-(?:Host|Output)\b/i.test(command)) &&
    !/(?:&&|\|\||[;|])/.test(command)
}

function workflowExecutableSource(source) {
  const output = [...source].map(character =>
    character === '\r' || character === '\n' ? character : ' '
  )
  const lines = [...source.matchAll(/[^\r\n]*(?:\r?\n|$)/g)]

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex++) {
    const line = lines[lineIndex]
    const run = /^(\s*)(?:-\s*)?run\s*:\s*(.*?)(?:\r?\n)?$/.exec(line[0])

    if (!run) {
      continue
    }

    const runIndent = run[1].length
    const valueOffset = line.index + line[0].indexOf(run[2])

    if (/^[|>][-+]?\s*$/.test(run[2])) {
      for (let nestedIndex = lineIndex + 1; nestedIndex < lines.length; nestedIndex++) {
        const nested = lines[nestedIndex]
        const content = nested[0].replace(/\r?\n$/, '')
        const indentation = /^\s*/.exec(content)[0].length

        if (content.trim() && indentation <= runIndent) {
          break
        }

        if (!isInertOutputCommand(content.trim())) {
          for (let index = 0; index < content.length; index++) {
            output[nested.index + index] = source[nested.index + index]
          }
        }
      }
    } else if (!isInertOutputCommand(run[2])) {
      for (let index = 0; index < run[2].length; index++) {
        output[valueOffset + index] = source[valueOffset + index]
      }
    }
  }

  return output.join('')
}

function powershellExecutableSource(source) {
  const characters = [...source]

  for (const assignment of source.matchAll(/^\s*\$([A-Za-z_]\w*)\s*=.*$/gm)) {
    const variable = escapeRegExp(assignment[1])
    const remainder = source.slice(assignment.index + assignment[0].length)
    const isExecutableValue = new RegExp(
      `(?:&|Get-CimInstance|Get-WmiObject|Invoke-CimMethod)\\s+\\$${variable}\\b`,
      'i'
    ).test(remainder)

    if (!isExecutableValue) {
      maskRange(characters, assignment.index, assignment.index + assignment[0].length)
    }
  }

  return characters.join('')
}

function addShellViolations(violations, masked, relativePath) {
  const extension = extname(relativePath).toLowerCase()
  const executableSource = ['.yaml', '.yml'].includes(extension)
    ? workflowExecutableSource(masked)
    : extension === '.ps1'
      ? powershellExecutableSource(masked)
      : masked
  const normalized = normalizeAdjacentQuotedFragments(executableSource)
    .replace(/['"]?Stop-\{0\}['"]?\s*-f\s*['"]?Process['"]?/gi, 'Stop-Process')
    .replace(/\^(?=[A-Za-z])/g, '')
  const reason = invocationReason(normalized)

  if (reason) {
    const vocabulary = {
      [REASONS.taskkill]: /taskkill/i,
      [REASONS.stopProcess]: /(?:Stop-Process|spps)/i,
      [REASONS.getProcess]: /(?:Get-Process|gps)/i,
      [REASONS.wmi]: /(?:Get-CimInstance|Get-WmiObject|wmic)/i,
      [REASONS.pkill]: /pkill/i,
      [REASONS.killall]: /killall/i
    }[reason]
    const match = vocabulary.exec(normalized)

    violations.push({
      file: relativePath,
      line: lineNumberAt(executableSource, match?.index ?? 0),
      reason
    })
  }
}

function addPackageScriptViolations(violations, source, relativePath) {
  let parsed

  try {
    parsed = JSON.parse(source)
  } catch {
    return
  }

  for (const [name, command] of Object.entries(parsed?.scripts ?? {})) {
    if (typeof command !== 'string') {
      continue
    }

    const reason = invocationReason(normalizeAdjacentQuotedFragments(command))

    if (reason) {
      const keyIndex = source.indexOf(JSON.stringify(name))
      violations.push({
        file: relativePath,
        line: lineNumberAt(source, keyIndex === -1 ? 0 : keyIndex),
        reason
      })
    }
  }
}

function uniqueViolations(violations) {
  const unique = new Map()

  for (const violation of violations) {
    unique.set(`${violation.file}:${violation.line}:${violation.reason}`, violation)
  }

  return [...unique.values()].sort((left, right) =>
    left.file.localeCompare(right.file) ||
    left.line - right.line ||
    left.reason.localeCompare(right.reason)
  )
}

export function scanText(source, relativePath) {
  const normalizedPath = normalizeRelativePath(relativePath)
  const extension = extname(normalizedPath).toLowerCase()
  const hasSemanticVocabulary =
    SEMANTIC_PROCESS_VOCABULARY.test(source) ||
    /(?:task\s*(?:\^|["'`+\s])*kill|pki\s*["'`+\s]*ll|kill\s*["'`+\s]*all|Stop-[\s\S]{0,80}?Process)/i.test(source)

  if (normalizedPath !== DESKTOP_PACKAGE_JSON &&
      !isVerifierHarnessPath(normalizedPath) &&
      !hasSemanticVocabulary) {
    return []
  }

  const masked = maskComments(source, normalizedPath)
  const rawApiViolations = rawProcessApiViolations(masked, normalizedPath)

  if (rawApiViolations.length) {
    return uniqueViolations(rawApiViolations)
  }

  const violations = []

  if (normalizedPath === DESKTOP_PACKAGE_JSON) {
    addPackageScriptViolations(violations, source, normalizedPath)
  } else if (!hasSemanticVocabulary) {
    return []
  } else if (['.cjs', '.js', '.mjs', '.py', '.ts', '.tsx'].includes(extension)) {
    addInvocationViolations(violations, masked, normalizedPath)
  } else if (['.bash', '.bat', '.cmd', '.ps1', '.sh', '.yaml', '.yml'].includes(extension)) {
    addShellViolations(violations, masked, normalizedPath)
  }

  return uniqueViolations(violations)
}

export function formatViolations(violations) {
  return violations
    .map(violation => `${violation.file}:${violation.line}: ${violation.reason}`)
    .join('\n')
}

export function scanRepository(repoRoot = REPO_ROOT, {
  spawnSyncImpl = spawnSync
} = {}) {
  const tracked = spawnSyncImpl(
    'git',
    ['ls-files', '-z', '--cached', '--others', '--exclude-standard'],
    {
      cwd: repoRoot,
      encoding: 'utf8',
      windowsHide: true
    }
  )

  if (tracked.status !== 0) {
    const detail = tracked.stderr.trim() || `git ls-files exited ${tracked.status}`
    throw new Error(`could not enumerate repository files: ${detail}`)
  }

  const files = tracked.stdout
    .split('\0')
    .filter(Boolean)
    .map(normalizeRelativePath)
    .filter(shouldScanRepositoryPath)
    .sort()
  const violations = files.flatMap(relativePath =>
    scanText(readFileSync(join(repoRoot, ...relativePath.split('/')), 'utf8'), relativePath)
  )

  return {
    filesScanned: files.length,
    violations
  }
}

function main() {
  const result = scanRepository()

  if (result.violations.length) {
    console.error('Desktop verifier process-safety gate failed:')
    console.error(formatViolations(result.violations))
    process.exitCode = 1
    return
  }

  console.log(
    `Desktop verifier process-safety gate passed ` +
    `(${result.filesScanned} tracked/untracked code and config files scanned)`
  )
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
