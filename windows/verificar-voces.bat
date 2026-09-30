@echo off
rem Transcribe con Whisper los audios de voz ya generados y marca los que no dicen lo que tienen que decir.
rem El informe queda en verificacion_voces.txt
cd /d "%~dp0.."
".venv\Scripts\python.exe" pregenerar_voces.py --verificar
pause
