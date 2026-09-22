#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PythonExecutable,
    [Parameter(Mandatory=$true)][string]$CliLauncherSource,
    [Parameter(Mandatory=$true)][string]$InstalledCliLauncher,
    [Parameter(Mandatory=$true)][string]$Destination
)

$ErrorActionPreference = 'Stop'
$python = [IO.Path]::GetFullPath($PythonExecutable)
$launcherSource = [IO.Path]::GetFullPath($CliLauncherSource)
$launcher = [IO.Path]::GetFullPath($InstalledCliLauncher)
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (-not [IO.Path]::IsPathRooted($PythonExecutable) -or
    -not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Python executable must be an existing absolute file: $PythonExecutable"
}
if (-not [IO.Path]::IsPathRooted($CliLauncherSource) -or
    -not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) {
    throw "CLI launcher source must be an existing absolute file: $CliLauncherSource"
}
if (-not [IO.Path]::IsPathRooted($InstalledCliLauncher)) {
    throw "Installed CLI launcher must be an absolute path: $InstalledCliLauncher"
}
if ($python.Contains('"') -or $launcher.Contains('"')) {
    throw 'Launcher paths cannot contain a double quote.'
}
$parent = Split-Path -Parent $destinationPath
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Destination parent does not exist: $parent"
}

$body = @(
    '@echo off',
    ('"{0}" "{1}" %*' -f $python, $launcher),
    'exit /b %ERRORLEVEL%',
    ''
) -join "`r`n"
[IO.File]::WriteAllText($destinationPath, $body, [Text.Encoding]::ASCII)
