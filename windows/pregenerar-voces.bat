@echo off
rem Genera de antemano los audios de voz de todos los anuncios (usa la placa de video).
rem Se puede cortar con Ctrl+C y retomar despues: saltea lo que ya esta generado.
cd /d "%~dp0.."
".venv\Scripts\python.exe" pregenerar_voces.py
pause
