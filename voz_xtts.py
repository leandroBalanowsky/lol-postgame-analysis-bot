"""Genera un audio con XTTS-v2 clonando una voz de referencia, y termina.

El bot lo ejecuta como un proceso aparte, con el entorno de Python donde está instalado
coqui-tts (XTTS_PYTHON en .env). Así el modelo solo ocupa la placa de video mientras genera
y el bot no necesita PyTorch.

Uso: python voz_xtts.py --texto "..." --salida audio.wav --referencia voz.mp3 [--temperatura 0.75]

La voz de referencia se analiza una sola vez: el resultado se guarda al lado del archivo
(<referencia>.latentes.pt) y se reutiliza mientras la referencia no cambie.
"""
import argparse
import os
import sys
from pathlib import Path

os.environ["COQUI_TOS_AGREED"] = "1"  # licencia del modelo: Coqui Public Model License (uso no comercial)

import numpy as np
import soundfile as sf
import torch
from TTS.api import TTS

SR = 24000  # frecuencia de muestreo de XTTS
FUNDIDO = 0.02  # segundos de fundido al inicio y al final, para que no suene cortado
SILENCIO_FINAL = 0.5


def latentes(modelo, referencia: Path):
    """Análisis de la voz de referencia, guardado en disco para no repetirlo."""
    cache = referencia.with_name(referencia.name + ".latentes.pt")
    if cache.exists() and cache.stat().st_mtime >= referencia.stat().st_mtime:
        datos = torch.load(cache, map_location=modelo.device)
        return datos["gpt"], datos["speaker"]
    gpt, speaker = modelo.get_conditioning_latents(audio_path=[str(referencia)])
    torch.save({"gpt": gpt, "speaker": speaker}, cache)
    return gpt, speaker


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--texto", required=True)
    p.add_argument("--salida", required=True, type=Path)
    p.add_argument("--referencia", required=True, type=Path)
    p.add_argument("--temperatura", type=float, default=0.75)
    p.add_argument("--idioma", default="es")
    args = p.parse_args()

    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    modelo = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False).to(dispositivo)
    modelo = modelo.synthesizer.tts_model
    gpt, speaker = latentes(modelo, args.referencia)

    wav = np.asarray(modelo.inference(args.texto, args.idioma, gpt, speaker, temperature=args.temperatura)["wav"],
                     dtype=np.float32)
    n = min(int(SR * FUNDIDO), len(wav) // 2)
    rampa = np.linspace(0, 1, n, dtype=np.float32)
    wav[:n] *= rampa
    wav[-n:] *= rampa[::-1]
    wav = np.concatenate([wav, np.zeros(int(SR * SILENCIO_FINAL), dtype=np.float32)])

    temporal = args.salida.with_suffix(".tmp.wav")
    sf.write(temporal, wav, SR)
    temporal.replace(args.salida)  # nunca queda un audio a medio escribir


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # el bot lee este mensaje del error estándar
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
