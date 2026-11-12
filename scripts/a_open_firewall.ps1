# OWNER: P1
# a_open_firewall.ps1: let other laptops reach the whiteboard server.
#
# Run in an ADMINISTRATOR PowerShell on the SERVER laptop:
#     powershell -ExecutionPolicy Bypass -File scripts\a_open_firewall.ps1 [-Port 5050]
#
# What it does:
#   1. Adds an inbound Windows Firewall rule that allows TCP <Port>
#      (default 5050) on every profile (Domain, Private, Public). A phone
#      hotspot is usually classified "Public", where Windows blocks all
#      inbound connections by default; that is the usual reason friends time
#      out even though the server is running and the IP is right.
#   2. Prints any existing Block rules that mention python.exe or
#      WhiteboardServer.exe. A Block rule beats an Allow rule, so one of
#      those would still stop the connection. (When you first ran python
#      the "Allow access?" pop-up may have been answered with Cancel, which
#      Windows stores as a Block rule.)
#
# Safe to run twice: an existing rule with the same name is replaced.
param([int]$Port = 5050)

$ruleName = "Whiteboard server TCP $Port"

# Adding firewall rules needs administrator rights.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This script needs an Administrator PowerShell." -ForegroundColor Red
    Write-Host "Start menu > type PowerShell > right-click > Run as administrator, then run it again."
    exit 1
}

# 1. The allow rule (replace an old one of the same name).
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
Write-Host "OK: inbound TCP $Port is now allowed (rule '$ruleName')." -ForegroundColor Green

# 2. Block rules that would still win against the allow rule.
Write-Host ""
Write-Host "Existing Block rules for python.exe / WhiteboardServer.exe:"
$found = $false
$blocks = Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True -ErrorAction SilentlyContinue
foreach ($rule in $blocks) {
    $app = ($rule | Get-NetFirewallApplicationFilter).Program
    if ($app -match 'python(w)?\.exe$' -or $app -match 'WhiteboardServer\.exe$') {
        $found = $true
        Write-Host ("  BLOCK: {0}   program: {1}" -f $rule.DisplayName, $app) -ForegroundColor Yellow
        Write-Host ("         remove with: Remove-NetFirewallRule -Name '{0}'" -f $rule.Name)
    }
}
if (-not $found) {
    Write-Host "  none found."
}
