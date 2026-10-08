@echo off
rem Starts filmcraft with its control channel on port 9876 (the default ports of the apps differ; VectorCraft is moved off 7979, which DesignCraft uses).
start "" "%~dp0filmcraft\filmcraft.exe" --control 9876 %*
