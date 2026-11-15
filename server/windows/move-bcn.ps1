<#
Move BCN (Camera Keeper) from tapper to another Camera Keeper server, run from any Windows PC
on the VPN that can SSH to tapper (key login as claude@100.64.0.2). The new server only needs
Camera Keeper installed and running (CameraKeeper-Setup.exe); nothing has to be signed in there.

  .\move-bcn.ps1 export    1. stops Camera Keeper on tapper and exports everything to one .zip
                              (Arlo/Blink/IP-camera accounts and sessions, phone numbers, alert
                              settings, the away-from-home password, Barb's photo, the phone app,
                              all clips)
                           2. downloads it to this PC and checks it (SHA-256 and contents)
                           3. only then turns BCN off on tapper: service disabled, recording marked
                              "moved" (so it never signs in to Arlo again there), selfheal stops
                              watching it. Clips stay on tapper until you delete them.
  .\move-bcn.ps1 import    4. asks where the new server keeps clips (its drives are listed; or pass
                              -ClipsFolder 'D:\BCN Clips'), then uploads the .zip; it restarts and
                              imports it
                           5. waits until the new server is signed in to the cameras
                           6. points bcn.tapper.co (and the old ban.tapper.co) at the new server
                              with a Cloudflare API token (asked for; used once, not saved)
                           7. points GarysTV at the new server: tapper's, and this PC's if it used
                              tapper's BCN
                           8. checks https://bcn.tapper.co from outside, then deletes the .zip
                              (it holds passwords; -KeepFile keeps it)
  .\move-bcn.ps1 all       export, then import
  .\move-bcn.ps1 undo      turns BCN back on at tapper (if the move has to be called off)

Defaults: -From claude@100.64.0.2  -To http://100.64.0.8:8090 (DESKTOP-OFIE0BD)
          -Folder <Documents>\BCN-move
#>
param(
    [Parameter(Position = 0)][ValidateSet('export', 'import', 'all', 'undo')][string]$Step = 'all',
    [string]$From = 'claude@100.64.0.2',
    [string]$To = 'http://100.64.0.8:8090',
    [string]$Folder = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'BCN-move'),
    [string]$File,
    [string]$Hostname = 'bcn.tapper.co',
    [string]$CloudflareToken,
    [string]$ClipsFolder,
    [switch]$NoTunnel,      # leave bcn.tapper.co alone (switch it later on the new server's Move page)
    [switch]$NoClips,
    [switch]$KeepFile,
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$To = $To.TrimEnd('/')
$RemoteDir = '/var/tmp'

function Say([string]$m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Ok([string]$m) { Write-Host "   $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "   $m" -ForegroundColor Yellow }

# --- running things on tapper ------------------------------------------------------------
# Each remote step is a bash script sent base64-encoded, so no quoting survives PowerShell.
$Common = @'
set -euo pipefail
APP=/opt/camera-keeper
ENVF=/etc/camera-keeper/camera-keeper.env
ENVS=$(sudo grep -v '^\s*#' "$ENVF" | grep '=' | xargs)
py() {   # the app's python, with the service's settings, as the service's user
  local code=$1; shift
  sudo -u camera-keeper env $ENVS "$APP/venv/bin/python" -c "import sys; sys.path.insert(0, '$APP'); $code" "$@"
}
'@

function Remote([string]$Script, [string[]]$ArgList = @()) {
    $text = ($Common + "`n" + $Script) -replace "`r`n", "`n"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
    $quoted = ($ArgList | ForEach-Object { "'" + ($_ -replace "'", "'\''") + "'" }) -join ' '
    $cmd = "echo $b64 | base64 -d > /tmp/bcn-move.sh && bash /tmp/bcn-move.sh $quoted; rc=`$?; rm -f /tmp/bcn-move.sh; exit `$rc"
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 $From $cmd 2>&1 |
            ForEach-Object { if ($_ -is [Management.Automation.ErrorRecord]) { "$_" } else { $_ } } |
            Where-Object { $_ -notmatch 'Pseudo-terminal will not be allocated' } |
            ForEach-Object { Write-Host "   $_" }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

$ExportScript = @'
OUT=$1; shift
echo "Stopping Camera Keeper on $(hostname) for a clean copy..."
sudo systemctl stop camera-keeper
sudo rm -f "$OUT" "$OUT.sha256"
sudo install -o camera-keeper -m 600 /dev/null "$OUT"
py "from app import migrate; migrate.main(sys.argv[1:])" export "$OUT" "$@"
sudo chown "$(id -un)" "$OUT"
sha256sum "$OUT" | cut -d' ' -f1 > "$OUT.sha256"
'@

$RestartScript = @'
sudo systemctl start camera-keeper
echo "Camera Keeper is running on $(hostname) again."
'@

$FinishExportScript = @'
OUT=$1; TO=$2
py "from app import store; import time; store.init(); store.set_setting('moved_away', {'at': time.strftime('%Y-%m-%d %H:%M'), 'to': sys.argv[1]})" "$TO"
sudo systemctl disable camera-keeper 2>&1 | grep -v '^Removed' || true
echo "Camera Keeper on $(hostname): $(systemctl is-active camera-keeper), $(systemctl is-enabled camera-keeper 2>/dev/null)."
C=/etc/selfheal/selfheal.conf
if [ -f "$C" ] && grep -q camera-keeper "$C"; then
  sudo cp -a "$C" "$C.before-bcn-move"
  sudo sed -i -E '/^SERVICES=/s/ ?camera-keeper//; /^HTTP_CHECKS=/s# ?camera-keeper\|http://127\.0\.0\.1:8090/health##' "$C"
  echo "selfheal no longer watches camera-keeper (old settings: $C.before-bcn-move)."
fi
sudo rm -f "$OUT" "$OUT.sha256"
'@

$UndoScript = @'
C=/etc/selfheal/selfheal.conf
[ -f "$C.before-bcn-move" ] && sudo cp -a "$C.before-bcn-move" "$C" && echo "selfheal watches camera-keeper again."
py "from app import store; store.init(); c = store.connect(); c.execute(\"DELETE FROM settings WHERE key='moved_away'\"); c.commit()"
sudo systemctl enable --now camera-keeper 2>&1 | grep -v '^Created' || true
echo "Camera Keeper on $(hostname): $(systemctl is-active camera-keeper)."
'@

$GarysScript = @'
URL=$1
if curl -sf -m 10 http://127.0.0.1:5480/api/settings -o /tmp/garystv-settings.json; then
  python3 - "$URL" <<'P'
import json, sys, urllib.request
s = json.load(open('/tmp/garystv-settings.json'))
a = s.get('arlo') or {}
if not a.get('enabled'):
    print("GarysTV on tapper doesn't use BCN; nothing to change.")
elif a.get('url', '').rstrip('/') == sys.argv[1]:
    print('GarysTV on tapper already uses', sys.argv[1])
else:
    old = a.get('url'); a['url'] = sys.argv[1]
    req = urllib.request.Request('http://127.0.0.1:5480/api/settings', data=json.dumps({'arlo': a}).encode(),
                                 method='PUT', headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=10).read()
    print('GarysTV on tapper: camera server', old, '->', sys.argv[1])
P
  rm -f /tmp/garystv-settings.json
else
  echo "GarysTV isn't answering on tapper; set Settings > Cameras > Camera Keeper address by hand."
fi
'@

# --- helpers --------------------------------------------------------------------------------
$OldHosts = @(($From -split '@')[-1], '10.0.0.156', '10.0.0.225', 'tapper')   # tapper's addresses

function Set-LocalGarysTV([string]$Url) {
    # GarysTV on the PC running this script (Fort Madison's, for one), if it uses tapper's BCN.
    $f = Join-Path $env:APPDATA 'garystv\data\settings.json'
    if (-not (Test-Path $f)) { return }
    $j = Get-Content $f -Raw | ConvertFrom-Json
    if (-not $j.arlo -or -not $j.arlo.enabled) { return }
    $cur = "$($j.arlo.url)".TrimEnd('/')
    if ($cur -eq $Url) { Ok "GarysTV on this PC already uses $Url."; return }
    if ($cur -match '(bcn|ban)\.tapper\.co') { Ok "GarysTV on this PC uses $cur, which follows the move by itself."; return }
    $fromOld = $OldHosts + ($To -replace '^https?://', '' -replace ':.*$', '')
    if (-not ($fromOld | Where-Object { $cur -match [regex]::Escape($_) })) {
        Warn "GarysTV on this PC uses $cur; leaving it alone."; return
    }
    $j.arlo.url = $Url
    try {
        Invoke-WebRequest http://127.0.0.1:5480/api/settings -Method Put -ContentType 'application/json' `
            -Body (@{ arlo = $j.arlo } | ConvertTo-Json -Depth 5 -Compress) -UseBasicParsing -TimeoutSec 10 | Out-Null
        Ok "GarysTV on this PC ($env:COMPUTERNAME): camera server $cur -> $Url"
    } catch { Warn "GarysTV on this PC isn't answering; set Settings > Cameras > Camera Keeper address to $Url by hand." }
}

function Get-Json([string]$Url, [int]$Timeout = 10) {
    try { return Invoke-RestMethod $Url -TimeoutSec $Timeout -UseBasicParsing } catch { return $null }
}

function Test-Export([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $m = $z.Entries | Where-Object FullName -eq 'ban-export.json' | Select-Object -First 1
        $db = $z.Entries | Where-Object FullName -eq 'clips.db' | Select-Object -First 1
        if (-not $m -or -not $db) { throw "$Path isn't a complete Camera Keeper export." }
        $r = New-Object IO.StreamReader($m.Open())
        try { $man = $r.ReadToEnd() | ConvertFrom-Json } finally { $r.Close() }
        $files = ($z.Entries | Where-Object { $_.FullName -like 'clips/*.mp4' }).Count
        return @{ Manifest = $man; ClipFiles = $files }
    } finally { $z.Dispose() }
}

# --- export ----------------------------------------------------------------------------------------
function Invoke-Export {
    Say "Export from tapper ($From)"
    New-Item -ItemType Directory -Force $Folder | Out-Null
    $name = "bcn-export-tapper-$(Get-Date -Format yyyyMMdd-HHmm).zip"
    $remote = "$RemoteDir/$name"
    $local = Join-Path $Folder $name
    $exportArgs = @($remote); if ($NoClips) { $exportArgs += '--no-clips' }

    if ((Remote $ExportScript $exportArgs) -ne 0) {
        Warn "The export failed on tapper. Turning Camera Keeper back on there."
        Remote $RestartScript | Out-Null
        throw 'Export failed; nothing was moved.'
    }

    Say "Downloading to $local"
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & scp -o BatchMode=yes "${From}:$remote" "${From}:$remote.sha256" "$Folder\" 2>&1 | Out-Null
    $scpRc = $LASTEXITCODE; $ErrorActionPreference = $old
    try {
        if ($scpRc -ne 0 -or -not (Test-Path $local)) { throw 'The download failed.' }
        $want = (Get-Content "$local.sha256" -Raw).Trim()
        $got = (Get-FileHash $local -Algorithm SHA256).Hash.ToLower()
        if ($want -ne $got) { throw "The download is damaged (SHA-256 $got, expected $want)." }
        $t = Test-Export $local
        Ok ("Checked: {0:N2} GB, SHA-256 matches, {1} clips in the database, {2} clip files, made {3}." -f `
            ((Get-Item $local).Length / 1GB), $t.Manifest.clips, $t.ClipFiles, $t.Manifest.made)
        Remove-Item "$local.sha256" -Force
    } catch {
        Warn "$($_.Exception.Message) Turning Camera Keeper back on at tapper; the export is still at $remote there."
        Remote $RestartScript | Out-Null
        throw
    }

    Say "Turning BCN off on tapper"
    if ((Remote $FinishExportScript @($remote, $To)) -ne 0) { Warn 'Some of that failed; check the lines above.' }
    Ok "Export done: $local"
    return $local
}

# --- where the new server keeps clips ---------------------------------------------------------------
function Select-ClipsFolder([long]$Need) {
    $loc = Get-Json "$To/api/storage/location"
    if (-not $loc) {
        if ($ClipsFolder) { throw "Camera Keeper at $To is too old to choose a clips folder. Install the newest CameraKeeper-Setup.exe there first." }
        Warn "Camera Keeper at $To can't choose a clips folder (older version); clips go to its data folder."
        return
    }
    $gb = { param($b) if ($null -eq $b) { '?' } else { '{0:N0} GB' -f ($b / 1GB) } }
    $want = $ClipsFolder
    if (-not $want -and -not $Yes) {
        Write-Host ("   Clips on the new server go to: {0}  ({1} free; the export is {2})" -f $loc.clips_dir, (& $gb $loc.free), (& $gb $Need))
        if ($loc.can_change) {
            $i = 0
            foreach ($d in $loc.drives) { $i++; Write-Host ("     {0}. {1}  {2} free of {3}" -f $i, $d.path, (& $gb $d.free), (& $gb $d.total)) }
            $ans = (Read-Host '   Enter to keep it, a number to use that drive, or type a folder').Trim().Trim('"')
            if ($ans -match '^\d+$' -and [int]$ans -ge 1 -and [int]$ans -le $loc.drives.Count) {
                $root = $loc.drives[[int]$ans - 1].path
                $want = if ($root -match '^[A-Za-z]:\\$') { $root + 'BCN Clips' } else { $root.TrimEnd('/') + '/bcn-clips' }
            } elseif ($ans) { $want = $ans }
        }
    }
    if (-not $want -or $want.TrimEnd('\', '/') -eq $loc.clips_dir) {
        if ($loc.free -and $loc.free -lt $Need * 1.1) { Warn "Only $(& $gb $loc.free) free there for $(& $gb $Need) of export." }
        return
    }
    if (-not $loc.can_change) { throw "The clips folder on $To is set in its env file (CLIPS_DIR); change it there." }
    Write-Host "   Switching the clips folder to $want ..."
    try {
        $r = Invoke-RestMethod "$To/api/storage/location" -Method Post -ContentType 'application/json' `
            -Body (@{ path = $want } | ConvertTo-Json -Compress) -TimeoutSec 30 -UseBasicParsing
    } catch {
        $msg = $_.ErrorDetails.Message; try { $msg = ($msg | ConvertFrom-Json).error } catch { }
        throw "The new server refused that folder: $msg"
    }
    $target = $r.move.to
    $deadline = (Get-Date).AddMinutes(20)
    Start-Sleep 3
    while ((Get-Date) -lt $deadline) {
        $l = Get-Json "$To/api/storage/location" 5
        if ($l -and $l.clips_dir -eq $target -and -not $l.move.moving) {
            Ok ("Clips folder on the new server: {0} ({1} free)" -f $l.clips_dir, (& $gb $l.free)); return
        }
        if ($l -and $l.move.error) { throw "Switching the clips folder failed: $($l.move.error)" }
        Start-Sleep 3
    }
    throw "The clips folder switch didn't finish; check $To/settings#storage."
}

# --- import ----------------------------------------------------------------------------------------
function Invoke-Import([string]$Path) {
    if (-not $Path) {
        $Path = Get-ChildItem $Folder -Filter 'bcn-export-*.zip' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Path -or -not (Test-Path $Path)) { throw "No export to import. Run '.\move-bcn.ps1 export' first, or pass -File." }
    $t = Test-Export $Path

    Say "Import into $To"
    $st = Get-Json "$To/api/status"
    if (-not $st) { throw "Camera Keeper isn't answering at $To. Is it installed and running there?" }
    if (($st.stats.count -gt 0 -or -not $st.needs_login) -and -not $Yes) {
        $a = Read-Host "   $To already has accounts or $($st.stats.count) clips. Its settings and sign-ins get replaced. Continue? (y/N)"
        if ($a -notmatch '^[yY]') { throw 'Stopped; nothing was imported.' }
    }

    Select-ClipsFolder (Get-Item $Path).Length

    Write-Host ("   Uploading {0:N2} GB ({1} clips)..." -f ((Get-Item $Path).Length / 1GB), $t.Manifest.clips)
    $resp = Join-Path $env:TEMP 'bcn-import-response.json'
    & curl.exe -sS --progress-bar -X POST -T $Path -H 'Content-Type: application/zip' -o $resp "$To/api/move/import"
    $r = Get-Content $resp -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
    Remove-Item $resp -ErrorAction SilentlyContinue
    if (-not $r.ok) { throw "The upload was refused: $($r.error)" }
    Ok "Uploaded. Camera Keeper there restarts and imports it..."

    $deadline = (Get-Date).AddMinutes(30); $res = $null
    Start-Sleep 5
    while ((Get-Date) -lt $deadline) {
        $s = Get-Json "$To/api/move/state" 5
        if ($s -and -not $s.pending -and $s.last -and $s.last.finished) { $res = $s.last; break }
        Start-Sleep 5
    }
    if (-not $res) { throw "The import hasn't finished after 30 minutes. Check $To/move." }
    if (-not $res.ok) { throw "The import failed there: $($res.error)" }
    Ok ("Imported from {0}: {1} clips added{2}." -f $res.from, $res.clips_added,
        $(if ($res.settings_changed) { "; settings brought over: $($res.settings_changed -join ', ')" } else { '' }))

    Write-Host '   Waiting for it to sign in to the cameras...'
    $deadline = (Get-Date).AddMinutes(4); $h = $null
    while ((Get-Date) -lt $deadline) {
        $h = Get-Json "$To/health" 5
        if ($h -and $h.connected) { break }
        Start-Sleep 5
    }
    if ($h -and $h.connected) { Ok "Signed in: $($h.accounts) Arlo account(s) connected." }
    else { Warn "Not signed in yet$(if ($h.error) { " ($($h.error))" }). Check $To/connect; Arlo may want a code." }

    Say "Pointing $Hostname at the new server"
    $token = $CloudflareToken
    if ($NoTunnel) {
        Warn "Left as it is (-NoTunnel). Switch it later on $To/move (section 4)."
    } elseif (-not $token) {
        $sec = Read-Host "   Cloudflare API token (Cloudflare Tunnel: Edit and DNS: Edit; Enter to skip)" -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
    if ($token -and -not $NoTunnel) {
        $html = & curl.exe -sS -m 180 --data-urlencode "api_token=$token" --data-urlencode "hostname=$Hostname" "$To/move/tunnel"
        $token = $null
        $page = $html -join "`n"
        $err = [regex]::Match($page, '<div class="flash error">(.*?)</div>', 'Singleline')
        if ($err.Success) { Warn ("Cloudflare: " + [Net.WebUtility]::HtmlDecode($err.Groups[1].Value.Trim())) }
        [regex]::Matches($page, '<li>(.*?)</li>') | ForEach-Object { Ok ([Net.WebUtility]::HtmlDecode($_.Groups[1].Value)) }
    } elseif (-not $NoTunnel) {
        Warn "Skipped. Do it later on $To/move (section 4)."
    }

    Say 'GarysTV'
    Remote $GarysScript @($To) | Out-Null
    Set-LocalGarysTV $To

    Say 'Checking from outside'
    $okOutside = $false
    foreach ($i in 1..12) {
        try {
            $m = Invoke-WebRequest "https://$Hostname/manifest.webmanifest" -UseBasicParsing -TimeoutSec 10
            if ($m.StatusCode -eq 200) { $okOutside = $true; break }
        } catch { }
        Start-Sleep 5
    }
    if ($okOutside) { Ok "https://$Hostname answers (tapper's copy is off, so that's the new server)." }
    else { Warn "https://$Hostname isn't answering yet; give it a minute, or check $To/move." }

    if ($KeepFile) { Warn "Kept $Path. Delete it when you're done: it holds the Arlo and Twilio passwords." }
    else { Remove-Item $Path -Force; Ok "Deleted $Path (it held the passwords)." }

    Say 'Done'
    Write-Host "   BCN now runs at $To. Phones on its network find it there; everywhere else they use https://$Hostname."
    Write-Host "   Tapper still has the old clips (/var/lib/camera-keeper) until you delete them."
}

# --- go -------------------------------------------------------------------------------------------
switch ($Step) {
    'export' { Invoke-Export | Out-Null }
    'import' { Invoke-Import $File }
    'all' { $f = Invoke-Export; Invoke-Import $f }
    'undo' {
        Say 'Turning BCN back on at tapper'
        Remote $UndoScript | Out-Null
        Remote $GarysScript @('http://127.0.0.1:8090') | Out-Null
        Set-LocalGarysTV ('http://' + ($From -split '@')[-1] + ':8090')
        Warn "If bcn.tapper.co was already pointed at the new server, point it back from http://tapper:8090/move."
    }
}
