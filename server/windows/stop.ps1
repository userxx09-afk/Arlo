# Stops Camera Keeper: ends the scheduled task and any of its python/ffmpeg processes still running.
param([string]$AppDir = $PSScriptRoot)
Stop-ScheduledTask -TaskName 'Camera Keeper' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$dir = $AppDir.TrimEnd('\') + '\'
Get-Process python, pythonw, ffmpeg, ffprobe -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
