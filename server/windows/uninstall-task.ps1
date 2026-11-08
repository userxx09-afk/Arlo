# Run by the uninstaller (as admin). Stops Arlo Keeper and removes its scheduled task and firewall rule.
# Clips, settings and the Arlo sign-in in %ProgramData%\ArloKeeper are kept.
param([Parameter(Mandatory)][string]$AppDir)
$TaskName = 'Arlo Keeper'
& (Join-Path $AppDir 'stop.ps1') -AppDir $AppDir
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
