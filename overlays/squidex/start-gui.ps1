#requires -Version 5.1
<#
    start-gui.ps1  -  RQ9 blind-curation curator launcher
    (companion plan 2026-07-13 sec 5.6; runbook docs/rq9-blind-curation-runbook.md sec 6/8).

    WHAT THIS IS
      The single, enforced entry point for a blind curator's GUI session. It does two
      things every sitting must have:
        1. sets ANON_EDIT_TRACE so every edit you apply is timestamped into
           edit-trace-<Curator>.jsonl - this IS the RQ9 effort measurement;
        2. launches `arch serve` over YOUR OWN rules dir (a fresh default copy, never
           the committed overlays/), so your curation is isolated and reproducible.
      ==> ALWAYS start the GUI through this script. Closing and reopening between
          sittings is fine - the trace keeps accumulating (append mode).

    BLINDING (runbook sec 4)
      As the curator the tool is your instrument: the repo, the public project docs,
      the `arch propose` draft, and the GUI are all fair game. You must NOT look at any
      reference.rsf, any rater worksheet, the eval repo's references/ or overlays/
      dirs, or ANY fidelity score. Your stopping rule is your own review-complete
      judgment (runbook sec 6 step 4).

    FACILITATOR - before handing the packet over (runbook sec 8 Phase 0 step 3):
      Under <Root> (this script's dir when copied into the packet) prepare, per curator:
        out/<System>/blind-curation/ws-<Curator>/architecture   <- a prepared workspace:
            copy the onboarded out/<System>/architecture here, then it already carries
            `arch run --no-llm` + `arch propose` output.
        out/<System>/blind-curation/curator-rules-<Curator>/     <- a FRESH default rules
            dir (an empty-default mapping-rules.yaml - NEVER the committed overlay).
      Do NOT ship fixtures/<System> with a .git that points anywhere the curator could
      pull references from; a plain checkout at the pinned SHA is enough.
#>
[CmdletBinding()]
param(
    # Curator code, e.g. R-C. Namespaces the trace + rules so two curators never collide.
    [Parameter(Mandatory)] [string] $Curator,

    # System under curation.
    [string] $System = 'squidex',

    # Packet / workspace root. Defaults to this script's directory (the packet root).
    # The facilitator can point it at the eval-repo root to test in place.
    [string] $Root = $PSScriptRoot,

    # Localhost port for the GUI (arch serve default is 8765).
    [int] $Port = 8765
)

$ErrorActionPreference = 'Stop'

# --- resolve the four packet paths -----------------------------------------------
$fixture  = Join-Path $Root "fixtures/$System"
$archDir  = Join-Path $Root "out/$System/blind-curation/ws-$Curator/architecture"
$rulesDir = Join-Path $Root "out/$System/blind-curation/curator-rules-$Curator"
$traceDir = Join-Path $Root "out/$System/blind-curation"
$trace    = Join-Path $traceDir "edit-trace-$Curator.jsonl"

# --- preflight: fail loudly with an actionable message ---------------------------
function Assert-Path([string]$Path, [string]$What) {
    if (-not (Test-Path $Path)) {
        throw "$What not found:`n    $Path`nThe facilitator must prepare it before this session (see the header of this script)."
    }
}
Assert-Path $fixture  "Fixture checkout"
Assert-Path $archDir  "Prepared workspace (arch-dir)"
Assert-Path $rulesDir "Your rules dir"
if (-not (Test-Path (Join-Path $rulesDir 'mapping-rules.yaml'))) {
    throw "No mapping-rules.yaml in your rules dir:`n    $rulesDir`nAsk the facilitator to seed a fresh default there."
}

# Ensure the trace directory exists (the file itself is created on first edit).
New-Item -ItemType Directory -Force -Path $traceDir | Out-Null

# --- edit trace: the load-bearing env var ----------------------------------------
$env:ANON_EDIT_TRACE = $trace

# --- pick a free port (requested one, else the next free above it) ---------------
# The default 8765 is often grabbed by a WSL localhost relay (wslrelay) or a stale
# serve; probe upward so a curator never hits a bind error.
function Test-PortFree([int]$p) {
    -not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}
$chosen = $Port
while (-not (Test-PortFree $chosen) -and $chosen -lt ($Port + 20)) {
    Write-Host "  port $chosen busy - trying $($chosen + 1)" -ForegroundColor Yellow
    $chosen++
}
if (-not (Test-PortFree $chosen)) { throw "No free port in $Port..$($Port + 20). Free one or pass -Port." }
$Port = $chosen

# --- locate `arch` (prefer a bundled venv, else PATH) ----------------------------
$venvArch = Join-Path $Root ".venv/Scripts/arch.exe"
$arch = if (Test-Path $venvArch) { $venvArch } else { 'arch' }

Write-Host ""
Write-Host "  RQ9 blind curation - $System - curator $Curator" -ForegroundColor Cyan
Write-Host "  edit trace : $trace"
Write-Host "  rules dir  : $rulesDir"
Write-Host "  workspace  : $archDir"
Write-Host "  GUI        : http://127.0.0.1:$Port/  (the token-bearing URL is printed below)" -ForegroundColor Cyan
Write-Host ""

# --- launch the read-only-write GUI server ---------------------------------------
# arch serve prints the per-session tokened URL to open. Ctrl-C to stop; relaunch via
# this script for every subsequent sitting.
& $arch serve --repo $fixture --arch-dir $archDir --rules-dir $rulesDir --port $Port
