# Build a clean, secret-free full Windows distributable bundle of ResearchRadar.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "1/4 Building React SPA (web/dist)..."
Push-Location web
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend build failed" }
Pop-Location

$Rev = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
$SrcModel = Join-Path $Root "data\models\bge-small-en-v1.5\$Rev"
$DstModel = Join-Path $Root "build\staged_models\bge-small-en-v1.5\$Rev"
if (Test-Path $DstModel) { Remove-Item -Recurse -Force $DstModel }
if (Test-Path $SrcModel) {
    Write-Host "2/4 Staging clean BGE weights (without .cache)..."
    New-Item -ItemType Directory -Force -Path $DstModel | Out-Null
    Get-ChildItem $SrcModel -Force | Where-Object { $_.Name -ne ".cache" } | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $DstModel $_.Name) -Recurse -Force
    }
} else {
    Write-Host "2/4 Warning: local BGE weights not found at $SrcModel; bundle will run in keyword mode until downloaded."
}

Write-Host "3/4 Running PyInstaller (ResearchRadar.spec)..."
.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --clean ResearchRadar.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$BundleDir = Join-Path $Root "dist\ResearchRadar"
if (-not (Test-Path (Join-Path $BundleDir "ResearchRadar.exe"))) {
    throw "ResearchRadar.exe was not produced"
}
# Ensure no personal .env or sqlite database leaked into dist\ResearchRadar
Get-ChildItem $BundleDir -Recurse -Force | Where-Object { $_.Name -in @(".env", "radar.sqlite3") } | Remove-Item -Force -ErrorAction SilentlyContinue

$ZipPath = Join-Path $Root "dist\ResearchRadar-win64-full.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Write-Host "4/4 Creating distributable zip archive at $ZipPath ..."
Compress-Archive -Path $BundleDir -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Done! Bundle dir: $BundleDir | Zip: $ZipPath"
