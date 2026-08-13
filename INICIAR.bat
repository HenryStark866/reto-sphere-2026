@echo off
REM Arranque en un clic. Delega en iniciar.ps1 para no pelear con el escapado
REM de PowerShell dentro de un .bat, y salta la politica de ejecucion sin
REM cambiarla en el sistema.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1"
if errorlevel 1 (
  echo.
  pause
)
