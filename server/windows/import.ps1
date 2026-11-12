# Import an Arlo Keeper export from the command line (for very big exports; the Move page at
# http://localhost:8090/move does the same thing with an upload). Run as administrator:
#   powershell -ExecutionPolicy Bypass -File "C:\Program Files\Arlo Keeper\import.ps1" D:\ban-export.zip
param([Parameter(Mandatory)][string]$File, [string]$AppDir = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this as administrator.' }
$File = (Resolve-Path $File).Path
$data = Join-Path $env:ProgramData 'ArloKeeper'
$settings = Join-Path $data 'arlo-keeper.env'

# Same DATA_DIR the launcher uses (the settings file can move it).
$env:DATA_DIR = $data
if (Test-Path $settings) {
    $m = Select-String -Path $settings -Pattern '^\s*DATA_DIR\s*=\s*(.+)$' | Select-Object -First 1
    if ($m) { $env:DATA_DIR = $m.Matches[0].Groups[1].Value.Trim().Trim('"') }
}
Write-Host "Stopping Arlo Keeper..."
& (Join-Path $AppDir 'stop.ps1') -AppDir $AppDir
Push-Location $AppDir
try {
    & (Join-Path $AppDir 'python\python.exe') -m app.migrate import $File --settings-file $settings
    if ($LASTEXITCODE -ne 0) { throw "Import failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
    Write-Host "Starting Arlo Keeper..."
    Start-ScheduledTask -TaskName 'Arlo Keeper'
}
Write-Host "Done. Open http://localhost:8090/move to point ban.tapper.co here, then delete $File (it holds passwords)."
