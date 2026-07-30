param(
    [switch]$Prepare
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sourcePath = Join-Path $PSScriptRoot 'windows-verifier-job-host.cs'
$cacheSchema = 'v1'

function Get-Sha256([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-FileSha256([string]$path) {
    return Get-Sha256([System.IO.File]::ReadAllBytes($path))
}

function Get-JobHostCacheEntry {
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Windows verifier Job host source is missing: $sourcePath"
    }

    Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
    $sourceHash = Get-FileSha256 $sourcePath
    $referenceIdentity = [System.Web.Script.Serialization.JavaScriptSerializer].Assembly.FullName
    $runtimeIdentity = "$($PSVersionTable.PSEdition)|$($PSVersionTable.PSVersion)|$([Environment]::Version)|$([Environment]::Is64BitProcess)"
    $key = Get-Sha256([System.Text.Encoding]::UTF8.GetBytes("$cacheSchema`n$sourceHash`n$referenceIdentity`n$runtimeIdentity"))
    $root = Join-Path $env:LOCALAPPDATA 'Hermes\cache\desktop-verifier-job-host\v1'
    $directory = Join-Path $root $key

    return [pscustomobject]@{
        Key = $key
        Directory = $directory
        DllPath = Join-Path $directory 'HermesVerifierJobHost.dll'
        ManifestPath = Join-Path $directory 'manifest.json'
        SourceHash = $sourceHash
        ReferenceIdentity = $referenceIdentity
        RuntimeIdentity = $runtimeIdentity
    }
}

function Get-ValidatedJobHostAssembly($entry) {
    if (-not ((Test-Path -LiteralPath $entry.DllPath -PathType Leaf) -and
              (Test-Path -LiteralPath $entry.ManifestPath -PathType Leaf))) {
        return $null
    }

    try {
        $manifest = Get-Content -LiteralPath $entry.ManifestPath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ($manifest.schema -ne $cacheSchema -or $manifest.key -ne $entry.Key -or
            $manifest.sourceSha256 -ne $entry.SourceHash -or
            $manifest.referenceIdentity -ne $entry.ReferenceIdentity -or
            $manifest.runtimeIdentity -ne $entry.RuntimeIdentity -or
            $manifest.dllSha256 -ne (Get-FileSha256 $entry.DllPath)) {
            return $null
        }
        $assembly = [System.Reflection.Assembly]::LoadFrom($entry.DllPath)
        $controller = $assembly.GetType('HermesVerifierJobHost.Controller', $false)
        if ($null -eq $controller -or $null -eq $controller.GetMethod('Run')) {
            return $null
        }
        return $assembly
    }
    catch {
        return $null
    }
}

function Get-JobHostAssembly {
    $entry = Get-JobHostCacheEntry
    $assembly = Get-ValidatedJobHostAssembly $entry
    if ($null -ne $assembly) {
        return $assembly
    }

    [System.IO.Directory]::CreateDirectory($entry.Directory) | Out-Null
    $mutex = [System.Threading.Mutex]::new($false, "Local\HermesVerifierJobHost_$($entry.Key)")
    $ownsMutex = $false
    try {
        try {
            $ownsMutex = $mutex.WaitOne(30000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        if (-not $ownsMutex) {
            throw 'timed out waiting for the verifier Job host cache lock'
        }

        $assembly = Get-ValidatedJobHostAssembly $entry
        if ($null -ne $assembly) {
            return $assembly
        }

        $temporaryDll = Join-Path $entry.Directory ("HermesVerifierJobHost.$PID.$([Guid]::NewGuid().ToString('N')).tmp.dll")
        try {
            Add-Type -Path $sourcePath -ReferencedAssemblies 'System.Web.Extensions.dll' -OutputAssembly $temporaryDll -ErrorAction Stop
            Move-Item -LiteralPath $temporaryDll -Destination $entry.DllPath -Force
            $assembly = [System.Reflection.Assembly]::LoadFrom($entry.DllPath)
            $controller = $assembly.GetType('HermesVerifierJobHost.Controller', $false)
            if ($null -eq $controller -or $null -eq $controller.GetMethod('Run')) {
                throw 'compiled verifier Job host is missing Controller.Run'
            }
            $manifest = [ordered]@{
                schema = $cacheSchema
                key = $entry.Key
                sourceSha256 = $entry.SourceHash
                referenceIdentity = $entry.ReferenceIdentity
                runtimeIdentity = $entry.RuntimeIdentity
                dllSha256 = Get-FileSha256 $entry.DllPath
            }
            $temporaryManifest = Join-Path $entry.Directory ("manifest.$PID.$([Guid]::NewGuid().ToString('N')).tmp.json")
            [System.IO.File]::WriteAllText($temporaryManifest, ($manifest | ConvertTo-Json -Compress), [System.Text.Encoding]::UTF8)
            Move-Item -LiteralPath $temporaryManifest -Destination $entry.ManifestPath -Force
            return $assembly
        }
        finally {
            if (Test-Path -LiteralPath $temporaryDll -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryDll -Force -ErrorAction SilentlyContinue
            }
        }
    }
    finally {
        if ($ownsMutex) {
            $mutex.ReleaseMutex()
        }
        $mutex.Dispose()
    }
}

try {
    $assembly = Get-JobHostAssembly
    if ($Prepare) {
        exit 0
    }

    $controller = $assembly.GetType('HermesVerifierJobHost.Controller', $true)
    $exitCode = $controller.GetMethod('Run').Invoke($null, @())
    exit [int]$exitCode
}
catch {
    [Console]::Error.WriteLine("Windows verifier Job host bootstrap failed: $($_.Exception.Message)")
    exit 1
}
