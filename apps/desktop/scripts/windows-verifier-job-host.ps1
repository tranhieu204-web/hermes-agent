param(
    [switch]$Prepare
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$sourcePath = Join-Path $PSScriptRoot 'windows-verifier-job-host.cs'
$cacheSchema = 'v1'
$mutexAcquireTimeoutMs = 20000
$diagnosticsEnabled = $env:HERMES_VERIFIER_JOB_HOST_DIAGNOSTICS -eq '1'
$script:jobHostDiagnosticSequence = 0
$script:jobHostDiagnosticSourceHash = $null

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
        [Console]::Error.Flush()
    }
}

function Write-JobHostDiagnosticEvent([string]$eventName, [string]$phase) {
    if ($diagnosticsEnabled -and -not [string]::IsNullOrWhiteSpace($script:jobHostDiagnosticSourceHash)) {
        # Every event is bound to the static source hash and uses a fixed label.
        # It never emits a cache root, command line, PID, environment value, or exception text.
        $script:jobHostDiagnosticSequence += 1
        [Console]::Error.WriteLine(
            "HermesVerifierJobHost diagnostic event=$eventName phase=$phase source_sha256=$($script:jobHostDiagnosticSourceHash) sequence=$($script:jobHostDiagnosticSequence)"
        )
        [Console]::Error.Flush()
    }
}

function Write-JobHostLockOpenDiagnostic([string]$eventName, [int]$attemptSequence, [string]$outcome) {
    if ($diagnosticsEnabled -and -not [string]::IsNullOrWhiteSpace($script:jobHostDiagnosticSourceHash)) {
        # The native FileStream constructor can block without returning. Emit a
        # fixed, source-bound enter marker immediately before it, then emit an
        # outcome only after the constructor has actually returned or thrown.
        # This deliberately leaves enter-without-outcome as fail-closed evidence
        # for a mid-open termination. No path, exception text, handle, PID, or
        # environment value is included.
        $script:jobHostDiagnosticSequence += 1
        if ($eventName -eq 'lock_open_enter') {
            [Console]::Error.WriteLine(
                "HermesVerifierJobHost diagnostic event=lock_open_enter attempt_seq=$attemptSequence source_sha256=$($script:jobHostDiagnosticSourceHash) sequence=$($script:jobHostDiagnosticSequence)"
            )
        }
        elseif ($eventName -eq 'lock_retry_budget') {
            [Console]::Error.WriteLine(
                "HermesVerifierJobHost diagnostic event=lock_retry_budget attempt_seq=$attemptSequence state=$outcome source_sha256=$($script:jobHostDiagnosticSourceHash) sequence=$($script:jobHostDiagnosticSequence)"
            )
        }
        else {
            [Console]::Error.WriteLine(
                "HermesVerifierJobHost diagnostic event=lock_open_outcome attempt_seq=$attemptSequence outcome=$outcome source_sha256=$($script:jobHostDiagnosticSourceHash) sequence=$($script:jobHostDiagnosticSequence)"
            )
        }
        [Console]::Error.Flush()
    }
}

function Write-JobHostMutexIdentityDiagnostic([string]$mutexKey) {
    if ($diagnosticsEnabled) {
        # This pseudonymous fixed-length digest correlates lock behavior
        # without disclosing the cache root or any environment value.
        [Console]::Error.WriteLine("HermesVerifierJobHost diagnostic mutex_identity_sha256=$mutexKey")
        [Console]::Error.Flush()
    }
}

function Write-JobHostPrecompileFailureDiagnostic([string]$sourceHash, [string]$classification) {
    if ($diagnosticsEnabled) {
        # Keep the root cause usable without revealing a filesystem path or
        # forwarding the original exception text.
        [Console]::Error.WriteLine(
            "HermesVerifierJobHost diagnostic precompile_failure source_sha256=$sourceHash class=$classification"
        )
        [Console]::Error.Flush()
    }
}

function Write-JobHostCompileOutcomeDiagnostic([string]$sourceHash, [string]$outcome) {
    if ($diagnosticsEnabled) {
        # This is a fixed-vocabulary terminal outcome for the exact Add-Type
        # boundary. It deliberately carries no exception, path, process, or
        # environment detail, and does not alter the original failure flow.
        [Console]::Error.WriteLine(
            "HermesVerifierJobHost diagnostic compile_outcome outcome=$outcome source_sha256=$sourceHash"
        )
        [Console]::Error.Flush()
    }
}

function Invoke-JobHostPhase([string]$phase, [scriptblock]$operation) {
    Write-JobHostDiagnosticEvent 'phase_begin' $phase
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        return & $operation
    }
    finally {
        $stopwatch.Stop()
        Write-JobHostDiagnostic $phase $stopwatch.ElapsedMilliseconds
        Write-JobHostDiagnosticEvent 'phase_end' $phase
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
    $attemptSequence = 0
    while ($true) {
        $attemptSequence += 1
        Write-JobHostLockOpenDiagnostic 'lock_open_enter' $attemptSequence $null
        try {
            # File sharing follows the physical directory, including a junction
            # alias that cannot be represented reliably by lexical path cleanup.
            $publicationLock = [System.IO.FileStream]::new(
                $entry.LockPath,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
            $outcome = if ($attemptSequence -eq 1) { 'acquired' } else { 'acquired_after_retry' }
            Write-JobHostLockOpenDiagnostic 'lock_open_outcome' $attemptSequence $outcome
            return $publicationLock
        }
        catch [System.IO.IOException] {
            Write-JobHostLockOpenDiagnostic 'lock_open_outcome' $attemptSequence 'io_exception_retry'
            $remainingMs = $mutexAcquireTimeoutMs - [int]$budgetStopwatch.ElapsedMilliseconds
            # A fixed state records only the existing control-flow branch after
            # FileStream has already returned. It deliberately omits elapsed time,
            # paths, handles, exceptions, and process identity.
            $budgetState = if ($remainingMs -le 0) { 'exhausted' } else { 'remaining' }
            Write-JobHostLockOpenDiagnostic 'lock_retry_budget' $attemptSequence $budgetState
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
    if ($diagnosticsEnabled -and (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        # A source hash is an immutable correlation token, not a source disclosure.
        $script:jobHostDiagnosticSourceHash = Get-FileSha256 $sourcePath
    }
    $entry = Invoke-JobHostPhase 'cache_entry' { Get-JobHostCacheEntry }
    Write-JobHostMutexIdentityDiagnostic $entry.MutexKey
    $assembly = Invoke-JobHostPhase 'validation' { Get-ValidatedJobHostAssembly $entry }
    if ($null -ne $assembly) {
        return $assembly
    }

    try {
        Invoke-JobHostPhase 'cache_directory' { [System.IO.Directory]::CreateDirectory($entry.Directory) | Out-Null }
    }
    catch [System.IO.PathTooLongException] {
        Write-JobHostPrecompileFailureDiagnostic $entry.SourceHash 'cache_root_unavailable'
        throw
    }
    catch [System.IO.DirectoryNotFoundException] {
        Write-JobHostPrecompileFailureDiagnostic $entry.SourceHash 'cache_root_unavailable'
        throw
    }
    catch [System.NotSupportedException] {
        Write-JobHostPrecompileFailureDiagnostic $entry.SourceHash 'cache_root_unavailable'
        throw
    }
    $mutex = [System.Threading.Mutex]::new($false, $entry.MutexName)
    $ownsMutex = $false
    $publicationLock = $null
    try {
        $lockStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        # This brackets the existing mutex/publication-lock acquisition only
        # when diagnostic mode is explicitly enabled. If the owned preparer
        # is terminated while waiting, the missing end event is fail-closed
        # evidence of that exact boundary without changing lock semantics.
        Write-JobHostDiagnosticEvent 'phase_begin' 'lock_wait'
        Write-JobHostDiagnosticEvent 'phase_begin' 'mutex_wait'
        try {
            $ownsMutex = $mutex.WaitOne($mutexAcquireTimeoutMs)
        }
        catch [System.Threading.AbandonedMutexException] {
            $ownsMutex = $true
        }
        finally {
            # This is strictly observational. It closes after the existing
            # WaitOne/abandoned-mutex handling and before publication-lock
            # acquisition, so it distinguishes the two blocking boundaries.
            Write-JobHostDiagnosticEvent 'phase_end' 'mutex_wait'
        }
        try {
            if (-not $ownsMutex) {
                throw "verifier Job host cache lock timed out after $mutexAcquireTimeoutMs ms"
            }
            Write-JobHostDiagnosticEvent 'phase_begin' 'publication_lock_wait'
            try {
                $publicationLock = Open-JobHostPublicationLock $entry $lockStopwatch
            }
            finally {
                # Preserve acquisition semantics; this records only that the
                # existing publication-lock operation returned or unwound.
                Write-JobHostDiagnosticEvent 'phase_end' 'publication_lock_wait'
            }
        }
        finally {
            $lockStopwatch.Stop()
            Write-JobHostDiagnostic 'lock_wait' $lockStopwatch.ElapsedMilliseconds
            # Keep the aggregate marker for compatibility. It brackets both
            # sub-boundaries and remains non-authoritative for causality.
            Write-JobHostDiagnosticEvent 'phase_end' 'lock_wait'
        }

        $assembly = Invoke-JobHostPhase 'validation' { Get-ValidatedJobHostAssembly $entry }
        if ($null -ne $assembly) {
            return $assembly
        }

        $temporaryDll = Join-Path $entry.Directory ("HermesVerifierJobHost.$PID.$([Guid]::NewGuid().ToString('N')).tmp.dll")
        try {
            if ($diagnosticsEnabled) {
                # Bind the diagnostic to the exact static source without exposing
                # its path or contents. A missing end marker is fail-closed evidence
                # that the real Add-Type boundary did not return.
                [Console]::Error.WriteLine("HermesVerifierJobHost diagnostic compile_start source_sha256=$($entry.SourceHash)")
                [Console]::Error.Flush()
            }
            Invoke-JobHostPhase 'compile' {
                try {
                    Add-Type -Path $sourcePath -ReferencedAssemblies 'System.Web.Extensions.dll' -OutputAssembly $temporaryDll -ErrorAction Stop
                }
                catch {
                    # This catch is deliberately inside the exact Add-Type boundary:
                    # phase-finalization and later diagnostics failures must never be
                    # reported as compiler exceptions. Diagnostic emission is best
                    # effort so it cannot replace the original compiler failure.
                    $compileError = $_
                    try {
                        Write-JobHostCompileOutcomeDiagnostic $entry.SourceHash 'exception'
                    }
                    catch {
                        # Preserve the original Add-Type error and exit behavior.
                    }
                    throw $compileError
                }
            } | Out-Null
            if ($diagnosticsEnabled) {
                [Console]::Error.WriteLine(
                    "HermesVerifierJobHost diagnostic compile_end source_sha256=$($entry.SourceHash) output_sha256=$(Get-FileSha256 $temporaryDll)"
                )
                [Console]::Error.Flush()
            }
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
