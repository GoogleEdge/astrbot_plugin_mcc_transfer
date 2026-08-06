@echo off
setlocal
set "SERVICE_NAME=%SERVICE_NAME%"
if "%SERVICE_NAME%"=="" set "SERVICE_NAME=AstrBot-MCC-Transfer"
set "NSSM_EXE=%NSSM_EXE%"
if "%NSSM_EXE%"=="" set "NSSM_EXE=nssm.exe"
set "ASTRBOT_DIR=%ASTRBOT_DIR%"
if "%ASTRBOT_DIR%"=="" set "ASTRBOT_DIR=%~dp0..\.."
set "ASTRBOT_EXE=%ASTRBOT_EXE%"
if "%ASTRBOT_EXE%"=="" set "ASTRBOT_EXE=python"
set "ASTRBOT_ARGS=%ASTRBOT_ARGS%"
if "%ASTRBOT_ARGS%"=="" set "ASTRBOT_ARGS=-m astrbot"
set "LOG_DIR=%LOG_DIR%"
if "%LOG_DIR%"=="" set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

"%NSSM_EXE%" install "%SERVICE_NAME%" "%ASTRBOT_EXE%"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppParameters "%ASTRBOT_ARGS%"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppDirectory "%ASTRBOT_DIR%"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppStdout "%LOG_DIR%\astrbot.out.log"
"%NSSM_EXE%" set "%SERVICE_NAME%" AppStderr "%LOG_DIR%\astrbot.err.log"
"%NSSM_EXE%" set "%SERVICE_NAME%" Start SERVICE_AUTO_START
"%NSSM_EXE%" set "%SERVICE_NAME%" AppExit Default Restart
"%NSSM_EXE%" set "%SERVICE_NAME%" AppRestartDelay 5000
endlocal
