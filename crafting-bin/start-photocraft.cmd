@echo off
rem Starts photocraft with its control channel on port 9878 (the default ports of the apps differ; VectorCraft is moved off 7979, which DesignCraft uses).
start "" "%~dp0photocraft\photocraft.exe" --control 9878 %*
