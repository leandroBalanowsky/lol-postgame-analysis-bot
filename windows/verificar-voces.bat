@echo off
rem Transcribe con Whisper los audios de voz ya generados, regenera los que salieron mal
rem (palabras que faltan o repeticiones) y los vuelve a verificar. Informe en verificacion_voces.txt
cd /d "%~dp0.."
".venv\Scripts\python.exe" pregenerar_voces.py --verificar --corregir
pause
