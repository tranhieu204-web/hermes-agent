import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import {
  formatViolations,
  scanRepository,
  scanText,
  shouldScanRepositoryPath
} from './check-verifier-process-safety.mjs'

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
  }
]

const ALLOW_CASES = [
  {
    name: 'canonical owned Desktop launch',
    path: 'apps/desktop/scripts/desktop-verifier-lib.mjs',
    source: [
      "import { spawn as nodeSpawn } from 'node:child_process'",
      'export async function launchOwnedDesktop(spec) {',
      '  const child = nodeSpawn(spec.executable, spec.args, spec.spawnOptions)',
      '  return child',
      '}'
    ].join('\n')
  },
  {
    name: 'canonical exact Windows owned-handle cleanup',
    path: 'apps/desktop/scripts/desktop-verifier-lib.mjs',
    source: [
      'async function terminateOwnedChild({ child }) {',
      '  return child.kill()',
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
    name: 'PowerShell independent PID-only lookup and cleanup',
    path: 'scripts/desktop-verifier.ps1',
    source: [
      'Get-Process -Id $candidatePid',
      "$note = 'owned child only'",
      'Stop-Process -Id $ownedPid -Force'
    ].join('\n')
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
    name: 'PowerShell PID-only aliases',
    path: 'scripts/desktop-verifier.ps1',
    source: 'gps -Id $candidatePid\nspps -Id $ownedPid -Force'
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
    'apps/desktop/scripts/perf/lib/launch.mjs',
    'apps/desktop/scripts/verify-desktop.ps1',
    'apps/desktop/package.json',
    'apps/desktop/tsconfig.json',
    'scripts/desktop-verifier.ps1',
    'scripts/verify-desktop.py',
    'scripts/dev-desktop.mjs',
    '.github/workflows/js-tests.yml',
    '.github/workflows/e2e-desktop.yaml',
    '.github/workflows/verify.yml',
    '.github/workflows/python-tests.yml'
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
    '.github/actions/verify/action.yml',
    'website/docs/desktop-verification.md'
  ]) {
    assert.equal(shouldScanRepositoryPath(relativePath), false, relativePath)
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
      reason: 'process-name termination via taskkill image selector'
    }
  ])
})

test('formats deterministic relative file, line, and reason output', () => {
  const violations = scanText(
    '@echo off\ntaskkill /IM Hermes.exe /F\n',
    'scripts/desktop-verifier.cmd'
  )

  assert.equal(
    formatViolations(violations),
    'scripts/desktop-verifier.cmd:2: process-name termination via taskkill image selector'
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

    const spawnSyncImpl = (command, args, options) => {
      calls.push({ command, args, options })

      return {
        status: 0,
        stdout: [
          'apps/desktop/scripts/verify.cmd',
          'apps/desktop/scripts/untracked-verifier.ps1',
          ''
        ].join('\0'),
        stderr: ''
      }
    }

    assert.deepEqual(scanRepository(repoRoot, { spawnSyncImpl }), {
      filesScanned: 2,
      violations: [
        {
          file: 'apps/desktop/scripts/untracked-verifier.ps1',
          line: 1,
          reason: 'verifier harness uses raw process API outside canonical owned infrastructure'
        },
        {
          file: 'apps/desktop/scripts/verify.cmd',
          line: 2,
          reason: 'process-name termination via taskkill image selector'
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

test('scanner and canonical verifier sources safely scan themselves', () => {
  const paths = [
    'check-verifier-process-safety.mjs',
    'check-verifier-process-safety.node-test.mjs',
    'desktop-verifier-lib.mjs',
    'desktop-verifier-lib.node-test.mjs'
  ]

  for (const name of paths) {
    assert.deepEqual(
      scanText(
        readFileSync(new URL(`./${name}`, import.meta.url), 'utf8'),
        `apps/desktop/scripts/${name}`
      ),
      [],
      name
    )
  }
})
