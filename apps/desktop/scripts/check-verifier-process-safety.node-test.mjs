import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import {
  formatViolations,
  scanRepository,
  scanText,
  shouldScanRepositoryPath
} from './check-verifier-process-safety.mjs'

const JOB_HOST_PATH = 'apps/desktop/scripts/windows-verifier-job-host.cs'
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
const SYNTHETIC_JOB_HOST_SOURCE = `
class SyntheticJobHost {
  public int TerminationTimeoutMs;
  const uint JobObjectLimitKillOnJobClose = 0x00002000;
  private static extern bool TerminateProcess(IntPtr process, uint exitCode);
  void Run(dynamic record, dynamic request, dynamic job, dynamic process) {
    job = CreateJobObjectW(IntPtr.Zero, null);
    ConfigureKillOnClose(job);
    process = CreateSuspendedProcess(request);
    if (!AssignProcessToJobObject(job, process.hProcess)) { throw new Exception(); }
    uint resumeResult = ResumeThread(process.hThread);
    CreateProcessW(
      request.Executable,
      commandLine,
      IntPtr.Zero,
      IntPtr.Zero,
      false,
      CreateSuspended | CreateUnicodeEnvironment,
      environment,
      request.WorkingDirectory,
      ref startup,
      out process);
    SetInformationJobObject(job, 9, buffer, 1);
    QueryInformationJobObject(job, 1, buffer, 1, IntPtr.Zero);
    GetProcessTimes(process.hProcess, out a, out b, out c, out d);
    QueryFullProcessImageNameW(process.hProcess, 0, path, ref size);
    CloseHandle(process.hThread);
  }
  private static void TerminateSuspendedPreAssignment(IntPtr process) {
    TerminateProcess(process, unchecked((uint)ErrorExitCode));
    WaitForSingleObject(process, 5000);
  }
  private static int CommandLoop(dynamic job, dynamic request) {
    TerminateAssignedJob(job);
    WaitForZeroActive(job, request.TerminationTimeoutMs);
    TerminateJobObject(job, 1);
    QueryInformationJobObject(job, 1, buffer, 1, IntPtr.Zero);
    if (QueryAccounting(job).ActiveProcesses == 0) {
      var receipt = new Dictionary<string, object> { { "activeProcesses", 0 } };
    }
    return 0;
  }
  LaunchRequest ParseLaunchRequest(dynamic record) {
    var executable = RequireAbsoluteExistingFile(
      RequireString(record, "executable", "launch record"),
      "executable");
    return null;
  }
  IntPtr CreateJobObjectW(IntPtr attributes, string name) { return IntPtr.Zero; }
  bool SetInformationJobObject(IntPtr job, int kind, IntPtr data, uint size) { return true; }
  bool CreateProcessW(string executable, dynamic commandLine, IntPtr a, IntPtr b,
    bool inherit, uint flags, IntPtr environment, string cwd, ref dynamic startup,
    out dynamic process) { process = null; return true; }
  bool AssignProcessToJobObject(IntPtr job, IntPtr process) { return true; }
  uint ResumeThread(IntPtr thread) { return 0; }
  bool TerminateJobObject(IntPtr job, uint code) { return true; }
  bool QueryInformationJobObject(IntPtr job, int kind, IntPtr data, uint size,
    IntPtr returned) { return true; }
  bool GetProcessTimes(IntPtr process, out dynamic a, out dynamic b,
    out dynamic c, out dynamic d) { a = b = c = d = null; return true; }
  bool QueryFullProcessImageNameW(IntPtr process, uint flags, dynamic path,
    ref uint size) { return true; }
  uint WaitForSingleObject(IntPtr process, uint timeout) { return 0; }
  bool CloseHandle(IntPtr handle) { return true; }
}
`

const REJECT_CASES = [
  {
    name: 'Node dynamic concatenation through imported spawn alias',
    path: 'apps/desktop/scripts/verify-dynamic.mjs',
    source: "import { spawn as launch } from 'node:child_process'\nlaunch('task' + 'kill', ['/IM', 'Hermes.exe'])"
  },
  {
    name: 'Node imported execFile alias',
    path: 'apps/desktop/scripts/desktop-verifier-helper.mjs',
    source: "import { execFile as invoke } from 'node:child_process'\ninvoke('taskkill.exe', ['/FI', 'IMAGENAME eq Hermes.exe'])"
  },
  {
    name: 'Node required child_process destructuring',
    path: 'apps/desktop/scripts/verify-required.cjs',
    source: "const { spawnSync: go } = require('node:child_process')\ngo('pkill', ['Hermes'])"
  },
  {
    name: 'Node harmless spawn still fails closed in a new verifier helper',
    path: 'apps/desktop/scripts/new-verifier-helper.mjs',
    source: "import { spawn } from 'node:child_process'\nspawn('git', ['status'])"
  },
  {
    name: 'Node direct process.kill',
    path: 'apps/desktop/scripts/verify-kill.mjs',
    source: 'process.kill(candidatePid, "SIGTERM")'
  },
  {
    name: 'Node namespace API reassigned to a local alias',
    path: 'apps/desktop/src/process-cleanup.test.ts',
    source: [
      "import * as cp from 'node:child_process'",
      'const invoke = cp.spawnSync',
      "invoke('taskkill.exe', ['/IM', 'Hermes.exe', '/F'])"
    ].join('\n')
  },
  {
    name: 'Node executable call inside template interpolation',
    path: 'apps/desktop/src/process-cleanup.test.ts',
    source: [
      "import * as cp from 'node:child_process'",
      'const receipt = `result=${cp.spawnSync(\'taskkill.exe\', [\'/IM\', \'Hermes.exe\', \'/F\'])}`'
    ].join('\n')
  },
  {
    name: 'Python dynamic concatenation',
    path: 'apps/desktop/scripts/verify_dynamic.py',
    source: 'import subprocess\nsubprocess.run(["task" + "kill", "/IM", "Hermes.exe"])'
  },
  {
    name: 'Python from subprocess import run',
    path: 'apps/desktop/scripts/verify_import.py',
    source: 'from subprocess import run\nrun(["git", "status"])'
  },
  {
    name: 'Python aliased Popen',
    path: 'apps/desktop/scripts/desktop_verifier_helper.py',
    source: 'from subprocess import Popen as launch\nlaunch(["killall", "Hermes"])'
  },
  {
    name: 'Python os.kill',
    path: 'apps/desktop/scripts/verify_signal.py',
    source: 'import os\nos.kill(candidate_pid, 15)'
  },
  {
    name: 'shell function trap',
    path: 'scripts/verify-desktop.sh',
    source: "cleanup() { pkill Hermes; }\ntrap cleanup EXIT"
  },
  {
    name: 'shell variable command',
    path: 'scripts/desktop-verifier.sh',
    source: "killer='pki''ll'\n\"$killer\" Hermes"
  },
  {
    name: 'shell command substitution',
    path: 'scripts/verify-desktop.sh',
    source: 'killer=$(printf pki; printf ll)\n"$killer" Hermes'
  },
  {
    name: 'shell harmless external process still fails closed',
    path: 'scripts/verify-desktop.sh',
    source: 'git status --short'
  },
  {
    name: 'shell nested PowerShell termination',
    path: 'scripts/verify-desktop.sh',
    source: 'pwsh -Command "Stop-Process -Name Hermes -Force"'
  },
  {
    name: 'shell quoted taskkill image filter',
    path: 'scripts/verify-desktop.sh',
    source: "taskkill.exe /FI 'IMAGENAME eq Hermes.exe' /F"
  },
  {
    name: 'CMD inline taskkill image selector',
    path: 'scripts/desktop-verifier.cmd',
    source: '@echo off & taskkill /IM Hermes.exe /T /F'
  },
  {
    name: 'CMD nested taskkill filter',
    path: 'scripts/verify-desktop.cmd',
    source: 'cmd.exe /d /c "taskkill /FI \\"IMAGENAME eq Hermes.exe\\" /F"'
  },
  {
    name: 'CMD harmless external process still fails closed',
    path: 'scripts/verify-desktop.cmd',
    source: 'git status --short'
  },
  {
    name: 'PowerShell foreach collection kill',
    path: 'scripts/desktop-verifier.ps1',
    source: 'Get-Process Hermes | ForEach-Object { $_.Kill() }'
  },
  {
    name: 'PowerShell Stop-Process alias',
    path: 'scripts/verify-desktop.ps1',
    source: 'spps -Name Hermes -Force'
  },
  {
    name: 'PowerShell nested CMD taskkill',
    path: 'scripts/desktop-verifier.ps1',
    source: 'cmd /c "taskkill /IM Hermes.exe /F"'
  },
  {
    name: 'PowerShell Remove-CimInstance',
    path: 'scripts/verify-desktop.ps1',
    source: 'Get-CimInstance Win32_Process | Where-Object Name -eq Hermes.exe | Remove-CimInstance'
  },
  {
    name: 'PowerShell raw Start-Process',
    path: 'scripts/desktop-verifier.ps1',
    source: 'Start-Process git -ArgumentList status -Wait'
  },
  {
    name: 'PowerShell direct external process still fails closed',
    path: 'scripts/desktop-verifier.ps1',
    source: 'git status --short'
  },
  {
    name: 'workflow nested PowerShell termination',
    path: '.github/workflows/verify.yml',
    source: 'run: pwsh -Command "Stop-Process -Name Hermes -Force"'
  },
  {
    name: 'workflow split shell process-name command alias',
    path: '.github/workflows/verify.yml',
    source: "run: |\n  killer='pki''ll'\n  \"$killer\" Hermes"
  },
  {
    name: 'Node computed namespace member alias',
    path: 'apps/desktop/src/computed-process.test.ts',
    source: "import * as cp from 'node:child_process'\nconst invoke = cp['spawnSync']\ninvoke('taskkill.exe', ['/FI', 'IMAGENAME eq Hermes.exe', '/F'])"
  },
  {
    name: 'Python subprocess module alias',
    path: 'apps/desktop/scripts/process_probe.py',
    source: "import subprocess as sp\nsp.run(['taskkill.exe', '/IM', 'Hermes.exe', '/F'])"
  },
  {
    name: 'workflow CMD caret-split taskkill',
    path: '.github/workflows/verify.yml',
    source: 'run: cmd /d /c "task^kill /IM Hermes.exe /F"'
  },
  {
    name: 'workflow concatenated PowerShell cmdlet',
    path: '.github/workflows/verify.yml',
    source: "run: pwsh -Command \"& ('Stop-' + 'Process') -Name Hermes -Force\""
  },
  {
    name: 'workflow WMI class through a variable',
    path: '.github/workflows/verify.yml',
    source: "run: pwsh -Command \"$class='Win32_'+'Process'; Get-CimInstance $class | Remove-CimInstance\""
  },
  {
    name: 'Node command stored in an object property',
    path: 'apps/desktop/src/object-process.test.ts',
    source: "import * as cp from 'node:child_process'\nconst commands = { kill: 'taskkill.exe' }\ncp.spawnSync(commands.kill, ['/IM', 'Hermes.exe', '/F'])"
  },
  {
    name: 'Node command read from a bracket object property',
    path: 'apps/desktop/src/object-process.test.ts',
    source: "import * as cp from 'node:child_process'\nconst commands = { kill: 'taskkill.exe' }\ncp.spawnSync(commands['kill'], ['/IM', 'Hermes.exe', '/F'])"
  },
  {
    name: 'Python subprocess API assigned to a local alias',
    path: 'apps/desktop/scripts/process_probe.py',
    source: "import subprocess\ninvoke = subprocess.run\ninvoke(['taskkill.exe', '/IM', 'Hermes.exe', '/F'])"
  },
  {
    name: 'workflow PowerShell format-constructed cmdlet',
    path: '.github/workflows/verify.yml',
    source: "run: pwsh -Command \"& ('Stop-{0}' -f 'Process') -Name Hermes -Force\""
  },
  {
    name: 'taskkill even with an exact PID',
    path: 'apps/desktop/scripts/verify-owned.mjs',
    source: "import { spawnSync } from 'node:child_process'\nspawnSync('taskkill.exe', ['/PID', String(pid), '/T', '/F'])"
  },
  {
    name: 'PowerShell Stop-Process even with a PID',
    path: 'scripts/desktop-verifier.ps1',
    source: 'Stop-Process -Id $callerPid -Force'
  },
  {
    name: 'WMI/CIM process-tree reconstruction without an inline kill',
    path: 'scripts/desktop-verifier.ps1',
    source: 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId'
  },
  {
    name: 'native process API outside the exact checked-in Job host',
    path: 'apps/desktop/scripts/windows-verifier-job-host-copy.cs',
    source: '[DllImport("kernel32.dll")] private static extern IntPtr CreateJobObjectW(IntPtr a, string b);'
  },
  {
    name: 'C# native process controller hidden in a PowerShell here-string',
    path: 'apps/desktop/scripts/windows-verifier-wrapper.ps1',
    source: "$source = @'\n[DllImport(\"kernel32.dll\")] static extern bool OpenProcess();\n'@\nAdd-Type $source"
  }
]

const ALLOW_CASES = [
  {
    name: 'canonical owned Desktop launch',
    path: 'apps/desktop/scripts/desktop-verifier-lib.mjs',
    source: [
      "import { spawn as nodeSpawn } from 'node:child_process'",
      'export async function launchOwnedDesktop(spec, { spawnImpl = nodeSpawn } = {}) {',
      '  const child = spawnImpl(spec.executable, spec.args, spec.spawnOptions)',
      '  return child',
      '}'
    ].join('\n')
  },
  {
    name: 'canonical Windows Job controller launch',
    path: 'apps/desktop/scripts/desktop-verifier-lib.mjs',
    source: [
      'async function launchWindowsOwnedDesktop(spec, { spawnImpl }) {',
      "  return spawnImpl('powershell.exe', ['-File', 'windows-verifier-job-host.ps1'])",
      '}'
    ].join('\n')
  },
  {
    name: 'scanner-owned git enumeration',
    path: 'apps/desktop/scripts/check-verifier-process-safety.mjs',
    source: [
      "import { spawnSync } from 'node:child_process'",
      'export function scanRepository(repoRoot) {',
      "  return spawnSync('git', ['ls-files'])",
      '}'
    ].join('\n')
  },
  {
    name: 'ordinary Desktop runtime child launch outside verifier harness',
    path: 'apps/desktop/electron/backend-process.ts',
    source: "import { spawn } from 'node:child_process'\nspawn(command, args)"
  },
  {
    name: 'PowerShell here-string documentation',
    path: 'scripts/desktop-verifier.ps1',
    source: [
      "$documentation = @'",
      'Never run Stop-Process -Name Hermes.',
      'Never run cmd /c taskkill /IM Hermes.exe /F.',
      "'@",
      'Write-Output $documentation'
    ].join('\n')
  },
  {
    name: 'shell heredoc documentation',
    path: 'scripts/verify-desktop.sh',
    source: [
      "cat <<'DOC'",
      'Never run pkill Hermes.',
      'Never run taskkill /IM Hermes.exe /F.',
      'DOC'
    ].join('\n')
  },
  {
    name: 'package script routed through canonical verifier',
    path: 'apps/desktop/package.json',
    source: JSON.stringify({
      scripts: {
        'verify:desktop:isolated': 'node scripts/desktop-verifier.mjs'
      }
    })
  },
  {
    name: 'comments and warning strings',
    path: 'apps/desktop/scripts/verify-warning.mjs',
    source: [
      '// Never run taskkill.exe /IM Hermes.exe /F here.',
      'const warning = "Do not use Stop-Process -Name during verification"',
      'console.log(warning)'
    ].join('\n')
  },
  {
    name: 'PowerShell independent PID-only lookup without termination',
    path: 'scripts/desktop-status.ps1',
    source: 'Get-Process -Id $candidatePid'
  },
  {
    name: 'workflow echo warning text',
    path: '.github/workflows/verify.yml',
    source: 'run: echo "Never run Stop-Process -Name Hermes"'
  },
  {
    name: 'PowerShell warning-string assignment',
    path: 'scripts/desktop-verifier.ps1',
    source: '$warning = "Never run Stop-Process -Name Hermes"\nWrite-Output $warning'
  },
  {
    name: 'local JavaScript mock method named spawnSync',
    path: 'apps/desktop/src/process-policy.test.ts',
    source: "const fake = { spawnSync() { return { status: 0 } } }\nfake.spawnSync('taskkill.exe', ['/IM', 'Hermes.exe', '/F'])"
  },
  {
    name: 'workflow environment warning data',
    path: '.github/workflows/verify.yml',
    source: 'env:\n  PROCESS_POLICY_NOTE: "Never run Stop-Process -Name Hermes"\nsteps:\n  - run: echo "$PROCESS_POLICY_NOTE"'
  },
  {
    name: 'JavaScript regex policy text',
    path: 'apps/desktop/src/process-policy.test.ts',
    source: 'const policyExample = /taskkill \\/IM Hermes\\.exe/'
  },
  {
    name: 'PowerShell PID-only query alias',
    path: 'scripts/desktop-status.ps1',
    source: 'gps -Id $candidatePid'
  },
  {
    name: 'workflow nested Write-Host warning text',
    path: '.github/workflows/verify.yml',
    source: "run: pwsh -Command \"Write-Host 'Never run Stop-Process -Name Hermes'\""
  }
]

for (const fixture of REJECT_CASES) {
  test(`rejects ${fixture.name}`, () => {
    assert.notDeepEqual(scanText(fixture.source, fixture.path), [], fixture.name)
  })
}

for (const fixture of ALLOW_CASES) {
  test(`allows ${fixture.name}`, () => {
    assert.deepEqual(scanText(fixture.source, fixture.path), [], fixture.name)
  })
}

test('scans Desktop verification/test/perf/dev code, root harness scripts, workflows, and itself', () => {
  for (const relativePath of [
    'apps/desktop/src/main.tsx',
    'apps/desktop/electron/main.ts',
    'apps/desktop/e2e/launch-packaged-app.spec.ts',
    'apps/desktop/scripts/desktop-verifier-lib.mjs',
    'apps/desktop/scripts/check-verifier-process-safety.mjs',
    'apps/desktop/scripts/check-verifier-process-safety.node-test.mjs',
    'apps/desktop/scripts/windows-verifier-job-host.cs',
    'apps/desktop/scripts/windows-verifier-job-host-copy.cs',
    'apps/desktop/scripts/perf/lib/launch.mjs',
    'apps/desktop/scripts/verify-desktop.ps1',
    'apps/desktop/package.json',
    'apps/desktop/tsconfig.json',
    'scripts/desktop-verifier.ps1',
    'scripts/verify-desktop.py',
    'scripts/dev-desktop.mjs',
    'package.json',
    '.github/workflows/js-tests.yml',
    '.github/workflows/e2e-desktop.yaml',
    '.github/workflows/verify.yml',
    '.github/workflows/python-tests.yml',
    '.github/actions/verify/action.yml',
    '.github/actions/verify/action.yaml'
  ]) {
    assert.equal(shouldScanRepositoryPath(relativePath), true, relativePath)
  }

  for (const relativePath of [
    'apps/desktop/scripts/fixtures/process-safety/forbidden.ps1',
    'apps/desktop/README.md',
    'apps/desktop/assets/icon.ico',
    'apps/desktop/public/favicon.svg',
    'apps/desktop/scripts/perf/baseline.json',
    'scripts/install.ps1',
    'website/docs/desktop-verification.md'
  ]) {
    assert.equal(shouldScanRepositoryPath(relativePath), false, relativePath)
  }
})

test('allows native process APIs only in the exact checked-in C# Job host', () => {
  assert.deepEqual(scanText(SYNTHETIC_JOB_HOST_SOURCE, JOB_HOST_PATH), [])
  assert.notDeepEqual(
    scanText(SYNTHETIC_JOB_HOST_SOURCE, 'apps/desktop/scripts/copied/windows-verifier-job-host.cs'),
    []
  )
})

test('requires every native Job lifecycle API in the checked-in C# host', async t => {
  for (const api of REQUIRED_JOB_APIS) {
    await t.test(api, () => {
      const mutated = SYNTHETIC_JOB_HOST_SOURCE.replaceAll(api, `Missing${api}`)
      const violations = scanText(mutated, JOB_HOST_PATH)

      assert.ok(
        violations.some(violation => violation.reason.includes(api)),
        `${api}: ${formatViolations(violations)}`
      )
    })
  }
})

test('enforces suspended assign-before-resume launch and retained pre-assignment termination', () => {
  const mutations = [
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'CreateSuspended | CreateUnicodeEnvironment',
      'CreateUnicodeEnvironment'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'if (!AssignProcessToJobObject(job, process.hProcess))',
      'if (false)'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'TerminateProcess(process, unchecked((uint)ErrorExitCode));',
      'return;'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'WaitForSingleObject(process, 5000);',
      'return;'
    )
  ]

  for (const mutated of mutations) {
    assert.notDeepEqual(scanText(mutated, JOB_HOST_PATH), [])
  }
})

test('rejects caller PID authority, destructive OpenProcess, and breakaway flags in the Job host', () => {
  const mutations = [
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'public int TerminationTimeoutMs;',
      'public int TerminationTimeoutMs;\n            public int TerminationPid;'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'private static extern bool TerminateProcess',
      'private static extern bool OpenProcess'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'CreateSuspended | CreateUnicodeEnvironment',
      'CreateSuspended | CreateUnicodeEnvironment | 0x01000000'
    ),
    SYNTHETIC_JOB_HOST_SOURCE.replace(
      'JobObjectLimitKillOnJobClose = 0x00002000',
      'JobObjectLimitKillOnJobClose = 0x00002000 | 0x00000800'
    )
  ]

  for (const mutated of mutations) {
    assert.notDeepEqual(scanText(mutated, JOB_HOST_PATH), [])
  }
})

test('package scripts reject unsafe process control without flagging warning text', () => {
  const packageJson = JSON.stringify({
    scripts: {
      verify: 'taskkill /FI "IMAGENAME eq Hermes.exe" /F',
      warning: 'echo Never terminate Hermes by process name'
    }
  }, null, 2)

  assert.deepEqual(scanText(packageJson, 'apps/desktop/package.json'), [
    {
      file: 'apps/desktop/package.json',
      line: 3,
      reason: 'process termination via taskkill'
    }
  ])
})

test('root package lifecycle and delegated hooks cannot hide taskkill', () => {
  const packageJson = JSON.stringify({
    scripts: {
      pretest: 'taskkill.exe /IM Hermes.exe /T /F',
      verify: 'npm run delegated-cleanup',
      'delegated-cleanup': 'cmd /d /c "taskkill /FI \\"IMAGENAME eq Hermes.exe\\" /F"'
    }
  }, null, 2)
  const violations = scanText(packageJson, 'package.json')

  assert.equal(violations.length, 2, formatViolations(violations))
  assert.ok(violations.every(violation => /taskkill/i.test(violation.reason)))
})

test('composite action run steps are scanned as executable workflow behavior', () => {
  const action = [
    'name: unsafe desktop cleanup',
    'runs:',
    '  using: composite',
    '  steps:',
    '    - shell: pwsh',
    '      run: taskkill.exe /IM Hermes.exe /T /F'
  ].join('\n')

  assert.notDeepEqual(scanText(action, '.github/actions/desktop-check/action.yml'), [])
})

test('process-safety preflight must precede candidate-controlled workflow and aggregate commands', () => {
  const unsafeWorkflow = [
    'jobs:',
    '  desktop-windows-job-object:',
    '    runs-on: windows-latest',
    '    steps:',
    '      - run: npm --prefix apps/desktop run test:process-safety',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs'
  ].join('\n')
  const safeWorkflow = [
    'jobs:',
    '  desktop-windows-job-object:',
    '    runs-on: windows-latest',
    '    steps:',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs',
    '      - run: npm --prefix apps/desktop run test:process-safety'
  ].join('\n')
  const unsafePackage = JSON.stringify({
    scripts: {
      'check:process-safety': 'npm run test:process-safety && node scripts/check-verifier-process-safety.mjs'
    }
  })
  const safePackage = JSON.stringify({
    scripts: {
      'check:process-safety': 'node scripts/check-verifier-process-safety.mjs && npm run test:process-safety'
    }
  })

  for (const [source, path] of [
    [unsafeWorkflow, '.github/workflows/js-tests.yml'],
    [unsafePackage, 'apps/desktop/package.json']
  ]) {
    const violations = scanText(source, path)
    assert.ok(
      violations.some(violation => /preflight/i.test(violation.reason)),
      formatViolations(violations)
    )
  }
  assert.deepEqual(scanText(safeWorkflow, '.github/workflows/js-tests.yml'), [])
  assert.deepEqual(scanText(safePackage, 'apps/desktop/package.json'), [])
})

test('fails closed when a root package script delegates to an unsafe local Node helper', () => {
  const repoRoot = mkdtempSync(join(tmpdir(), 'hermes-process-safety-delegation-'))

  try {
    const helperPath = join(repoRoot, 'scripts', 'cleanup.mjs')
    mkdirSync(join(repoRoot, 'scripts'), { recursive: true })
    writeFileSync(
      join(repoRoot, 'package.json'),
      JSON.stringify({ scripts: { verify: 'node scripts/cleanup.mjs' } }),
      'utf8'
    )
    writeFileSync(
      helperPath,
      "import { spawnSync } from 'node:child_process'\nspawnSync('taskkill.exe', ['/IM', 'Hermes.exe', '/F'])\n",
      'utf8'
    )

    const result = scanRepository(repoRoot, {
      spawnSyncImpl: () => ({
        status: 0,
        stdout: 'package.json\0scripts/cleanup.mjs\0',
        stderr: ''
      })
    })

    assert.ok(
      result.violations.some(violation =>
        violation.file === 'scripts/cleanup.mjs' && /taskkill/i.test(violation.reason)
      ),
      formatViolations(result.violations)
    )
  } finally {
    rmSync(repoRoot, { recursive: true, force: true })
  }
})

test('fails closed when another workflow job uses a local action before scanner preflight', () => {
  const workflow = [
    'jobs:',
    '  desktop-windows-job-object:',
    '    runs-on: windows-latest',
    '    steps:',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs',
    '      - run: npm --prefix apps/desktop run test:process-safety',
    '  other-check:',
    '    runs-on: ubuntu-latest',
    '    steps:',
    '      - uses: actions/checkout@v4',
    '      - uses: ./.github/actions/candidate-check',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs'
  ].join('\n')
  const violations = scanText(workflow, '.github/workflows/js-tests.yml')

  assert.ok(
    violations.some(violation => violation.line === 7 && /preflight/i.test(violation.reason)),
    formatViolations(violations)
  )
})

test('fails closed when another workflow job runs npm before scanner preflight', () => {
  const workflow = [
    'jobs:',
    '  desktop-windows-job-object:',
    '    runs-on: windows-latest',
    '    steps:',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs',
    '      - run: npm --prefix apps/desktop run test:process-safety',
    '  other-check:',
    '    runs-on: ubuntu-latest',
    '    steps:',
    '      - uses: actions/checkout@v4',
    '      - run: npm ci',
    '      - run: node apps/desktop/scripts/check-verifier-process-safety.mjs'
  ].join('\n')
  const violations = scanText(workflow, '.github/workflows/js-tests.yml')

  assert.ok(
    violations.some(violation => violation.line === 7 && /preflight/i.test(violation.reason)),
    formatViolations(violations)
  )
})

test('keeps scoped legacy Desktop taskkill outside strict verifier policy', () => {
  const legacyDesktopSource = [
    "import { execFileSync } from 'node:child_process'",
    'function forceKillProcessTree(pid) {',
    "  execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'])",
    '}'
  ].join('\n')

  assert.deepEqual(
    scanText(legacyDesktopSource, 'apps/desktop/electron/main.ts'),
    []
  )
  assert.deepEqual(
    scanText('taskkill.exe /PID $ownedPid /T /F', 'scripts/desktop-verifier.ps1'),
    [{
      file: 'scripts/desktop-verifier.ps1',
      line: 1,
      reason: 'process termination via taskkill'
    }]
  )
})

test('formats deterministic relative file, line, and reason output', () => {
  const violations = scanText(
    '@echo off\ntaskkill /IM Hermes.exe /F\n',
    'scripts/desktop-verifier.cmd'
  )

  assert.equal(
    formatViolations(violations),
    'scripts/desktop-verifier.cmd:2: process termination via taskkill'
  )
})

test('repository scan uses tracked plus nonignored untracked Git enumeration', () => {
  const repoRoot = mkdtempSync(join(tmpdir(), 'hermes-process-safety-'))
  const calls = []

  try {
    const scriptsDir = join(repoRoot, 'apps', 'desktop', 'scripts')
    mkdirSync(scriptsDir, { recursive: true })
    writeFileSync(join(scriptsDir, 'verify.cmd'), '@echo off\ntaskkill /IM Hermes.exe /F\n', 'utf8')
    writeFileSync(
      join(scriptsDir, 'untracked-verifier.ps1'),
      'Stop-Process -Name Hermes -Force\n',
      'utf8'
    )
    writeFileSync(join(scriptsDir, 'ignored.py'), 'from subprocess import run\n', 'utf8')
    writeFileSync(
      join(scriptsDir, 'untracked-native.cs'),
      '[DllImport("kernel32.dll")] static extern bool OpenProcess();\n',
      'utf8'
    )

    const spawnSyncImpl = (command, args, options) => {
      calls.push({ command, args, options })

      return {
        status: 0,
        stdout: [
          'apps/desktop/scripts/verify.cmd',
          'apps/desktop/scripts/untracked-verifier.ps1',
          'apps/desktop/scripts/untracked-native.cs',
          ''
        ].join('\0'),
        stderr: ''
      }
    }

    assert.deepEqual(scanRepository(repoRoot, { spawnSyncImpl }), {
      filesScanned: 3,
      violations: [
        {
          file: 'apps/desktop/scripts/untracked-native.cs',
          line: 1,
          reason: 'Windows native process APIs are allowed only in the exact checked-in Job host'
        },
        {
          file: 'apps/desktop/scripts/untracked-verifier.ps1',
          line: 1,
          reason: 'verifier harness uses raw process API outside canonical owned infrastructure'
        },
        {
          file: 'apps/desktop/scripts/verify.cmd',
          line: 2,
          reason: 'process termination via taskkill'
        }
      ]
    })
    assert.equal(calls.length, 1)
    assert.equal(calls[0].command, 'git')
    assert.deepEqual(calls[0].args, [
      'ls-files',
      '-z',
      '--cached',
      '--others',
      '--exclude-standard'
    ])
    assert.equal(calls[0].options.cwd, repoRoot)
  } finally {
    rmSync(repoRoot, { recursive: true, force: true })
  }
})

test('source-reading policy rejects test code that opens or copies production source text', () => {
  const readCall = [
    "import { read",
    "FileSync } from 'node:fs'",
    "const source = read",
    "FileSync(new URL('./production",
    ".ts', import.meta.url), 'utf8')"
  ].join('')
  const copyCall = [
    "import { copy",
    "FileSync } from 'node:fs'",
    "copy",
    "FileSync(new URL('./controller",
    ".cs', import.meta.url), fixturePath)"
  ].join('')

  for (const source of [readCall, copyCall]) {
    const violations = scanText(
      source,
      'apps/desktop/scripts/synthetic-policy.node-test.mjs'
    )
    assert.ok(
      violations.some(violation => /reads production source text/i.test(violation.reason)),
      formatViolations(violations)
    )
  }
})

test('source-reading policy follows the source operand when a fixture destination tries to mask it', () => {
  const productionToFixture = [
    "import { copyFileSync } from 'node:fs'",
    "const productionPath = new URL('./desktop-verifier-lib.mjs', import.meta.url)",
    "const fixturePath = new URL('./fixtures/copied-controller.mjs', import.meta.url)",
    'copyFileSync(productionPath, fixturePath)'
  ].join('\n')
  const fixtureToFixture = [
    "import * as fs from 'node:fs'",
    "const sourceFixture = new URL('./fixtures/controller.mjs', import.meta.url)",
    "const destinationFixture = new URL('./fixtures/copied-controller.mjs', import.meta.url)",
    'fs.copyFileSync(sourceFixture, destinationFixture)'
  ].join('\n')

  const violations = scanText(
    productionToFixture,
    'apps/desktop/scripts/source-policy-adversary.node-test.mjs'
  )
  assert.ok(
    violations.some(violation => /reads production source text/i.test(violation.reason)),
    formatViolations(violations)
  )
  assert.deepEqual(
    scanText(fixtureToFixture, 'apps/desktop/scripts/source-policy-adversary.node-test.mjs'),
    []
  )
})
