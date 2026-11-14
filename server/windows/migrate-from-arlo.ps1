# Run by setup (as admin) before it installs anything. Before the rename to Camera Keeper / BCN this
# was "Arlo Keeper": scheduled task "Arlo Keeper", C:\Program Files\Arlo Keeper and
# %ProgramData%\ArloKeeper. This moves such an install over, once:
#   - stops and removes the old task and its firewall rule
#   - moves the clips, settings and Arlo/Blink sign-ins to %ProgramData%\CameraKeeper
#   - points the bcn.tapper.co tunnel service (cloudflared, if the Move page set it up) at the new folder
#   - removes the old program folder and Start-menu folder
# Does nothing on a PC that never had Arlo Keeper. Notes go to %TEMP%\camera-keeper-migrate.log.
param([string]$OldAppDir = (Join-Path $env:ProgramFiles 'Arlo Keeper'))
$ErrorActionPreference = 'Continue'
$OldTask = 'Arlo Keeper'
$oldData = Join-Path $env:ProgramData 'ArloKeeper'
$newData = Join-Path $env:ProgramData 'CameraKeeper'
$log = Join-Path $env:TEMP 'camera-keeper-migrate.log'
function Note($m) { Add-Content -Path $log -Value ('{0}  {1}' -f (Get-Date -Format s), $m) }

$task = Get-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue
$svc = Get-CimInstance Win32_Service -Filter "Name='Cloudflared'" -ErrorAction SilentlyContinue
$tunnelHere = $svc -and $svc.PathName -like '*\ArloKeeper\cloudflared\*'
if (-not $task -and -not (Test-Path $oldData) -and -not (Test-Path $OldAppDir)) { exit 0 }
Note "Moving Arlo Keeper to Camera Keeper (old program folder: $OldAppDir)"

# 1. Stop the old copy: its task, then anything still running from its folder.
if ($task) {
    Stop-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
$dir = $OldAppDir.TrimEnd('\') + '\'
Get-Process python, pythonw, ffmpeg, ffprobe -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
if ($tunnelHere) {
    Stop-Service Cloudflared -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
if ($task) { Unregister-ScheduledTask -TaskName $OldTask -Confirm:$false -ErrorAction SilentlyContinue; Note 'removed the old task' }
Get-NetFirewallRule -DisplayName $OldTask -ErrorAction SilentlyContinue | Remove-NetFirewallRule

# 2. Move the data. Files can stay locked for a moment after the processes end, so try a few times.
#    If it still fails, the launcher tries again when Camera Keeper starts.
if ((Test-Path $oldData) -and -not (Test-Path $newData)) {
    for ($i = 1; $i -le 10 -and (Test-Path $oldData); $i++) {
        try { Move-Item -LiteralPath $oldData -Destination $newData -ErrorAction Stop; Note "moved $oldData to $newData" }
        catch { Note "move attempt $i failed: $($_.Exception.Message)"; Start-Sleep -Seconds 2 }
    }
} elseif ((Test-Path $oldData) -and (Test-Path $newData)) {
    Note "both $oldData and $newData exist; left $oldData alone"
}
if (Test-Path $newData) {
    $oldEnv = Join-Path $newData 'arlo-keeper.env'
    $newEnv = Join-Path $newData 'camera-keeper.env'
    if ((Test-Path $oldEnv) -and -not (Test-Path $newEnv)) { Rename-Item $oldEnv 'camera-keeper.env' }
    if (Test-Path $newEnv) {   # a DATA_DIR written out as the old folder
        $text = Get-Content -Raw $newEnv
        $fixed = $text -replace [regex]::Escape($oldData), $newData
        if ($fixed -ne $text) { Set-Content -Path $newEnv -Value $fixed -NoNewline -Encoding UTF8 }
    }
    foreach ($n in 'arlo-keeper.log', 'arlo-keeper.log.1') {
        $p = Join-Path $newData "logs\$n"
        if (Test-Path $p) { Rename-Item $p ($n -replace 'arlo-keeper', 'camera-keeper') -ErrorAction SilentlyContinue }
    }
}

# 3. The tunnel service ran cloudflared from the old data folder: point it at the new one.
if ($tunnelHere) {
    $exe = Join-Path $newData 'cloudflared\cloudflared.exe'
    if (Test-Path $exe) {
        $newPath = $svc.PathName -replace '\\ArloKeeper\\cloudflared\\', '\CameraKeeper\cloudflared\'
        Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Cloudflared' -Name ImagePath -Value $newPath
        Note 'tunnel service now runs from the new folder'
    }
    Start-Service Cloudflared -ErrorAction SilentlyContinue
}

# 4. The old program files and Start-menu folder (the installer puts the new ones in place).
if (Test-Path $OldAppDir) { Remove-Item -LiteralPath $OldAppDir -Recurse -Force -ErrorAction SilentlyContinue }
$oldMenu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Arlo Keeper'
if (Test-Path $oldMenu) { Remove-Item -LiteralPath $oldMenu -Recurse -Force -ErrorAction SilentlyContinue }
Note 'done'
exit 0
