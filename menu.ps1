# Cleam's terminal menu, without installing anything:
#
#   irm https://raw.githubusercontent.com/MONNNNNNNNNNN/cleam/main/menu.ps1 | iex
#
# Same as run.ps1, but fetches the command-line build and runs it in this
# window: an ASCII menu with checkboxes for debloat, junk and the security
# check. Run it from an elevated PowerShell for machine-wide settings.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $repo = 'MONNNNNNNNNNN/cleam'
    $file = 'cleam-cli-windows-x64.exe'
    $dir = Join-Path $env:LOCALAPPDATA 'Cleam\portable'
    $stamp = Join-Path $dir 'cli-release.txt'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    function Get-Exe([string]$tag) { Join-Path $dir "cleam-$tag.exe" }
    $have = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }
    # Asking GitHub for the newest release costs 1-2 s in Windows PowerShell,
    # so a copy that was checked in the last 6 hours starts straight away.
    $fresh = $have -and (Test-Path (Get-Exe $have)) -and
        ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalHours -lt 6
    if ($fresh) { & (Get-Exe $have) menu; return }
    Write-Host "Checking for the newest Cleam..."
    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -TimeoutSec 5 -Headers @{ 'User-Agent' = 'cleam-menu' }
        $asset = $release.assets | Where-Object { $_.name -eq $file } | Select-Object -First 1
        if (-not $asset) { throw "release $($release.tag_name) has no $file" }
        $exe = Get-Exe $release.tag_name
        if (-not (Test-Path $exe)) {
            Write-Host "Downloading Cleam $($release.tag_name)..."
            $part = "$exe.part"
            Invoke-WebRequest $asset.browser_download_url -OutFile $part -UseBasicParsing
            if ("$($asset.digest)" -like 'sha256:*') {
                if ((Get-FileHash $part -Algorithm SHA256).Hash -ne "$($asset.digest)".Substring(7)) {
                    Remove-Item $part -Force; throw "the download does not match its published SHA-256"
                }
            } else {
                Write-Warning "GitHub published no checksum for this file, so it was not verified."
            }
            Move-Item -Force $part $exe
        }
        Set-Content -Path $stamp -Value $release.tag_name
        $have = $release.tag_name
        Get-ChildItem $dir -Filter 'cleam-v*.exe' | Where-Object { $_.FullName -ne $exe } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch {
        if (-not $have -or -not (Test-Path (Get-Exe $have))) {
            Write-Host "Could not get Cleam: $_" -ForegroundColor Red
            return
        }
        Write-Warning "Could not check for a newer Cleam ($_). Starting $have, already downloaded."
    }
    & (Get-Exe $have) menu  # in this window: the menu needs the console iex is running in
}
