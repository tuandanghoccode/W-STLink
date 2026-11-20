param(
    [string]$OpenOcdArchive,
    [string]$AppName = 'STM32 Wireless Debug 1.3'
)

$ErrorActionPreference = 'Stop'
$hostRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $hostRoot
$packRoot = Join-Path $hostRoot 'build'
$venv = Join-Path $packRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$vendorName = 'xpack-openocd-0.12.0-7'
$vendorSha256 = '6BFD3C97135AAFEF8AFFC9AF1ACF34FD0E2B9CA26044506F6ABD7F95B7630052'
$vendor = Join-Path $packRoot $vendorName
New-Item -ItemType Directory -Path $packRoot -Force | Out-Null

if (-not $OpenOcdArchive) {
    $OpenOcdArchive = Join-Path $packRoot "$vendorName-win32-x64.zip"
    if (-not (Test-Path -LiteralPath $OpenOcdArchive)) {
        $uri = "https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v0.12.0-7/$vendorName-win32-x64.zip"
        Invoke-WebRequest -Uri $uri -OutFile $OpenOcdArchive
    }
}
if ((Get-FileHash -LiteralPath $OpenOcdArchive -Algorithm SHA256).Hash -ne $vendorSha256) {
    throw "OpenOCD archive SHA-256 does not match the pinned Windows x64 release."
}
Expand-Archive -LiteralPath $OpenOcdArchive -DestinationPath $packRoot -Force

if (-not (Test-Path -LiteralPath $python)) {
    & py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $python -m pip install -r (Join-Path $hostRoot 'requirements.txt') 'pyinstaller==6.20.0'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$dist = Join-Path $projectRoot 'dist'
$work = Join-Path $packRoot 'pyinstaller'
& $python -m PyInstaller --noconfirm --onefile --windowed --noupx `
    --hidden-import bleak.backends.winrt.client --hidden-import bleak.backends.winrt.scanner `
    --name $AppName --distpath $dist --workpath $work --specpath $packRoot `
    --add-data "${vendor}:openocd" --add-data "$(Join-Path $hostRoot 'wireless.cfg'):." `
    --add-data "$(Join-Path $hostRoot 'third-party.md'):." (Join-Path $hostRoot 'desktop.py')
exit $LASTEXITCODE
