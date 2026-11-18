param(
    [string]$Version = "latest"
)

$ErrorActionPreference = "Stop"
$repo = "BakaXXXXXL/xiaoye_cap-sync"
$asset = "cpa-sync-windows-x64.exe"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\cpa-sync"

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "This release currently supports Windows x64 only."
}

$release = if ($Version -eq "latest") {
    "https://github.com/$repo/releases/latest/download"
} else {
    "https://github.com/$repo/releases/download/$Version"
}

$temp = Join-Path $env:TEMP ("cpa-sync-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null

try {
    $exe = Join-Path $temp $asset
    $hashFile = Join-Path $temp "$asset.sha256"
    Invoke-WebRequest -UseBasicParsing -Uri "$release/$asset" -OutFile $exe
    Invoke-WebRequest -UseBasicParsing -Uri "$release/$asset.sha256" -OutFile $hashFile
    $expected = ((Get-Content -LiteralPath $hashFile -Raw).Trim() -split "\s+")[0].ToUpperInvariant()
    $actual = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    if ($actual -ne $expected) {
        throw "SHA-256 verification failed; nothing was installed."
    }

    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $installDir "cpa-sync.exe") -Force
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    if ($entries -notcontains $installDir) {
        [Environment]::SetEnvironmentVariable("Path", ((@($installDir) + $entries) -join ";"), "User")
    }
    if (($env:Path -split ";") -notcontains $installDir) {
        $env:Path = "$installDir;$env:Path"
    }
    & (Join-Path $installDir "cpa-sync.exe") --version
    Write-Host "Installed cpa-sync to $installDir"
    Write-Host "Open a new terminal if cpa-sync is not found."
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}
