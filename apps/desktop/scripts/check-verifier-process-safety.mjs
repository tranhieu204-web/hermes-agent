import { spawnSync } from 'node:child_process'
import { lstatSync, readFileSync, realpathSync } from 'node:fs'
import { dirname, extname, isAbsolute, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = resolve(fileURLToPath(new URL('.', import.meta.url)))
const REPO_ROOT = resolve(SCRIPT_DIR, '..', '..', '..')
const SELF_PATH = 'apps/desktop/scripts/check-verifier-process-safety.mjs'
const SELF_TEST_PATH = 'apps/desktop/scripts/check-verifier-process-safety.node-test.mjs'
const VERIFIER_LIB_PATH = 'apps/desktop/scripts/desktop-verifier-lib.mjs'
const OWNED_VERIFIER_TEST_PROCESS_PATH = 'apps/desktop/scripts/owned-verifier-test-process.mjs'
const WINDOWS_JOB_HOST_PATH = 'apps/desktop/scripts/windows-verifier-job-host.cs'
const WINDOWS_JOB_HOST_TEST_PATH = 'apps/desktop/scripts/windows-verifier-job-host.node-test.mjs'
const FIXTURE_PREFIX = 'apps/desktop/scripts/fixtures/process-safety/'
const ROOT_PACKAGE_JSON = 'package.json'
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
  '.cs',
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
  stopProcess: 'process termination via Stop-Process',
  taskkill: 'process termination via taskkill',
  wmi: 'WMI/CIM process-tree reconstruction'
}
const NATIVE_API_OUTSIDE_REASON =
  'Windows native process APIs are allowed only in the exact checked-in Job host'
const HIDDEN_CSHARP_REASON =
  'PowerShell verifier embeds hidden C# native process code'
const SOURCE_READING_TEST_REASON =
  'test reads production source text instead of executing behavior'
const PREFLIGHT_ORDER_REASON =
  'process-safety scanner preflight must run before candidate-controlled workflow or package commands'
const DELEGATION_REASON =
  'package-script delegation must resolve to bounded repository-controlled script files'
const GOVERNED_PACKAGE_PATHS = [ROOT_PACKAGE_JSON, DESKTOP_PACKAGE_JSON]
const MAX_DELEGATION_DEPTH = 32
const MAX_DELEGATION_NODES = 2048
const MAX_PACKAGE_COMMAND_LENGTH = 32_768
const DIRECT_SCANNER_COMMAND =
  /^\s*node(?:\.exe)?\s+(?:\.[\\/])?apps[\\/]desktop[\\/]scripts[\\/]check-verifier-process-safety\.mjs\s*$/i
const REQUIRED_JOB_APIS = [
  'CreateJobObjectW',
  'SetInformationJobObject',
  'CreateProcessW',
  'AssignProcessToJobObject',
  'ResumeThread',
  'TerminateJobObject',
  'QueryInformationJobObject',
  'GetProcessTimes',
  'QueryFullProcessImageNameW',
  'WaitForSingleObject',
  'CloseHandle'
]
const NATIVE_PROCESS_VOCABULARY = new RegExp(
  `\\b(?:DllImport|OpenProcess|TerminateProcess|${REQUIRED_JOB_APIS.join('|')})\\b`,
  'i'
)

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
      // Launches only the source-controlled PowerShell Job controller.
      launchWindowsOwnedDesktop: new Set(['spawnImpl']),
      // Prepares only the source-controlled Job-host cache before the protocol deadline begins.
      prepareWindowsJobHost: new Set(['member.kill', 'spawnImpl']),
      // POSIX behavior remains a direct exact executable launch.
      launchOwnedDesktop: new Set(['spawnImpl']),
      // POSIX cleanup signals only the negative PGID created for this launch.
      terminateOwnedProcessGroup: new Set(['killImpl'])
    }
  },
  [OWNED_VERIFIER_TEST_PROCESS_PATH]: {
    // Test-only direct-child lifecycle helper. It has no descendant authority.
    imports: new Set(['child_process']),
    functions: {
      launchDirectOwnedVerifierTestProcess: new Set(['nodeSpawn', 'member.kill'])
    }
  },
  [WINDOWS_JOB_HOST_TEST_PATH]: {
    // Host tests execute the checked-in bootstrap and test-defined synthetic controllers.
    imports: new Set(['child_process']),
    functions: {
      runRealController: new Set(['testSpawnSync'])
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

function isRelevantCompositeAction(normalized) {
  return /^\.github\/actions\/.+\/action\.ya?ml$/i.test(normalized)
}

export function shouldScanRepositoryPath(relativePath) {
  const normalized = normalizeRelativePath(relativePath)

  if (normalized.startsWith(FIXTURE_PREFIX)) {
    return false
  }

  if (normalized === ROOT_PACKAGE_JSON || isRelevantCompositeAction(normalized) ||
      isRelevantRootScript(normalized) || isRelevantWorkflow(normalized)) {
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

function nativeViolation(source, relativePath, reason, token = '') {
  const index = token ? source.indexOf(token) : 0

  return {
    file: relativePath,
    line: lineNumberAt(source, Math.max(0, index)),
    reason
  }
}

function countCalls(source, name) {
  return [...source.matchAll(new RegExp(`\\b${escapeRegExp(name)}\\s*\\(`, 'g'))].length
}

function bracedBlockAt(source, signature) {
  const start = source.indexOf(signature)
  if (start === -1) {
    return ''
  }
  const open = source.indexOf('{', start + signature.length)
  if (open === -1) {
    return ''
  }

  let depth = 0
  for (let index = open; index < source.length; index++) {
    if (source[index] === '{') {
      depth++
    } else if (source[index] === '}') {
      depth--
      if (depth === 0) {
        return source.slice(start, index + 1)
      }
    }
  }

  return ''
}

function nativeProcessViolations(source, relativePath) {
  const extension = extname(relativePath).toLowerCase()

  if (extension === '.ps1') {
    for (const hereString of source.matchAll(/@(['"])[ \t]*\r?\n([\s\S]*?)\r?\n\1@/g)) {
      if (NATIVE_PROCESS_VOCABULARY.test(hereString[2]) && /\bAdd-Type\b/i.test(source)) {
        return [nativeViolation(source, relativePath, HIDDEN_CSHARP_REASON, hereString[0])]
      }
    }

    return []
  }

  if (extension !== '.cs') {
    return []
  }

  if (relativePath !== WINDOWS_JOB_HOST_PATH) {
    return NATIVE_PROCESS_VOCABULARY.test(source)
      ? [nativeViolation(source, relativePath, NATIVE_API_OUTSIDE_REASON)]
      : []
  }

  const violations = []
  const addContract = (detail, token = '') => {
    violations.push(nativeViolation(
      source,
      relativePath,
      `Windows Job host is missing required native lifecycle contract: ${detail}`,
      token
    ))
  }

  for (const api of REQUIRED_JOB_APIS) {
    if (countCalls(source, api) < 2) {
      addContract(`required native API ${api} must be declared and invoked`, api)
    }
  }

  if (/\bOpenProcess\s*\(/i.test(source)) {
    violations.push(nativeViolation(
      source,
      relativePath,
      'Windows Job host uses destructive OpenProcess authority',
      'OpenProcess'
    ))
  }

  if (/\b(?:CreateBreakawayFromJob|JobObjectLimitBreakawayOk|JobObjectLimitSilentBreakawayOk|CREATE_BREAKAWAY_FROM_JOB|BREAKAWAY_OK|SILENT_BREAKAWAY_OK)\b/i.test(source) ||
      /0x0*1000000\b/i.test(source) ||
      /0x0*800\b/i.test(source) ||
      /0x0*1000\b/i.test(source)) {
    violations.push(nativeViolation(
      source,
      relativePath,
      'Windows Job host enables a forbidden breakaway flag'
    ))
  }

  if (/\b(?:caller|requested|termination)Pid\b/i.test(source) ||
      /RequireInteger\s*\([^)]*['"](?:pid|terminationPid)['"]/i.test(source)) {
    violations.push(nativeViolation(
      source,
      relativePath,
      'Windows Job host accepts caller PID authority'
    ))
  }

  const createJob = source.indexOf('job = CreateJobObjectW(IntPtr.Zero, null);')
  const configureJob = source.indexOf('ConfigureKillOnClose(job);')
  const createTarget = source.indexOf('process = CreateSuspendedProcess(request);')
  const assignTarget = source.indexOf('if (!AssignProcessToJobObject(job, process.hProcess))')
  const resumeTarget = source.indexOf('uint resumeResult = ResumeThread(process.hThread);')

  if (!(createJob >= 0 && createJob < configureJob && configureJob < createTarget &&
        createTarget < assignTarget && assignTarget < resumeTarget)) {
    addContract('CreateJobObjectW/configure/create/assign/resume order is required')
  }

  if (!/RequireAbsoluteExistingFile\s*\([\s\S]{0,240}?RequireString\s*\(record,\s*['"]executable['"]/m.test(source) ||
      !/CreateProcessW\s*\(\s*request\.Executable,[\s\S]{0,500}?CreateSuspended\s*\|\s*CreateUnicodeEnvironment/m.test(source)) {
    addContract('absolute target and CREATE_SUSPENDED/CREATE_UNICODE_ENVIRONMENT are required')
  }

  const preAssignment = bracedBlockAt(
    source,
    'private static void TerminateSuspendedPreAssignment'
  )
  if (!/TerminateProcess\s*\(process,/.test(preAssignment) ||
      !/WaitForSingleObject\s*\(process,\s*5000\)/.test(preAssignment)) {
    addContract('suspended pre-assignment target must use TerminateProcess then WaitForSingleObject')
  }

  const commandLoop = bracedBlockAt(source, 'private static int CommandLoop(')
  const terminateJob = commandLoop.indexOf('TerminateAssignedJob(job);')
  const waitZero = commandLoop.indexOf('WaitForZeroActive(job, request.TerminationTimeoutMs);')

  if (!(terminateJob >= 0 && terminateJob < waitZero) ||
      !/QueryAccounting\s*\(job\)[\s\S]{0,240}?ActiveProcesses\s*==\s*0/m.test(source) ||
      !/\{\s*['"]activeProcesses['"]\s*,\s*0\s*\}/.test(source)) {
    addContract('TerminateJobObject must precede QueryInformationJobObject zero-active acknowledgement')
  }

  return uniqueViolations(violations)
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

function rawProcessApiViolations(masked, relativePath, strictVerifierPolicy) {
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
      [/\b(?!process\b)[A-Za-z_$][\w$]*\.kill\s*\(/g, 'member.kill']
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
      const isProcessNameTermination = invocationReason(command, strictVerifierPolicy) !== null
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

function invocationReason(commandSource, strictVerifierPolicy = false) {
  if (/\btaskkill(?:\.exe)?\b/i.test(commandSource) &&
      (strictVerifierPolicy || /(?:\/IM\b|\/FI\b[\s\S]{0,240}?\bIMAGENAME\b)/i.test(commandSource))) {
    return REASONS.taskkill
  }

  if (/\b(?:Stop-Process|spps)\b/i.test(commandSource) &&
      (strictVerifierPolicy || /\s-Name\b/i.test(commandSource))) {
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
      /\b(?:Get-CimInstance|Get-WmiObject)\b/i.test(commandSource) ||
      /\bwmic(?:\.exe)?\b[\s\S]{0,500}?\bprocess\b/i.test(commandSource)) {
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

function addInvocationViolations(violations, masked, relativePath, strictVerifierPolicy) {
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

    const reason = invocationReason(resolved, strictVerifierPolicy)

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

function addShellViolations(violations, masked, relativePath, strictVerifierPolicy) {
  const extension = extname(relativePath).toLowerCase()
  const executableSource = ['.yaml', '.yml'].includes(extension)
    ? workflowExecutableSource(masked)
    : extension === '.ps1'
      ? powershellExecutableSource(masked)
      : masked
  const normalized = normalizeAdjacentQuotedFragments(executableSource)
    .replace(/['"]?Stop-\{0\}['"]?\s*-f\s*['"]?Process['"]?/gi, 'Stop-Process')
    .replace(/\^(?=[A-Za-z])/g, '')
  const reason = invocationReason(normalized, strictVerifierPolicy)

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

function delegationViolation(relativePath, source, detail, marker = '') {
  const index = marker ? source.indexOf(marker) : 0

  return {
    file: relativePath,
    line: lineNumberAt(source, Math.max(0, index)),
    reason: `${DELEGATION_REASON}: ${detail}`
  }
}

function shellCommandClauses(command) {
  if (command.length > MAX_PACKAGE_COMMAND_LENGTH) {
    return { clauses: [], error: 'command exceeds the bounded delegation length' }
  }

  const clauses = [[]]
  let token = ''
  let quote = null
  let escaped = false

  const finishToken = () => {
    if (token) {
      clauses.at(-1).push(token)
      token = ''
    }
  }
  const finishClause = () => {
    finishToken()
    if (clauses.at(-1).length) {
      clauses.push([])
    }
  }

  for (let index = 0; index < command.length; index++) {
    const character = command[index]

    if (escaped) {
      token += character
      escaped = false
      continue
    }

    if (quote) {
      if (character === quote) {
        quote = null
      } else if (character === '\\' && quote !== "'") {
        if ([quote, '\\'].includes(command[index + 1])) {
          escaped = true
        } else {
          token += character
        }
      } else {
        token += character
      }
      continue
    }

    if (character === "'" || character === '"' || character === '`') {
      quote = character
    } else if (character === '\\') {
      if (/\s|[;'"`|&\\]/.test(command[index + 1] ?? '')) {
        escaped = true
      } else {
        token += character
      }
    } else if (/\s/.test(character)) {
      finishToken()
      if (character === '\n' || character === '\r') {
        finishClause()
      }
    } else if (character === ';' || character === '|' || character === '&') {
      finishClause()
      while (command[index + 1] === character) {
        index++
      }
    } else {
      token += character
    }
  }

  if (quote || escaped) {
    return { clauses: [], error: 'unclosed quote or escape in package command' }
  }

  finishToken()
  return {
    clauses: clauses.filter(clause => clause.length),
    error: null
  }
}

function commandName(token) {
  return token
    .replaceAll('\\', '/')
    .slice(token.replaceAll('\\', '/').lastIndexOf('/') + 1)
    .toLowerCase()
    .replace(/\.(?:cmd|exe)$/i, '')
}

function isDynamicDelegationOperand(operand) {
  return /\$\{|\$\(|\$[A-Za-z_]|\$\{\{|%[A-Za-z_][^%]*%|[*?{}[\]<>]/.test(operand)
}

function looksLikeLocalScriptOperand(operand) {
  const normalized = operand.replaceAll('\\', '/')
  const extension = extname(normalized).toLowerCase()

  return normalized.startsWith('./') ||
    normalized.startsWith('../') ||
    normalized.includes('/') ||
    EXECUTABLE_EXTENSIONS.has(extension)
}

function packageManagerInvocation(tokens, managerIndex) {
  const manager = commandName(tokens[managerIndex])
  const words = []
  let prefix = null
  let workspace = null
  let allWorkspaces = false

  for (let index = managerIndex + 1; index < tokens.length; index++) {
    const token = tokens[index]
    const [option, inlineValue] = token.split('=', 2)

    if (['--prefix', '--dir', '-C'].includes(option)) {
      prefix = inlineValue ?? tokens[++index]
      if (!prefix) {
        return { error: `${manager} delegation has a missing directory operand` }
      }
    } else if (['--workspace', '-w', '--filter'].includes(option)) {
      workspace = inlineValue ?? tokens[++index]
      if (!workspace) {
        return { error: `${manager} delegation has a missing workspace operand` }
      }
    } else if (['--workspaces', '--ws'].includes(token)) {
      allWorkspaces = true
    } else if (token === '--') {
      words.push(...tokens.slice(index + 1))
      break
    } else if (!token.startsWith('-')) {
      words.push(token)
    }
  }

  if (manager === 'yarn' && words[0] === 'workspace') {
    if (!words[1]) {
      return { error: 'yarn workspace delegation has a missing workspace operand' }
    }
    workspace = words[1]
    words.splice(0, 2)
  }

  return { manager, words, prefix, workspace, allWorkspaces, error: null }
}

function runtimeScriptOperands(tokens, runtimeIndex) {
  const runtime = commandName(tokens[runtimeIndex])
  const operands = []
  let testMode = false

  if (runtime === 'node') {
    for (let index = runtimeIndex + 1; index < tokens.length; index++) {
      const token = tokens[index]

      if (['-e', '--eval', '-p', '--print'].includes(token)) {
        return operands
      }
      if (['-h', '--help', '-v', '--version'].includes(token)) {
        return operands
      }
      if (['-r', '--require', '--import', '--loader', '--experimental-loader'].includes(token)) {
        const preload = tokens[++index]
        if (!preload) {
          return [{ error: `node ${token} has a missing local script operand` }]
        }
        if (looksLikeLocalScriptOperand(preload)) {
          operands.push({ operand: preload })
        }
        continue
      }
      if (token.startsWith('--require=') || token.startsWith('--import=') ||
          token.startsWith('--loader=') || token.startsWith('--experimental-loader=')) {
        const preload = token.slice(token.indexOf('=') + 1)
        if (looksLikeLocalScriptOperand(preload)) {
          operands.push({ operand: preload })
        }
        continue
      }
      if (token === '--test') {
        testMode = true
        continue
      }
      if (token.startsWith('-')) {
        continue
      }
      if (looksLikeLocalScriptOperand(token)) {
        operands.push({ operand: token })
      }
      if (!testMode) {
        break
      }
    }
    return operands.length ? operands : [{ error: 'node command has no bounded local script operand' }]
  }

  if (['tsx', 'python', 'python3', 'bash', 'sh'].includes(runtime)) {
    for (const token of tokens.slice(runtimeIndex + 1)) {
      if (token === '-m' || token === '-c') {
        return operands
      }
      if (!token.startsWith('-')) {
        return looksLikeLocalScriptOperand(token) ? [{ operand: token }] : operands
      }
    }
  }

  if (['powershell', 'pwsh'].includes(runtime)) {
    const fileIndex = tokens.findIndex((token, index) =>
      index > runtimeIndex && /^-(?:file|f)$/i.test(token)
    )
    if (fileIndex !== -1) {
      return tokens[fileIndex + 1]
        ? [{ operand: tokens[fileIndex + 1] }]
        : [{ error: `${runtime} -File has a missing local script operand` }]
    }
  }

  return operands
}

function resolveRepositoryDelegations(repoRoot, repositoryFiles) {
  const fileSet = new Set(repositoryFiles)
  const packageRecords = new Map()
  const violations = []
  const delegatedFiles = new Set()
  const repoRealPath = realpathSync(repoRoot)

  for (const packagePath of GOVERNED_PACKAGE_PATHS) {
    if (!fileSet.has(packagePath)) {
      continue
    }

    const source = readFileSync(join(repoRoot, ...packagePath.split('/')), 'utf8')
    let parsed
    try {
      parsed = JSON.parse(source)
    } catch {
      violations.push(delegationViolation(packagePath, source, 'package.json is malformed'))
      continue
    }

    const scripts = parsed?.scripts ?? {}
    if (!scripts || typeof scripts !== 'object' || Array.isArray(scripts)) {
      violations.push(delegationViolation(packagePath, source, 'scripts must be a JSON object', '"scripts"'))
      continue
    }

    const validScripts = new Map()
    for (const [name, command] of Object.entries(scripts)) {
      if (typeof command !== 'string') {
        violations.push(delegationViolation(
          packagePath,
          source,
          `script ${JSON.stringify(name)} must be a string`,
          JSON.stringify(name)
        ))
      } else {
        validScripts.set(name, command)
      }
    }

    packageRecords.set(packagePath, {
      path: packagePath,
      directory: dirname(packagePath).replaceAll('\\', '/').replace(/^\.$/, ''),
      name: typeof parsed?.name === 'string' ? parsed.name : null,
      scripts: validScripts,
      source
    })
  }

  const packageByDirectory = new Map(
    [...packageRecords.values()].map(record => [record.directory.toLowerCase(), record])
  )
  const packageByName = new Map(
    [...packageRecords.values()].filter(record => record.name).map(record => [record.name, record])
  )
  const lexicalPackageTarget = (record, packageDirectory) => {
    if (!packageDirectory || isDynamicDelegationOperand(packageDirectory) || isAbsolute(packageDirectory)) {
      return { target: null, known: false, error: 'package target is dynamic, absolute, or ambiguous' }
    }
    const absolute = resolve(
      repoRoot,
      ...record.directory.split('/').filter(Boolean),
      packageDirectory
    )
    const relativePath = relative(repoRoot, absolute).replaceAll('\\', '/')
    if (relativePath.startsWith('../') || isAbsolute(relativePath)) {
      return { target: null, known: false, error: 'package target escapes the repository' }
    }
    const normalized = relativePath ? normalizeRelativePath(relativePath) : ''
    const packagePath = normalized ? `${normalized}/package.json` : ROOT_PACKAGE_JSON
    return {
      target: packageByDirectory.get(normalized.toLowerCase()) ?? null,
      known: fileSet.has(packagePath),
      error: null
    }
  }
  const targetPackages = (record, invocation) => {
    if (invocation.prefix) {
      const resolution = lexicalPackageTarget(record, invocation.prefix)
      return {
        targets: resolution.target ? [resolution.target] : [],
        error: resolution.error ?? (resolution.known ? null : 'package directory does not contain package.json')
      }
    }
    if (invocation.workspace) {
      const namedTarget = packageByName.get(invocation.workspace)
      if (namedTarget) {
        return { targets: [namedTarget], error: null }
      }
      const resolution = lexicalPackageTarget(record, invocation.workspace)
      return {
        targets: resolution.target ? [resolution.target] : [],
        error: resolution.error ?? (resolution.known ? null : 'workspace target is unknown or ambiguous')
      }
    }
    if (invocation.allWorkspaces) {
      return {
        targets: [...packageRecords.values()].filter(candidate => candidate.path !== ROOT_PACKAGE_JSON),
        error: null
      }
    }
    return { targets: [record], error: null }
  }

  const addHelper = (record, command, operand) => {
    if (!operand || isDynamicDelegationOperand(operand) || isAbsolute(operand) || /^[A-Za-z]+:/.test(operand)) {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `local script operand ${JSON.stringify(operand)} is dynamic, absolute, or ambiguous`,
        command
      ))
      return
    }

    const absolute = resolve(repoRoot, ...record.directory.split('/').filter(Boolean), operand)
    const relativePath = relative(repoRoot, absolute).replaceAll('\\', '/')
    if (!relativePath || relativePath.startsWith('../') || isAbsolute(relativePath)) {
      violations.push(delegationViolation(record.path, record.source, 'local script operand escapes the repository', command))
      return
    }

    const normalized = normalizeRelativePath(relativePath)
    const extension = extname(normalized).toLowerCase()
    if (extension && !EXECUTABLE_EXTENSIONS.has(extension)) {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `local script operand ${JSON.stringify(normalized)} has an unsupported type`,
        command
      ))
      return
    }
    if (!fileSet.has(normalized)) {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `local script operand ${JSON.stringify(normalized)} is not tracked or nonignored`,
        command
      ))
      return
    }

    let metadata
    let realPath
    try {
      metadata = lstatSync(absolute)
      realPath = realpathSync(absolute)
    } catch {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `local script operand ${JSON.stringify(normalized)} cannot be read`,
        command
      ))
      return
    }

    const realRelative = relative(repoRealPath, realPath).replaceAll('\\', '/')
    if (!metadata.isFile() || metadata.isSymbolicLink() ||
        !realRelative || realRelative.startsWith('../') || isAbsolute(realRelative)) {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `local script operand ${JSON.stringify(normalized)} is not a contained regular file`,
        command
      ))
      return
    }

    delegatedFiles.add(normalized)
  }

  const states = new Map()
  let visitedNodes = 0
  const walkScript = (record, scriptName, depth, parentCommand = '') => {
    const key = `${record.path}\0${scriptName}`
    if (depth > MAX_DELEGATION_DEPTH || ++visitedNodes > MAX_DELEGATION_NODES) {
      violations.push(delegationViolation(
        record.path,
        record.source,
        'delegation exceeds the bounded recursion or node limit',
        parentCommand || JSON.stringify(scriptName)
      ))
      return
    }
    if (states.get(key) === 'visiting') {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `package-script cycle reaches ${JSON.stringify(scriptName)}`,
        parentCommand || JSON.stringify(scriptName)
      ))
      return
    }
    if (states.get(key) === 'done') {
      return
    }

    const command = record.scripts.get(scriptName)
    if (typeof command !== 'string') {
      violations.push(delegationViolation(
        record.path,
        record.source,
        `delegated script ${JSON.stringify(scriptName)} does not exist or is not a string`,
        parentCommand || JSON.stringify(scriptName)
      ))
      return
    }

    states.set(key, 'visiting')
    const parsed = shellCommandClauses(command)
    if (parsed.error) {
      violations.push(delegationViolation(record.path, record.source, parsed.error, command))
      states.set(key, 'done')
      return
    }

    const followScript = (target, targetName) => {
      for (const lifecycleName of [`pre${targetName}`, targetName, `post${targetName}`]) {
        if (lifecycleName === targetName || target.scripts.has(lifecycleName)) {
          walkScript(target, lifecycleName, depth + 1, command)
        }
      }
    }

    for (const clause of parsed.clauses) {
      for (const token of clause) {
        if (/^npm:[^\s]+$/i.test(token)) {
          followScript(record, token.slice(4))
        }
      }

      for (let index = 0; index < clause.length; index++) {
        const name = commandName(clause[index])
        if (['npm', 'pnpm', 'yarn'].includes(name)) {
          const invocation = packageManagerInvocation(clause, index)
          if (invocation.error) {
            violations.push(delegationViolation(record.path, record.source, invocation.error, command))
            continue
          }

          const targetResolution = targetPackages(record, invocation)
          if (targetResolution.error) {
            violations.push(delegationViolation(
              record.path,
              record.source,
              `${name} ${targetResolution.error}`,
              command
            ))
            continue
          }
          const targets = targetResolution.targets
          const words = invocation.words
          const runIndex = words.findIndex(word => /^(?:run|run-script)$/i.test(word))
          const implicitScript = ['start', 'stop', 'restart', 'test'].includes(words[0])
            ? words[0]
            : null
          const directManagerScript = runIndex === -1 && ['pnpm', 'yarn'].includes(name) &&
            words[0] && !/^(?:add|audit|ci|exec|install|publish|remove|update|why)$/i.test(words[0])
            ? words[0]
            : null
          const targetScript = runIndex === -1
            ? implicitScript ?? directManagerScript
            : words[runIndex + 1]
          if (runIndex !== -1 && !targetScript) {
            violations.push(delegationViolation(
              record.path,
              record.source,
              `${name} run delegation has a missing script name`,
              command
            ))
          } else if (targetScript) {
            for (const target of targets) {
              followScript(target, targetScript)
            }
          } else if (['ci', 'install'].includes(words[0]) ||
                     (name === 'yarn' && words.length === 0)) {
            for (const target of targets) {
              for (const lifecycleName of ['preinstall', 'install', 'postinstall', 'prepare']) {
                if (target.scripts.has(lifecycleName)) {
                  walkScript(target, lifecycleName, depth + 1, command)
                }
              }
            }
          }
        }

        if (['node', 'tsx', 'python', 'python3', 'bash', 'sh', 'powershell', 'pwsh'].includes(name)) {
          for (const candidate of runtimeScriptOperands(clause, index)) {
            if (candidate.error) {
              violations.push(delegationViolation(record.path, record.source, candidate.error, command))
            } else {
              addHelper(record, command, candidate.operand)
            }
          }
        }
      }

      const first = clause[0]
      if (first && /^(?:\.\.?[\\/]|[^\s]+[\\/])/.test(first) &&
          looksLikeLocalScriptOperand(first)) {
        addHelper(record, command, first)
      }
    }

    states.set(key, 'done')
  }

  for (const record of packageRecords.values()) {
    for (const scriptName of record.scripts.keys()) {
      walkScript(record, scriptName, 0)
    }
  }

  return {
    files: delegatedFiles,
    violations: uniqueViolations(violations)
  }
}

function workflowLineRecords(source) {
  return [...source.matchAll(/[^\r\n]*(?:\r?\n|$)/g)]
    .filter(match => match[0])
    .map(match => ({
      index: match.index,
      raw: match[0].replace(/\r?\n$/, ''),
      indent: /^(?: *)/.exec(match[0])[0].length
    }))
}

function workflowRunCommand(stepSource) {
  const lines = workflowLineRecords(stepSource)
  for (let index = 0; index < lines.length; index++) {
    const match = /^\s*(?:-\s*)?run\s*:\s*(.*?)\s*$/.exec(lines[index].raw)
    if (!match) {
      continue
    }
    if (!/^[|>][-+]?\s*$/.test(match[1])) {
      return match[1].replace(/^(['"])([\s\S]*)\1$/, '$2')
    }

    const nested = []
    for (const line of lines.slice(index + 1)) {
      if (line.raw.trim() && line.indent <= lines[index].indent) {
        break
      }
      if (line.raw.trim()) {
        nested.push(line)
      }
    }
    if (!nested.length) {
      return ''
    }
    const indentation = Math.min(...nested.map(line => line.indent))
    return nested.map(line => line.raw.slice(indentation)).join('\n')
  }
  return null
}

function workflowJobRecords(source) {
  const lines = workflowLineRecords(source)
  const jobsLines = lines.filter(line => /^\s*jobs\s*:\s*(?:#.*)?$/.test(line.raw))
  if (jobsLines.length !== 1 || /^\s/.test(jobsLines[0].raw)) {
    return { jobs: [], error: 'workflow must contain one unambiguous top-level jobs mapping' }
  }

  const jobsLine = jobsLines[0]
  const body = lines.filter(line => line.index > jobsLine.index)
  const candidateIndents = body
    .filter(line => line.raw.trim() && line.indent > jobsLine.indent)
    .map(line => line.indent)
  if (!candidateIndents.length) {
    return { jobs: [], error: 'workflow jobs mapping is empty' }
  }
  const jobIndent = Math.min(...candidateIndents)
  const headers = body.filter(line =>
    line.indent === jobIndent && /^\s*[A-Za-z0-9_-]+\s*:\s*(?:#.*)?$/.test(line.raw)
  )
  if (!headers.length) {
    return { jobs: [], error: 'workflow job keys are malformed or ambiguous' }
  }

  const jobs = headers.map((header, index) => {
    const end = headers[index + 1]?.index ?? source.length
    return {
      index: header.index,
      bodyIndex: header.index + header.raw.length,
      source: source.slice(header.index, end)
    }
  })
  return { jobs, error: null }
}

function workflowStepRecords(jobSource) {
  const lines = workflowLineRecords(jobSource)
  const stepsLines = lines.filter(line => /^\s*steps\s*:\s*(?:#.*)?$/.test(line.raw))
  if (!stepsLines.length) {
    return []
  }
  if (stepsLines.length !== 1) {
    return null
  }
  const stepsLine = stepsLines[0]
  const body = lines.filter(line => line.index > stepsLine.index && line.indent > stepsLine.indent)
  const stepIndents = body.filter(line => /^\s*-\s+/.test(line.raw)).map(line => line.indent)
  if (!stepIndents.length) {
    return null
  }
  const stepIndent = Math.min(...stepIndents)
  const headers = body.filter(line => line.indent === stepIndent && /^\s*-\s+/.test(line.raw))

  return headers.map((header, index) => ({
    index: header.index,
    source: jobSource.slice(header.index, headers[index + 1]?.index ?? jobSource.length)
  }))
}

function isCandidateRelevantWorkflow(source, relativePath) {
  return relativePath === '.github/workflows/js-tests.yml' ||
    /apps[\\/]desktop[\\/]scripts[\\/]check-verifier-process-safety\.mjs/i.test(source)
}

function isCandidateControlledWorkflowCommand(command) {
  if (!command.trim()) {
    return true
  }
  if (isInertOutputCommand(command.trim())) {
    return false
  }

  return /\b(?:npm|npx|pnpm|yarn)(?:\.cmd|\.exe)?\b/i.test(command) ||
    /\b(?:node|tsx)(?:\.exe)?\b/i.test(command) ||
    /\b(?:python3?|bash|sh|pwsh|powershell)(?:\.exe)?\s+(?:-(?:c|command)\b|\.\.?[\\/]|[^\s;|&]+[\\/])/i.test(command) ||
    /\b(?:pip3?|uv|poetry)(?:\.exe)?\s+(?:install|sync|run)\b/i.test(command) ||
    /(?:^|[;&|\r\n]\s*)\.\.?[\\/][^\s;|&]+/i.test(command)
}

function workflowPreflightViolations(source, relativePath) {
  if (!isRelevantWorkflow(relativePath) || !isCandidateRelevantWorkflow(source, relativePath)) {
    return []
  }

  const parsed = workflowJobRecords(source)
  if (parsed.error) {
    return [{ file: relativePath, line: 1, reason: `${PREFLIGHT_ORDER_REASON}: ${parsed.error}` }]
  }

  const violations = []
  for (const job of parsed.jobs) {
    const jobLevelUses = /^\s*uses\s*:\s*(.*?)\s*$/m.exec(job.source)?.[1]
      ?.replace(/^(['"])([\s\S]*)\1$/, '$2')
    if (jobLevelUses?.startsWith('./')) {
      violations.push({
        file: relativePath,
        line: lineNumberAt(source, job.bodyIndex),
        reason: PREFLIGHT_ORDER_REASON
      })
      continue
    }

    const steps = workflowStepRecords(job.source)
    if (steps === null) {
      violations.push({
        file: relativePath,
        line: lineNumberAt(source, job.bodyIndex),
        reason: `${PREFLIGHT_ORDER_REASON}: steps are malformed or ambiguous`
      })
      continue
    }

    let scannerSeen = false
    let unsafeOrdering = false
    for (const step of steps) {
      const usesMatch = /^\s*(?:-\s*)?uses\s*:\s*(.*?)\s*$/m.exec(step.source)
      const runCommand = workflowRunCommand(step.source)
      const uses = usesMatch?.[1]?.replace(/^(['"])([\s\S]*)\1$/, '$2') ?? null
      const safeRemoteUses = uses &&
        /^(?:actions\/(?:checkout|setup-node))@[A-Za-z0-9_.-]+(?:\s+#.*)?$/i.test(uses)
      const candidateUses = uses !== null && !safeRemoteUses

      if (runCommand !== null && DIRECT_SCANNER_COMMAND.test(runCommand)) {
        scannerSeen = true
      } else if (candidateUses ||
                 (runCommand !== null && isCandidateControlledWorkflowCommand(runCommand))) {
        if (!scannerSeen) {
          unsafeOrdering = true
          break
        }
      }
    }

    if (unsafeOrdering) {
      violations.push({
        file: relativePath,
        line: lineNumberAt(source, job.bodyIndex),
        reason: PREFLIGHT_ORDER_REASON
      })
    }
  }

  return uniqueViolations(violations)
}

function preflightOrderingViolations(source, relativePath) {
  if (relativePath === DESKTOP_PACKAGE_JSON) {
    let parsed

    try {
      parsed = JSON.parse(source)
    } catch {
      return [{ file: relativePath, line: 1, reason: PREFLIGHT_ORDER_REASON }]
    }

    const command = parsed?.scripts?.['check:process-safety']
    if (command === undefined) {
      return []
    }
    const scannerFirst = typeof command === 'string' &&
      /^\s*node(?:\.exe)?\s+scripts[\\/]check-verifier-process-safety\.mjs(?:\s|&&|$)/i.test(command)

    return scannerFirst
      ? []
      : [{
          file: relativePath,
          line: lineNumberAt(source, Math.max(0, source.indexOf('"check:process-safety"'))),
          reason: PREFLIGHT_ORDER_REASON
        }]
  }

  return workflowPreflightViolations(source, relativePath)
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

function isTestSourcePath(relativePath) {
  return /(?:^|\/)(?:e2e|test|tests)\//i.test(relativePath) ||
    /(?:^|\.)(?:node-test|spec|test)\.[^/]+$/i.test(fileName(relativePath))
}

function extractCall(source, codeSource, start) {
  const open = codeSource.indexOf('(', start)

  if (open === -1) {
    return source.slice(start, Math.min(source.length, start + 800))
  }

  let depth = 0
  for (let index = open; index < codeSource.length; index++) {
    if (codeSource[index] === '(') {
      depth++
    } else if (codeSource[index] === ')') {
      depth--
      if (depth === 0) {
        return source.slice(start, index + 1)
      }
    }
  }

  return source.slice(start, Math.min(source.length, start + 1600))
}

function extractSourceOperand(source, codeSource, start) {
  const open = codeSource.indexOf('(', start)

  if (open === -1) {
    return ''
  }

  let parentheses = 1
  let brackets = 0
  let braces = 0
  for (let index = open + 1; index < codeSource.length; index++) {
    const character = codeSource[index]

    if (character === '(') {
      parentheses++
    } else if (character === ')') {
      parentheses--
      if (parentheses === 0) {
        return source.slice(open + 1, index)
      }
    } else if (character === '[') {
      brackets++
    } else if (character === ']') {
      brackets--
    } else if (character === '{') {
      braces++
    } else if (character === '}') {
      braces--
    } else if (character === ',' && parentheses === 1 && brackets === 0 && braces === 0) {
      return source.slice(open + 1, index)
    }
  }

  return source.slice(open + 1, Math.min(source.length, open + 1600))
}

function sourceReadingTestViolations(source, relativePath) {
  if (!isTestSourcePath(relativePath)) {
    return []
  }

  const extension = extname(relativePath).toLowerCase()
  const masked = maskComments(source, relativePath)
  const codeSource = maskStringContents(masked, relativePath)
  const sourcePathPattern = /(?:^|[\\/'"`])[^\r\n'"`]*\.(?:cjs|cs|js|mjs|ps1|py|ts|tsx)(?=$|[?#:'"`)\],])/i
  const fixturePathPattern = /(?:^|[\\/])(?:fixtures?|synthetic)(?:[\\/]|$)/i
  const pathBindings = new Map(collectLiteralAssignments(masked))
  const violations = []

  for (const assignment of masked.matchAll(
    /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[^\r\n]{0,1200}/g
  )) {
    if (codeSource.slice(assignment.index, assignment.index + assignment[0].length).trim() &&
        sourcePathPattern.test(assignment[0])) {
      pathBindings.set(assignment[1], assignment[0])
    }
  }

  const addIfProductionSource = (index, call, sourceOperand = call) => {
    let resolved = sourceOperand
    for (const [name, value] of pathBindings) {
      if (new RegExp(`\\b${escapeRegExp(name)}\\b`).test(sourceOperand)) {
        resolved += ` ${value}`
      }
    }

    sourcePathPattern.lastIndex = 0
    fixturePathPattern.lastIndex = 0
    if (sourcePathPattern.test(resolved) && !fixturePathPattern.test(resolved)) {
      violations.push({
        file: relativePath,
        line: lineNumberAt(source, index),
        reason: SOURCE_READING_TEST_REASON
      })
    }
  }

  if (['.cjs', '.js', '.mjs', '.ts', '.tsx'].includes(extension)) {
    const accessNames = new Set([
      'copyFile',
      'copyFileSync',
      'createReadStream',
      'readFile',
      'readFileSync'
    ])
    const namespaces = new Set()

    for (const declaration of masked.matchAll(
      /\bimport\s*\{([^}]+)\}\s*from\s*['"]node:fs(?:\/promises)?['"]/g
    )) {
      if (!codeSource.slice(declaration.index, declaration.index + 6).includes('import')) {
        continue
      }
      for (const binding of declaration[1].split(',')) {
        const parts = binding.trim().split(/\s+as\s+/)
        if (accessNames.has(parts[0])) {
          accessNames.add(parts.at(-1))
        }
      }
    }
    for (const declaration of masked.matchAll(
      /\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\s*\(\s*['"]node:fs(?:\/promises)?['"]\s*\)/g
    )) {
      if (!codeSource.slice(declaration.index, declaration.index + declaration[0].length).trim()) {
        continue
      }
      for (const binding of declaration[1].split(',')) {
        const parts = binding.trim().split(/\s*:\s*/)
        if (accessNames.has(parts[0])) {
          accessNames.add(parts.at(-1))
        }
      }
    }
    for (const declaration of masked.matchAll(
      /\bimport\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+['"]node:fs(?:\/promises)?['"]/g
    )) {
      if (codeSource.slice(declaration.index, declaration.index + 6).includes('import')) {
        namespaces.add(declaration[1])
      }
    }
    for (const declaration of masked.matchAll(
      /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*['"]node:fs(?:\/promises)?['"]\s*\)/g
    )) {
      if (codeSource.slice(declaration.index, declaration.index + declaration[0].length).trim()) {
        namespaces.add(declaration[1])
      }
    }

    const localPattern = new RegExp(
      `\\b(?:${[...accessNames].map(escapeRegExp).join('|')})\\s*\\(`,
      'g'
    )
    for (const match of codeSource.matchAll(localPattern)) {
      const call = extractCall(masked, codeSource, match.index)
      addIfProductionSource(
        match.index,
        call,
        extractSourceOperand(masked, codeSource, match.index)
      )
    }

    if (namespaces.size) {
      const namespacePattern = new RegExp(
        `\\b(?:${[...namespaces].map(escapeRegExp).join('|')})\\s*\\.\\s*` +
        `(?:${[...accessNames].map(escapeRegExp).join('|')})\\s*\\(`,
        'g'
      )
      for (const match of codeSource.matchAll(namespacePattern)) {
        const call = extractCall(masked, codeSource, match.index)
        addIfProductionSource(
          match.index,
          call,
          extractSourceOperand(masked, codeSource, match.index)
        )
      }
    }
  } else {
    const patterns = extension === '.py'
      ? [
          /\bopen\s*\(/g,
          /\.(?:read_bytes|read_text)\s*\(/g,
          /\bshutil\.(?:copy|copy2|copyfile)\s*\(/g
        ]
      : extension === '.cs'
        ? [/\bFile\.(?:Copy|OpenRead|ReadAllBytes|ReadAllText|ReadLines)\s*\(/g]
        : extension === '.ps1'
          ? [/\b(?:Copy-Item|Get-Content)\b/gi]
          : []

    for (const pattern of patterns) {
      for (const match of codeSource.matchAll(pattern)) {
        const call = extractCall(masked, codeSource, match.index)
        addIfProductionSource(
          match.index,
          call,
          extractSourceOperand(masked, codeSource, match.index)
        )
      }
    }
  }

  return uniqueViolations(violations)
}

export function scanText(source, relativePath) {
  const normalizedPath = normalizeRelativePath(relativePath)
  const extension = extname(normalizedPath).toLowerCase()
  const nativeViolations = nativeProcessViolations(source, normalizedPath)
  const sourceReadingViolations = sourceReadingTestViolations(source, normalizedPath)
  const preflightViolations = preflightOrderingViolations(source, normalizedPath)
  const strictVerifierPolicy =
    isVerifierHarnessPath(normalizedPath) || normalizedPath === WINDOWS_JOB_HOST_PATH

  if (nativeViolations.length || sourceReadingViolations.length || preflightViolations.length) {
    return uniqueViolations([
      ...nativeViolations,
      ...sourceReadingViolations,
      ...preflightViolations
    ])
  }

  const hasSemanticVocabulary =
    SEMANTIC_PROCESS_VOCABULARY.test(source) ||
    /(?:task\s*(?:\^|["'`+\s])*kill|pki\s*["'`+\s]*ll|kill\s*["'`+\s]*all|Stop-[\s\S]{0,80}?Process)/i.test(source)

  if (![ROOT_PACKAGE_JSON, DESKTOP_PACKAGE_JSON].includes(normalizedPath) &&
      !isVerifierHarnessPath(normalizedPath) &&
      !hasSemanticVocabulary) {
    return []
  }

  const masked = maskComments(source, normalizedPath)
  const rawApiViolations = rawProcessApiViolations(
    masked,
    normalizedPath,
    strictVerifierPolicy
  )

  if (rawApiViolations.length) {
    return uniqueViolations(rawApiViolations)
  }

  const violations = []

  if ([ROOT_PACKAGE_JSON, DESKTOP_PACKAGE_JSON].includes(normalizedPath)) {
    addPackageScriptViolations(violations, source, normalizedPath)
  } else if (!hasSemanticVocabulary) {
    return []
  } else if (['.cjs', '.js', '.mjs', '.py', '.ts', '.tsx'].includes(extension)) {
    addInvocationViolations(violations, masked, normalizedPath, strictVerifierPolicy)
  } else if (['.bash', '.bat', '.cmd', '.ps1', '.sh', '.yaml', '.yml'].includes(extension)) {
    addShellViolations(violations, masked, normalizedPath, strictVerifierPolicy)
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

  const repositoryFiles = tracked.stdout
    .split('\0')
    .filter(Boolean)
    .map(normalizeRelativePath)
    .sort()
  const delegations = resolveRepositoryDelegations(repoRoot, repositoryFiles)
  const files = [...new Set([
    ...repositoryFiles.filter(shouldScanRepositoryPath),
    ...delegations.files
  ])].sort()
  const violations = uniqueViolations([
    ...delegations.violations,
    ...files.flatMap(relativePath =>
      scanText(readFileSync(join(repoRoot, ...relativePath.split('/')), 'utf8'), relativePath)
    )
  ])

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
