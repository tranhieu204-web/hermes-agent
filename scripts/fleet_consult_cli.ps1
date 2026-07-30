[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Hermes", "Claude")]
    [string]$Agent,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Prompt,

    [switch]$RequireVerdict,

    [ValidateNotNullOrEmpty()]
    [string]$HermesProvider = "openai-codex",

    [ValidateNotNullOrEmpty()]
    [string]$HermesModel = "gpt-5.6-sol",

    [ValidateRange(0.01, 1000.0)]
    [decimal]$ClaudeMaxBudgetUsd = 1.00
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Invoke-CheckedCli {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $command = Get-Command $Executable -ErrorAction Stop
    $output = & $command.Source @Arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        throw "$Executable exited with code $exitCode.`n$output"
    }

    return $output.Trim()
}

switch ($Agent) {
    "Hermes" {
        # Never inherit the machine's default Anthropic route for consultations.
        # Claude Code OAuth used through Hermes is a third-party-app route and
        # can be rejected by Anthropic's extra-usage policy even while the
        # first-party Claude CLI remains healthy.
        $response = Invoke-CheckedCli -Executable "hermes" -Arguments @(
            "--ignore-rules",
            "--provider", $HermesProvider,
            "--model", $HermesModel,
            "--oneshot", $Prompt
        )
    }
    "Claude" {
        # Safe mode keeps first-party Claude authentication but disables global
        # CLAUDE.md, hooks, plugins, skills, and MCP instructions that can
        # contaminate a bounded design consultation.
        $systemPrompt = @"
You are a bounded read-only design reviewer.
Follow only the explicit user prompt.
Do not inspect files, use tools, write plans, or modify state.
Return the requested verdict and concrete conditions without discussing
unrelated governance or instructions from outside the explicit prompt.
"@
        $response = Invoke-CheckedCli -Executable "claude" -Arguments @(
            "-p",
            "--safe-mode",
            "--permission-mode", "dontAsk",
            "--tools=",
            "--no-session-persistence",
            "--max-budget-usd",
            $ClaudeMaxBudgetUsd.ToString(
                [System.Globalization.CultureInfo]::InvariantCulture
            ),
            "--system-prompt", $systemPrompt,
            $Prompt
        )
    }
}

if (
    $RequireVerdict -and
    $response -notmatch "(?im)\b(APPROVE(?:\s+WITH\s+CONDITIONS)?|CONDITIONAL|HOLD)\b"
) {
    throw "$Agent returned exit code 0 without an explicit verdict.`n$response"
}

$response
