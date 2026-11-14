# Installs the build queue on Windows: a venv with psutil for the server, gq for Git Bash and cmd/PowerShell,
# and a Task Scheduler task that starts the server at logon and restarts it if it dies.
# The Claude Code hook is added to ~/.claude/settings.json separately (see README).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$label = "com.massa.build-queue"
$data = Join-Path $HOME ".local\share\build-queue"
$bin = Join-Path $HOME ".local\bin"
$venv = Join-Path $data "venv"
$python = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python -ErrorAction Stop).Source }

# a running server holds the venv's python open, so stop it before the venv is rebuilt
Stop-ScheduledTask -TaskName $label -ErrorAction SilentlyContinue
$server = Join-Path $here "bq\server.py"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($server) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

New-Item -ItemType Directory -Force $data, $bin | Out-Null
& $python -m venv --clear $venv
if ($LASTEXITCODE) { throw "venv creation failed with $python" }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install -q --disable-pip-version-check "psutil>=6"
if ($LASTEXITCODE) { throw "pip install psutil failed" }

$gq = Join-Path $here "bq\gq.py"
Set-Content -Encoding ascii (Join-Path $bin "gq.cmd") "@`"$py`" `"$gq`" %*"
# Git Bash, where Claude Code runs its Bash tool: LF line endings, forward slashes
$sh = "#!/bin/sh`nexec `"$($py -replace '\\', '/')`" `"$($gq -replace '\\', '/')`" `"`$@`"`n"
[IO.File]::WriteAllText((Join-Path $bin "gq"), $sh)

$action = New-ScheduledTaskAction -Execute (Join-Path $venv "Scripts\pythonw.exe") `
    -Argument "`"$server`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $label -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $label

if (-not (($env:Path -split ";") -contains $bin)) {
    Write-Host "add $bin to your user PATH so Git Bash and cmd find gq"
}
Write-Host "installed: http://127.0.0.1:8787 (log: $(Join-Path $data 'server.log'))"
