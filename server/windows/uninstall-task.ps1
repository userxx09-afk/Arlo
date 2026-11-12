# Run by the uninstaller (as admin). Stops Arlo Keeper and removes its scheduled task and firewall rule.
# Clips, settings and the Arlo sign-in in %ProgramData%\ArloKeeper are kept.
param([Parameter(Mandatory)][string]$AppDir)
$TaskName = 'Arlo Keeper'
& (Join-Path $AppDir 'stop.ps1') -AppDir $AppDir
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

# The ban.tapper.co tunnel, if the Move page set it up on this PC (its cloudflared lives in ProgramData).
$svc = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'" -ErrorAction SilentlyContinue
if ($svc -and $svc.PathName -like "*\ArloKeeper\cloudflared\*") {
    Stop-Service Cloudflared -Force -ErrorAction SilentlyContinue
    & sc.exe delete Cloudflared | Out-Null
}
