# Start Cleam without installing anything:
#
#   irm https://raw.githubusercontent.com/MONNNNNNNNNNN/cleam/main/run.ps1 | iex
#
# Downloads the window from the latest GitHub release (one .exe, no Python
# needed) into %LOCALAPPDATA%\Cleam\portable, checks it against the SHA-256
# GitHub publishes for it, and starts it. Later runs download again only when
# a newer release exists, and fall back to the copy already there when offline.
# Run it from an elevated PowerShell to include the targets that need admin.
#
# Everything is inside & { } so that `iex` does not leave $ErrorActionPreference
# and friends changed in the caller's session.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell's progress bar makes downloads crawl
    $repo = 'MONNNNNNNNNNN/cleam'
    $file = 'cleam-gui-windows-x64.exe'
    $dir = Join-Path $env:LOCALAPPDATA 'Cleam\portable'
    $stamp = Join-Path $dir 'release.txt'  # the tag of the copy to start
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # One file per release: a Cleam that is still open locks its .exe, and
    # replacing that file in place failed exactly when an update arrived.
    function Get-Exe([string]$tag) { Join-Path $dir "cleam-gui-$tag.exe" }
    $have = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }

    try {
        $release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'cleam-run' }
        $asset = $release.assets | Where-Object { $_.name -eq $file } | Select-Object -First 1
        if (-not $asset) { throw "release $($release.tag_name) has no $file" }
        $exe = Get-Exe $release.tag_name
        if (-not (Test-Path $exe)) {
            Write-Host "Downloading Cleam $($release.tag_name)..."
            $part = "$exe.part"
            Invoke-WebRequest $asset.browser_download_url -OutFile $part -UseBasicParsing
            # GitHub records a digest for every asset uploaded since mid-2025.
            if ("$($asset.digest)" -like 'sha256:*') {
                $got = (Get-FileHash $part -Algorithm SHA256).Hash
                if ($got -ne "$($asset.digest)".Substring(7)) { Remove-Item $part -Force; throw "the download does not match its published SHA-256" }
            } else {
                Write-Warning "GitHub published no checksum for this file, so it was not verified."
            }
            Move-Item -Force $part $exe
        }
        Set-Content -Path $stamp -Value $release.tag_name
        $have = $release.tag_name
        # Older copies, unless one is still running (then it stays locked, and goes next time).
        Get-ChildItem $dir -Filter 'cleam-gui-*.exe' | Where-Object { $_.FullName -ne $exe } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch {
        if (-not $have -or -not (Test-Path (Get-Exe $have))) {
            Write-Host "Could not get Cleam: $_" -ForegroundColor Red
            return
        }
        Write-Warning "Could not check for a newer Cleam ($_). Starting $have, already downloaded."
    }
    Start-Process -FilePath (Get-Exe $have)
}
