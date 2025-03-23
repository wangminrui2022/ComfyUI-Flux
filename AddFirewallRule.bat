@echo off
setlocal

REM Ask user for the port number
:: set PORT=43568
set /p PORT=Enter the port number to add to firewall rules: 

REM Check if running as administrator
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Please run this script as an administrator.
    pause
    exit /b
)

REM Add inbound rule
echo Adding inbound rule for port %PORT%...
netsh advfirewall firewall add rule name="Allow Inbound Port %PORT%" protocol=TCP dir=in localport=%PORT% action=allow
if %errorLevel% neq 0 (
    echo Failed to add inbound rule for port %PORT%.
) else (
    echo Inbound rule for port %PORT% has been added.
)

REM Add outbound rule
echo Adding outbound rule for port %PORT%...
netsh advfirewall firewall add rule name="Allow Outbound Port %PORT%" protocol=TCP dir=out localport=%PORT% action=allow
if %errorLevel% neq 0 (
    echo Failed to add outbound rule for port %PORT%.
) else (
    echo Outbound rule for port %PORT% has been added.
)

REM Verify inbound rule
echo Verifying inbound rule for port %PORT%...
netsh advfirewall firewall show rule name="Allow Inbound Port %PORT%" | findstr /C:"LocalPort" /C:"Action"
if %errorLevel% neq 0 (
    echo Inbound rule for port %PORT% not found.
) else (
    echo Inbound rule for port %PORT% verified.
)

REM Verify outbound rule
echo Verifying outbound rule for port %PORT%...
netsh advfirewall firewall show rule name="Allow Outbound Port %PORT%" | findstr /C:"LocalPort" /C:"Action"
if %errorLevel% neq 0 (
    echo Outbound rule for port %PORT% not found.
) else (
    echo Outbound rule for port %PORT% verified.
)

REM Open the Windows Firewall with Advanced Security window
start wf.msc

pause
endlocal
exit /b
