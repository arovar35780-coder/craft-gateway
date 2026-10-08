@echo off
rem Starts PrintCraft (its --control option takes a token file, not a port, so it is started without one).
start "" "%~dp0printcraft\printcraft.exe" %*
