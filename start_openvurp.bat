@echo off
cd /d %~dp0
echo Avvio openvurp con Watcher (auto-restart abilitato)...
python watcher.py %*
