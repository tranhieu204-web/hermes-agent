Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sourcePath = Join-Path $PSScriptRoot 'windows-verifier-job-host.cs'

try {
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Windows verifier Job host source is missing: $sourcePath"
    }

    Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
    Add-Type -Path $sourcePath -ReferencedAssemblies 'System.Web.Extensions.dll' -ErrorAction Stop
    $exitCode = [HermesVerifierJobHost.Controller]::Run()
    exit $exitCode
}
catch {
    [Console]::Error.WriteLine("Windows verifier Job host bootstrap failed: $($_.Exception.Message)")
    exit 1
}
