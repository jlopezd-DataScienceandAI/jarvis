# Arranca J.A.R.V.I.S. al iniciar sesión en Windows.
#
#   Instalar:     powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   Desinstalar:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
#
# Crea un acceso directo en la carpeta Inicio del usuario. No necesita permisos de administrador.

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'JARVIS.lnk'

if ($Remove) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut
        Write-Host "Autoarranque eliminado."
    } else {
        Write-Host "No había autoarranque instalado."
    }
    return
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { throw "No encuentro 'uv' en el PATH." }

$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
$link.TargetPath = $uv
$link.Arguments = 'run jarvis'
$link.WorkingDirectory = $project
$link.WindowStyle = 7   # minimizada: la ventana queda en la barra de tareas con el registro
$link.Description = 'Asistente de voz J.A.R.V.I.S.'
$link.Save()

Write-Host "Listo: J.A.R.V.I.S. arrancará al iniciar sesión."
Write-Host "Acceso directo: $shortcut"
