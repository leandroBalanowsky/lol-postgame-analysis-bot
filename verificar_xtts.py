"""Transcribe audios con Whisper y los compara con el texto que tenían que decir.

Se ejecuta con el mismo entorno de Python que XTTS (necesita openai-whisper). Recibe un
JSON con [{"texto": ..., "salida": ..., "pista": ...}, ...] ("pista" opcional: nombres
propios que aparecen en el audio) y escribe una línea JSON por audio con lo
que entendió Whisper y el parecido con el texto esperado (0 a 1).

Uso: python verificar_xtts.py tareas.json [--modelo small]
"""
import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

import librosa
import soundfile as sf
import torch
import whisper


def normalizar(texto: str) -> str:
    """Solo las letras, en minúscula y sin tildes, signos ni espacios.

    Se compara letra por letra y sin espacios porque Whisper a veces une o separa palabras
    que suenan igual ("con Fiora" -> "Confiora", "Adroco" -> "a Droco").
    """
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return "".join(re.findall(r"[a-z0-9]", texto))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("tareas", type=Path)
    p.add_argument("--modelo", default="small")
    args = p.parse_args()
    tareas = json.loads(args.tareas.read_text(encoding="utf-8"))

    dispositivo = "cuda" if torch.cuda.is_available() else "cpu"
    modelo = whisper.load_model(args.modelo, device=dispositivo)

    for tarea in tareas:
        audio, sr = sf.read(tarea["salida"], dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        duracion = len(audio) / sr
        audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
        # fp16=False: las GTX 16xx dan resultados erróneos con media precisión.
        # La pista (nombres propios de la frase) ayuda a que Whisper los escriba bien.
        oido = modelo.transcribe(audio, language="es", fp16=False, condition_on_previous_text=False,
                                 initial_prompt=tarea.get("pista") or None)["text"].strip()
        parecido = difflib.SequenceMatcher(None, normalizar(tarea["texto"]), normalizar(oido)).ratio()
        print(json.dumps({"texto": tarea["texto"], "salida": tarea["salida"], "oido": oido,
                          "parecido": round(parecido, 3), "duracion": round(duracion, 1)},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
