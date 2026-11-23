@echo off
rem Removes the FPV Sync panel (and its analysis cache) from Premiere Pro.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d = Join-Path $env:APPDATA 'Adobe\CEP\extensions\fpvsync';" ^
  "if (Test-Path $d) {" ^
  "  $i = Get-Item $d -Force;" ^
  "  if ($i.Attributes -match 'ReparsePoint') { $i.Delete() } else { Remove-Item $d -Recurse -Force };" ^
  "  'FPV Sync removed. Restart Premiere Pro.'" ^
  "} else { 'FPV Sync is not installed.' }"
echo.
pause
