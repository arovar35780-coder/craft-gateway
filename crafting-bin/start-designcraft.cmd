@echo off
rem Starts designcraft with its control channel on port 7979 (used by the MCP bridge and build scripts).
start "" "%~dp0designcraft\designcraft.exe" --control 7979 %*
