#requires -Version 5.1
param(
    [switch]$Cli,
    [switch]$RuntimeProbe,
    [switch]$CaptureSelfTest,
    [ValidateRange(1,600)][int]$TimeoutSeconds = 120,
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs
)

$ErrorActionPreference = 'Stop'
$modes = @($Cli, $RuntimeProbe, $CaptureSelfTest) | Where-Object { $_ }
if ($modes.Count -gt 1) { throw 'Choose one launcher mode.' }

function ConvertTo-WindowsArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes) { [void]$builder.Append(('\' * $slashes)); $slashes = 0 }
        [void]$builder.Append($character)
    }
    if ($slashes) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Set-ProcessArguments {
    param([Diagnostics.ProcessStartInfo]$StartInfo, [string[]]$Values)
    $StartInfo.Arguments = (($Values | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' ')
}

function Invoke-CapturedProcess {
    param([Diagnostics.ProcessStartInfo]$StartInfo, [int]$Timeout)
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $StartInfo
    if (-not $process.Start()) { throw 'Child process did not start.' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($Timeout * 1000)
    if ($timedOut) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
        [void]$process.WaitForExit(5000)
    }
    [pscustomobject]@{
        ExitCode = if ($timedOut) { 124 } else { $process.ExitCode }
        StdOut = $stdoutTask.GetAwaiter().GetResult()
        StdErr = $stderrTask.GetAwaiter().GetResult()
        TimedOut = $timedOut
    }
}

if ($CaptureSelfTest) {
    $fixture = New-Object Diagnostics.ProcessStartInfo
    $fixture.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    Set-ProcessArguments $fixture @('-NoLogo','-NoProfile','-NonInteractive','-Command',
        '[Console]::Out.Write("fixture-out");[Console]::Error.Write(("e"*262144));exit 7')
    $result = Invoke-CapturedProcess $fixture 10
    if ($result.ExitCode -ne 7 -or $result.StdOut -ne 'fixture-out' -or $result.StdErr.Length -ne 262144) {
        throw 'Concurrent output drain or exit propagation failed.'
    }
    [pscustomobject]@{ Status='PASS'; Engine='WindowsPowerShell5.1'; ConcurrentDrain=$true; ExitCode=$result.ExitCode } |
        ConvertTo-Json -Compress
    exit 0
}

$canonicalRoot = 'C:\SakaanAIAgentWorkspace\Hermes\main'
$canonicalHome = 'C:\Users\HieuKa\.hermes'
$python = Join-Path $canonicalRoot '.venv\Scripts\python.exe'
$desktop = Join-Path $canonicalRoot 'apps\desktop\release\win-unpacked\Hermes.exe'
$hold = 'C:\Users\HieuKa\.codex-agent-relay\tasks\hermes-native-runtime-release-20260909\CUTOVER.lock'
if (Test-Path -LiteralPath $hold) { throw 'Hermes release transition is held; nothing was started.' }
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Canonical Hermes Python is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $canonicalHome 'config.yaml') -PathType Leaf)) {
    throw 'Canonical Hermes configuration is missing.'
}

$start = New-Object Diagnostics.ProcessStartInfo
$start.UseShellExecute = $false
$start.WorkingDirectory = $canonicalRoot
foreach ($name in @($start.EnvironmentVariables.Keys)) {
    if ($name -match '^(HERMES_|TERMINAL_|PYTHONPATH$|PYTHONHOME$|VIRTUAL_ENV$|CONDA_PREFIX$|ELECTRON_RUN_AS_NODE$|NODE_OPTIONS$|AWS_|GOOGLE_APPLICATION_CREDENTIALS$)' -or
        $name -match '(_API_KEY|_BASE_URL|_TOKEN|_SECRET|_PASSWORD|_CREDENTIALS|_ACCESS_KEY|_PRIVATE_KEY)$') {
        $start.EnvironmentVariables.Remove($name)
    }
}
$start.EnvironmentVariables['HERMES_HOME'] = $canonicalHome
$start.EnvironmentVariables['PYTHONPATH'] = $canonicalRoot
$start.EnvironmentVariables['PYTHONNOUSERSITE'] = '1'
$start.EnvironmentVariables['HERMES_DESKTOP_HERMES_ROOT'] = $canonicalRoot
$start.EnvironmentVariables['HERMES_DESKTOP_PYTHON'] = $python
$start.EnvironmentVariables['HERMES_DESKTOP_USER_DATA_DIR'] = 'C:\Users\HieuKa\AppData\Roaming\Hermes'
$start.EnvironmentVariables['HERMES_DESKTOP_PROTOCOL_MANAGED'] = '1'

if ($RuntimeProbe) {
    $start.FileName = $python
    Set-ProcessArguments $start @('-m','hermes_cli.subscription_probe','--provider','openai-codex')
    $result = Invoke-CapturedProcess $start $TimeoutSeconds
    [Console]::Out.Write($result.StdOut)
    [Console]::Error.Write($result.StdErr)
    exit $result.ExitCode
}

if ($Cli) {
    $start.FileName = $python
    Set-ProcessArguments $start (@('-m','hermes_cli.main') + @($CliArgs))
    $result = Invoke-CapturedProcess $start $TimeoutSeconds
    [Console]::Out.Write($result.StdOut)
    [Console]::Error.Write($result.StdErr)
    exit $result.ExitCode
}

if (-not (Test-Path -LiteralPath $desktop -PathType Leaf)) { throw 'Canonical desktop package is missing.' }
$start.FileName = $desktop
$start.CreateNoWindow = $true
$process = [Diagnostics.Process]::Start($start)
Write-Output ("Hermes desktop launched, PID {0}; live acceptance is not implied." -f $process.Id)
