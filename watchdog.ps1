# Angel AI Service Watchdog v2 (2026-04-28: schtasks-based, longer verify)
# Checks if port 8001 is alive, triggers AngelAI_Start schtasks if not.
$port = 8001
$logFile = 'E:\angel-ai-service\watchdog.log'

function Write-Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logFile -Value "[$ts] $msg"
}

try {
    $conn = Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue
    if ($conn.TcpTestSucceeded) {
        # Service is running, do nothing
        exit 0
    }
} catch {}

Write-Log "Port $port not responding. Triggering AngelAI_Start schtasks..."

# Kill any zombie python on :8001 before restart
$zombie = netstat -ano | findstr ":$port " | findstr LISTENING
if ($zombie) {
    $zPid = ($zombie -split '\s+')[-1]
    Write-Log "  killing zombie PID $zPid on :$port"
    Stop-Process -Id $zPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Use schtasks (the proven path that works manually) instead of inline Start-Process
$result = & schtasks.exe /Run /TN 'AngelAI_Start' 2>&1
Write-Log "schtasks /Run AngelAI_Start: $result"

# Wait long enough for uvicorn full startup (DB pool + key_store + imports take ~15-20s)
Start-Sleep -Seconds 30

try {
    $verify = Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue
    if ($verify.TcpTestSucceeded) {
        Write-Log "AI service recovered successfully on :$port."
    } else {
        Write-Log "WARNING: AI service still not responding 30s after schtasks /Run."
    }
} catch {
    Write-Log "WARNING: Could not verify AI service status: $($_.Exception.Message)"
}
