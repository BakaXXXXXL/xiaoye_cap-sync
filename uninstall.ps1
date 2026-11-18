$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\cpa-sync"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$entries = @($userPath -split ";" | Where-Object { $_ -and $_ -ne $installDir })
[Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")
Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Removed cpa-sync. Its configuration and cached capabilities were preserved."
