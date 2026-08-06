@echo off
setlocal
set "SERVICE_NAME=%SERVICE_NAME%"
if "%SERVICE_NAME%"=="" set "SERVICE_NAME=AstrBot-MCC-Transfer"
set "NSSM_EXE=%NSSM_EXE%"
if "%NSSM_EXE%"=="" set "NSSM_EXE=nssm.exe"
"%NSSM_EXE%" start "%SERVICE_NAME%"
endlocal
