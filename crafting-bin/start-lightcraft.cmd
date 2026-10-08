@echo off
rem Starts lightcraft with its control channel on port 7980 (the default ports of the apps differ; VectorCraft is moved off 7979, which DesignCraft uses).
start "" "%~dp0lightcraft\lightcraft.exe" --control 7980 %*
