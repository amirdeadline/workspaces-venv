# Machine-local hooks live in $env:USERPROFILE\.workspaces
# Run: python <workspaces>\scripts\install.py --path <workspaces-root>
$local = Join-Path $env:USERPROFILE '.workspaces\profile.ps1'
if (Test-Path -LiteralPath $local) { . $local }
