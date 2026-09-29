# Maneja el inicio automatico del bot en Windows (Programador de tareas).
# Uso: bot.ps1 activar | quitar | iniciar | detener | estado
param([Parameter(Mandatory)][ValidateSet("activar", "quitar", "iniciar", "detener", "estado")][string]$Accion)

$ErrorActionPreference = "Stop"
$Tarea = "Bot Analisis"
$BotDir = Split-Path -Parent $PSScriptRoot
# El supervisor arranca bot.py y lo reinicia si se cierra
$Supervisor = Join-Path $BotDir "supervisor.py"
$Script = Join-Path $BotDir "bot.py"

# Procesos de Python de esta carpeta (el supervisor y el bot); así no toca otros bots de la PC
function Procesos-De($ruta) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($ruta) }
}

function Detener-Bot {
    Stop-ScheduledTask -TaskName $Tarea -ErrorAction SilentlyContinue
    # Primero el supervisor, para que no vuelva a arrancar el bot al cerrarlo
    foreach ($ruta in $Supervisor, $Script) {
        Procesos-De $ruta | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    }
}

function Bot-Corriendo {
    [bool](Procesos-De $Script)
}

switch ($Accion) {
    "activar" {
        $programa = New-ScheduledTaskAction -Execute "$BotDir\.venv\Scripts\pythonw.exe" -Argument "`"$Supervisor`"" -WorkingDirectory $BotDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $config = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $Tarea -Action $programa -Trigger $trigger -Settings $config `
            -Description "Bot de Discord que analiza partidas de LoL" -Force | Out-Null
        Detener-Bot
        Start-ScheduledTask -TaskName $Tarea
        Write-Host "Inicio automatico ACTIVADO y bot iniciado."
    }
    "quitar" {
        Detener-Bot
        Unregister-ScheduledTask -TaskName $Tarea -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Inicio automatico QUITADO y bot detenido."
    }
    "iniciar" {
        if (-not (Get-ScheduledTask -TaskName $Tarea -ErrorAction SilentlyContinue)) {
            Write-Host "El inicio automatico no esta activado. Usa activar-inicio-automatico.bat"; exit 1
        }
        Start-ScheduledTask -TaskName $Tarea
        Write-Host "Bot iniciado."
    }
    "detener" {
        Detener-Bot
        Write-Host "Bot detenido (volvera a arrancar la proxima vez que inicies sesion)."
    }
    "estado" {
        $t = Get-ScheduledTask -TaskName $Tarea -ErrorAction SilentlyContinue
        Write-Host ("Inicio automatico: " + $(if ($t) { "activado" } else { "desactivado" }))
        Write-Host ("Bot corriendo:     " + $(if (Bot-Corriendo) { "si" } else { "no" }))
    }
}
