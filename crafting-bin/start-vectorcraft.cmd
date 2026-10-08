@echo off
rem Starts vectorcraft with its control channel on port 7981 (the default ports of the apps differ; VectorCraft is moved off 7979, which DesignCraft uses).
start "" "%~dp0vectorcraft\vectorcraft.exe" --control 7981 %*
