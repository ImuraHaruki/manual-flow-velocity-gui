param(
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultPythonExe = Join-Path $env:USERPROFILE 'anaconda3\envs\flow-velocity-gui\python.exe'
$releaseDir = Join-Path $projectDir 'release'
$packageDir = Join-Path $releaseDir 'ManualFlowVelocity-v1.0.0-win64'
$resolvedProjectDir = [IO.Path]::GetFullPath($projectDir).TrimEnd('\')
$resolvedReleaseDir = [IO.Path]::GetFullPath($releaseDir)

if (-not $resolvedReleaseDir.StartsWith($resolvedProjectDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe release path: $resolvedReleaseDir"
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = $defaultPythonExe
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "Python environment not found: $PythonExe"
}

Set-Location -LiteralPath $projectDir
$pyInstallerArguments = @(
    '-m', 'PyInstaller',
    '--noconfirm',
    '--clean',
    '--onefile',
    '--windowed',
    '--name', 'ManualFlowVelocity',
    '--version-file', 'version_info.txt'
)
$jpegDll = & $PythonExe -c "import sys; from pathlib import Path; p=Path(sys.prefix)/'Library'/'bin'/'jpeg8.dll'; print(p if p.exists() else '')"
if (-not [string]::IsNullOrWhiteSpace($jpegDll)) {
    $pyInstallerArguments += @('--add-binary', "$jpegDll;.")
}
$pyInstallerArguments += 'app.py'
& $PythonExe @pyInstallerArguments

if (Test-Path -LiteralPath $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
New-Item -ItemType Directory -Path $packageDir | Out-Null
Copy-Item -LiteralPath (Join-Path $projectDir 'dist\ManualFlowVelocity.exe') -Destination $packageDir
Copy-Item -LiteralPath (Join-Path $projectDir 'README.md') -Destination $packageDir

$zipPath = Join-Path $releaseDir 'ManualFlowVelocity-v1.0.0-win64.zip'
Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $zipPath -Force
Write-Host "Release created: $zipPath"
