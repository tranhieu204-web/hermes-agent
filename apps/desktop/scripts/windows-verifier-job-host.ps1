param(
    [switch]$Prepare
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sourcePath = Join-Path $PSScriptRoot 'windows-verifier-job-host.cs'
$cacheSchema = 'v1'
$mutexAcquireTimeoutMs = 20000
$diagnosticsEnabled = $env:HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS -eq '1'

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

function Get-NormalizedCacheRoot([string]$path) {
    $resolved = [System.IO.Path]::GetFullPath($path)
    $pathRoot = [System.IO.Path]::GetPathRoot($resolved)
    $trimmed = $resolved.TrimEnd([char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ))

    if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.Length -lt $pathRoot.Length) {
        $trimmed = $pathRoot
    }

    return $trimmed.ToUpperInvariant()
}

function Write-JobHostDiagnostic([string]$phase, [long]$elapsedMs) {
    if ($diagnosticsEnabled) {
        # Diagnostics deliberately contain only a fixed phase label and duration.
        # Cache roots, source bytes, environment values, and command lines remain private.
        [Console]::Error.WriteLine("HermesVerifierJobHost diagnostic phase=$phase elapsed_ms=$elapsedMs")
    }
}

function Write-JobHostMutexIdentityDiagnostic([string]$mutexKey) {
    if ($diagnosticsEnabled) {
        # This pseudonymous fixed-length digest correlates lock behavior
        # without disclosing the cache root or any environment value.
        [Console]::Error.WriteLine("HermesVerifierJobHost diagnostic mutex_identity_sha256=$mutexKey")
    }
}

function Invoke-JobHostPhase([string]$phase, [scriptblock]$operation) {
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        return & $operation
    }
    finally {
        $stopwatch.Stop()
        Write-JobHostDiagnostic $phase $stopwatch.ElapsedMilliseconds
    }
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
    $normalizedRoot = Get-NormalizedCacheRoot $root
    $mutexKey = Get-Sha256([System.Text.Encoding]::UTF8.GetBytes("$normalizedRoot`n$key"))
    $directory = Join-Path $root $key

    return [pscustomobject]@{
        Key = $key
        Directory = $directory
        DllPath = Join-Path $directory 'HermesVerifierJobHost.dll'
        ManifestPath = Join-Path $directory 'manifest.json'
        LockPath = Join-Path $directory 'publication.lock'
        SourceHash = $sourceHash
        ReferenceIdentity = $referenceIdentity
        RuntimeIdentity = $runtimeIdentity
        MutexKey = $mutexKey
        MutexName = "Local\HermesVerifierJobHost_$mutexKey"
    }
}

function Open-JobHostPublicationLock($entry, $budgetStopwatch) {
    while ($true) {
        try {
            # File sharing follows the physical directory, including a junction
            # alias that cannot be represented reliably by lexical path cleanup.
            return [System.IO.FileStream]::new(
                $entry.LockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        }
        catch [System.IO.IOException] {
            $remainingMs = $mutexAcquireTimeoutMs - [int]$budgetStopwatch.ElapsedMilliseconds
            if ($remainingMs -le 0) {
                throw "verifier Job host cache lock timed out after $mutexAcquireTimeoutMs ms"
            }
            Start-Sleep -Milliseconds ([Math]::Min(50, $remainingMs))
        }
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
    Write-JobHostMutexIdentityDiagnostic $entry.MutexKey
    $assembly = Invoke-JobHostPhase 'validation' { Get-ValidatedJobHostAssembly $entry }
    if ($null -ne $assembly) {
        return $assembly
    }

    [System.IO.Directory]::CreateDirectory($entry.Directory) | Out-Null
    $mutex = [System.Threading.Mutex]::new($false, $entry.MutexName)
    $ownsMutex = $false
    $publicationLock = $null
    try {
        $lockStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        try {
            $ownsMutex = $mutex.WaitOne($mutexAcquireTimeoutMs)
        }
        catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        try {
            if (-not $ownsMutex) {
                throw "verifier Job host cache lock timed out after $mutexAcquireTimeoutMs ms"
            }
            $publicationLock = Open-JobHostPublicationLock $entry $lockStopwatch
        }
        finally {
            $lockStopwatch.Stop()
            Write-JobHostDiagnostic 'lock_wait' $lockStopwatch.ElapsedMilliseconds
        }

        $assembly = Invoke-JobHostPhase 'validation' { Get-ValidatedJobHostAssembly $entry }
        if ($null -ne $assembly) {
            return $assembly
        }

        $temporaryDll = Join-Path $entry.Directory ("HermesVerifierJobHost.$PID.$([Guid]::NewGuid().ToString('N')).tmp.dll")
        try {
            Invoke-JobHostPhase 'compile' {
                Add-Type -Path $sourcePath -ReferencedAssemblies 'System.Web.Extensions.dll' -OutputAssembly $temporaryDll -ErrorAction Stop
            } | Out-Null
            Invoke-JobHostPhase 'publish' {
                Move-Item -LiteralPath $temporaryDll -Destination $entry.DllPath -Force
            } | Out-Null
            $assembly = Invoke-JobHostPhase 'validation' { [System.Reflection.Assembly]::LoadFrom($entry.DllPath) }
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
            Invoke-JobHostPhase 'publish' {
                [System.IO.File]::WriteAllText($temporaryManifest, ($manifest | ConvertTo-Json -Compress), [System.Text.Encoding]::UTF8)
                Move-Item -LiteralPath $temporaryManifest -Destination $entry.ManifestPath -Force
            } | Out-Null
            return $assembly
        }
        finally {
            if (Test-Path -LiteralPath $temporaryDll -PathType Leaf) {
                Remove-Item -LiteralPath $temporaryDll -Force -ErrorAction SilentlyContinue
            }
        }
    }
    finally {
        if ($null -ne $publicationLock) {
            $publicationLock.Dispose()
        }
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
