# Start-menu "Restart Camera Keeper". Asks Windows for admin rights, then stops and starts the task.
$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
& (Join-Path $PSScriptRoot 'stop.ps1')
Start-ScheduledTask -TaskName 'Camera Keeper'
