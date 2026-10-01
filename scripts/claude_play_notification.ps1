# Claude Code hook (litellm): play notification WAV (non-blocking). Stdin JSON is ignored.
param(
    [string]$WavPath = $env:LITELLM_NOTIFICATION_WAV
)

$ErrorActionPreference = 'SilentlyContinue'
if ([Console]::In) {
    try { [void][Console]::In.ReadToEnd() } catch {}
}

if (-not $WavPath) {
    $WavPath = 'C:\Windows\Media\litellm.wav'
}

if (-not (Test-Path -LiteralPath $WavPath)) {
    [Console]::Beep(880, 120)
    exit 0
}

try {
    $player = New-Object System.Media.SoundPlayer $WavPath
    $player.Play()
} catch {
    [Console]::Beep(880, 120)
}
exit 0
