[CmdletBinding()]
param(
    [string]$Refs = ".\references.json",
    [string]$State = "drivers.json",
    [string]$Server = "https://black-pearl.racing",
    
    [Parameter(Mandatory = $true, HelpMessage = "Please provide your API token.")]
    [string]$Token
)

# Set execution to stop on errors
$ErrorActionPreference = "Stop"

Write-Host "Running PIT Predictor..." -ForegroundColor Cyan

# Call uv run directly with Python's expected argument name (--refs)
uv run python .\run_pit_predictor.py --refs $Refs --state $State --server $Server --token $Token

if ($LASTEXITCODE -eq 0) {
    Write-Host "Execution completed successfully!" -ForegroundColor Green
} else {
    Write-Host "Execution failed with exit code $LASTEXITCODE." -ForegroundColor Red
}