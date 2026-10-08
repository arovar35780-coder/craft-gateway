@echo off
rem Starts effectcraft with its control channel on port 9877 (the default ports of the apps differ; VectorCraft is moved off 7979, which DesignCraft uses).
start "" "%~dp0effectcraft\effectcraft.exe" --control 9877 %*
