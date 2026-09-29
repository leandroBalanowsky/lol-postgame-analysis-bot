"""Mantiene bot.py corriendo: si se cierra por cualquier motivo, lo vuelve a arrancar.

El Programador de tareas de Windows no reinicia un programa que se cierra con error,
así que la tarea ejecuta este supervisor y él se encarga de los reinicios.
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
BOT = BASE_DIR / "bot.py"
LOG = BASE_DIR / "supervisor.log"


def anotar(mensaje: str):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {mensaje}\n")


espera = 15
while True:
    inicio = time.monotonic()
    anotar("Arrancando el bot")
    codigo = subprocess.run([sys.executable, str(BOT)], cwd=BASE_DIR).returncode
    duro = time.monotonic() - inicio
    # Si se cae enseguida (ej: token inválido), espera cada vez más, hasta 5 minutos
    espera = 30 if duro > 300 else min(espera * 2, 300)
    anotar(f"El bot se cerró (código {codigo}, duró {duro:.0f}s). Reinicio en {espera}s")
    time.sleep(espera)
