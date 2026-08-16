param(
    [string]$OutputDirectory = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "InventoryLauncher.cs"
$frameworkCompiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$compilerCommand = Get-Command csc.exe -ErrorAction SilentlyContinue

if ($compilerCommand) {
    $compiler = $compilerCommand.Source
} elseif (Test-Path -LiteralPath $frameworkCompiler) {
    $compiler = $frameworkCompiler
} else {
    throw "The Windows C# compiler was not found. Install the .NET Framework developer tools."
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

$initializeOutput = Join-Path $OutputDirectory "Initialize Inventory Sheet.exe"
$botOutput = Join-Path $OutputDirectory "Start Inventory Bot.exe"

& $compiler /nologo /target:exe /platform:anycpu /optimize+ /define:INIT_SHEET "/out:$initializeOutput" $source
if ($LASTEXITCODE -ne 0) {
    throw "Building Initialize Inventory Sheet.exe failed."
}

& $compiler /nologo /target:exe /platform:anycpu /optimize+ "/out:$botOutput" $source
if ($LASTEXITCODE -ne 0) {
    throw "Building Start Inventory Bot.exe failed."
}

Write-Host "Built: $initializeOutput"
Write-Host "Built: $botOutput"
