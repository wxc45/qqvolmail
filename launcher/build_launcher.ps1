param(
    [string]$PortableDir = "",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$launcherDir = $PSScriptRoot
$appDir = Split-Path -Parent $launcherDir

if ([string]::IsNullOrWhiteSpace($PortableDir)) {
    $PortableDir = Join-Path $appDir "dist_portable\QQ分卷邮递"
}
if ([string]::IsNullOrWhiteSpace($Output)) {
    $Output = Join-Path $appDir "dist\QQ分卷邮递.exe"
}

if (-not (Test-Path -LiteralPath $PortableDir)) {
    throw "Portable directory not found: $PortableDir"
}

$payload = Join-Path $launcherDir "payload.zip"
if (Test-Path -LiteralPath $payload) {
    Remove-Item -LiteralPath $payload -Force
}

Write-Host "Packing portable payload..."
Compress-Archive -LiteralPath $PortableDir -DestinationPath $payload -CompressionLevel Optimal

$cscCandidates = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) {
    throw "C# compiler (csc.exe) not found. .NET Framework 4.x is required."
}

$icon = Join-Path $appDir "assets\app.ico"
$source = Join-Path $launcherDir "QQVolMailLauncher.cs"
$manifest = Join-Path $launcherDir "launcher.manifest"
$outputDir = Split-Path -Parent $Output
if (-not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

Write-Host "Compiling launcher..."
$cscArgs = @(
    "/nologo",
    "/target:winexe",
    "/optimize+",
    "/out:$Output",
    "/win32icon:$icon",
    "/win32manifest:$manifest",
    "/resource:$payload,QQVolMail.App.zip",
    "/reference:System.Windows.Forms.dll",
    "/reference:System.IO.Compression.dll",
    "/reference:System.IO.Compression.FileSystem.dll",
    "$source"
)
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) {
    throw "csc.exe failed with exit code $LASTEXITCODE"
}

Remove-Item -LiteralPath $payload -Force
Get-Item -LiteralPath $Output | Select-Object FullName, Length
