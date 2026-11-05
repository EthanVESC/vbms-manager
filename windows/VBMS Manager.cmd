@echo off
rem VBMS Manager for Windows: runs the app with the Python bundled next to this file.
rem Opens http://127.0.0.1:8642 in your browser. Close this window to stop it.
rem Options go after the name, e.g.  "VBMS Manager.cmd" --lan   (see README.md)
title VBMS Manager
"%~dp0runtime\python.exe" -I -B -u "%~dp0app\vbms_manager.py" %*
if errorlevel 1 pause
