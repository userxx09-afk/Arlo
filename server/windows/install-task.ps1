# Run by the installer (as admin). Sets up the "Arlo Keeper" scheduled task (starts at boot, runs
# as SYSTEM with no window, restarts if it stops), opens the web port to the home network, and starts it.
param([Parameter(Mandatory)][string]$AppDir)
$ErrorActionPreference = 'Stop'
$TaskName = 'Arlo Keeper'

# Port from the settings file, if it was changed there.
$Port = 8090
$settings = Join-Path $env:ProgramData 'ArloKeeper\arlo-keeper.env'
if (Test-Path $settings) {
    $m = Select-String -Path $settings -Pattern '^\s*WEB_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
}

$pyw = Join-Path $AppDir 'python\pythonw.exe'
$launcher = Join-Path $AppDir 'launcher.py'
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$launcher`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description 'Saves Arlo clips, sends alerts, serves the BAN web page.' -Force | Out-Null

# Let phones, TVs and GarysTV on the home network reach it (whatever Windows calls the network).
Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $TaskName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress LocalSubnet -Profile Any -Action Allow | Out-Null

Start-ScheduledTask -TaskName $TaskName

# Wait (up to 45 s) for the web page, so "Open Arlo Keeper" at the end of setup finds it.
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $Port); $c.Close(); break
    } catch { Start-Sleep -Seconds 1 }
}
