@echo off
rem Starts PdfCraft (formerly PrintCraft) without a control channel; the gateway starts its own with --control PORT.
start "" "%~dp0pdfcraft\pdfcraft.exe" %*
