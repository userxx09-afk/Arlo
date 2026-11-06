# Builds the BAN Android app (signed release) and puts it on tapper, where the Alerts page offers it.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$sdk = "C:\Users\x\Claude\meshwall\toolchain\android-sdk"
"sdk.dir=$($sdk -replace '\\','\\')" | Set-Content -Encoding ascii local.properties

# Gradle wrapper: the same one GarysTV's Fire TV app uses (already downloaded on this PC).
if (-not (Test-Path gradlew.bat)) {
  $g = "C:\Users\x\Projects\GarysTV\firetv"
  Copy-Item "$g\gradlew", "$g\gradlew.bat" .
  New-Item -ItemType Directory -Force gradle\wrapper | Out-Null
  Copy-Item "$g\gradle\wrapper\*" gradle\wrapper\
}

# BAN's own signing key, made once. Keep it: updates only install over an app with the same key.
$keyDir = "C:\Users\x\Projects\BAN-signing-key"
$ks = "$keyDir\ban.keystore"
if (-not (Test-Path $ks)) {
  New-Item -ItemType Directory -Force $keyDir | Out-Null
  $pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
  $pw | Set-Content -Encoding ascii "$keyDir\BAN_KEYSTORE_PASSWORD.txt"
  cmd /c "keytool -genkeypair -keystore `"$ks`" -storepass $pw -keypass $pw -alias ban -keyalg RSA -keysize 2048 -validity 36500 -dname `"CN=BAN, O=Home`" 2>&1"
  if (-not (Test-Path $ks)) { throw "couldn't make the signing key" }
}
$pw = (Get-Content "$keyDir\BAN_KEYSTORE_PASSWORD.txt").Trim()
@"
storeFile=$($ks -replace '\\','/')
storePassword=$pw
keyAlias=ban
"@ | Set-Content -Encoding ascii signing.properties

$code = if ($env:BAN_VERSION_CODE) { $env:BAN_VERSION_CODE } else { "1" }
$name = if ($env:BAN_VERSION_NAME) { $env:BAN_VERSION_NAME } else { "1.0" }
cmd /c ".\gradlew.bat --no-daemon -q assembleRelease -PbanVersionCode=$code -PbanVersionName=$name 2>&1"
if ($LASTEXITCODE -ne 0) { throw "build failed" }

New-Item -ItemType Directory -Force build | Out-Null
Copy-Item app\build\outputs\apk\release\app-release.apk build\BAN.apk -Force
Write-Host "Built $here\build\BAN.apk ($([math]::Round((Get-Item build\BAN.apk).Length/1KB)) KB)"

$ErrorActionPreference = "Continue"
scp build\BAN.apk tapper:/tmp/ban.apk
ssh -o BatchMode=yes tapper "sudo install -o arlo-keeper -g arlo-keeper -m 644 /tmp/ban.apk /var/lib/arlo-keeper/ban.apk && rm /tmp/ban.apk && echo 'On tapper: http://10.0.0.156:8090/ban.apk'"
