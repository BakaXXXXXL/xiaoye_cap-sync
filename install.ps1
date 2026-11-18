param(
    [string]$Version = "latest"
)

$ErrorActionPreference = "Stop"
$previousProgressPreference = $ProgressPreference
$ProgressPreference = "SilentlyContinue"
$repo = "BakaXXXXXL/xiaoye_cap-sync"
$asset = "cpa-sync-windows-x64.exe"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\cpa-sync"
$previousSecurityProtocol = [Net.ServicePointManager]::SecurityProtocol
[Net.ServicePointManager]::SecurityProtocol = $previousSecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "This release currently supports Windows x64 only."
}

$releases = if ($Version -eq "latest") {
    @(
        "https://bakandarkfsg.com/cpa-sync/releases/latest",
        "https://github.com/$repo/releases/latest/download"
    )
} else {
    @(
        "https://bakandarkfsg.com/cpa-sync/releases/$Version",
        "https://github.com/$repo/releases/download/$Version"
    )
}

function Save-ReleaseAsset {
    param(
        [string]$Name,
        [string]$Destination
    )

    $failures = @()
    foreach ($release in $releases) {
        $uri = "$release/$Name"
        try {
            Write-Host "Downloading $uri"
            Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $Destination -TimeoutSec 60
            if (-not (Test-Path -LiteralPath $Destination) -or (Get-Item -LiteralPath $Destination).Length -eq 0) {
                throw "The downloaded file is empty."
            }
            return
        } catch {
            $failures += "${uri}: $($_.Exception.Message)"
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        }
    }
    throw "Unable to download ${Name}:$([Environment]::NewLine)$($failures -join [Environment]::NewLine)"
}

function Get-Sha256Hash {
    param([string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "")
        } finally {
            $algorithm.Dispose()
        }
    } finally {
        $stream.Dispose()
    }
}

$temp = Join-Path $env:TEMP ("cpa-sync-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null

try {
    $exe = Join-Path $temp $asset
    $hashFile = Join-Path $temp "$asset.sha256"
    Save-ReleaseAsset -Name $asset -Destination $exe
    Save-ReleaseAsset -Name "$asset.sha256" -Destination $hashFile
    $expected = ((Get-Content -LiteralPath $hashFile -Raw).Trim() -split "\s+")[0].ToUpperInvariant()
    $actual = Get-Sha256Hash -Path $exe
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
    $ProgressPreference = $previousProgressPreference
    [Net.ServicePointManager]::SecurityProtocol = $previousSecurityProtocol
}
