"""Genera de antemano los audios XTTS de todos los anuncios posibles.

Por cada campeón: la presentación de cada jugador vinculado y la de "el jugador de X"
(sin vincular), en derrota y en victoria; más todos los remates. Saltea los que ya
están guardados, así se puede cortar y retomar, o volver a correr después de vincular
a alguien nuevo. Conviene correrlo cuando no se está jugando: usa la placa de video.

Uso: python pregenerar_voces.py [--limite N]   (N = generar solo los primeros N, para probar)
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import bot
from analisis import Jugador, Resultado, TotalesEquipo


def presentacion(vinculo: dict | None, campeon: str, gano: bool) -> str:
    """La presentación hablada, igual a la que armaría el bot en un anuncio real."""
    j = Jugador(puuid=vinculo["puuid"] if vinculo else "-", nombre=vinculo["riot_id"].split("#")[0] if vinculo else "-",
                campeon=campeon, posicion="", kills=0, deaths=0, assists=0,
                farm_min=0, dano_min=0, vision_min=0, kp=0)
    vacio = TotalesEquipo(0, 0, 0, 0, 0, 0)
    return bot.armar_mensaje(Resultado("-", "", 0, 0, 0, gano, [j], vacio, vacio), hablado=True)


async def cargar_campeones():
    await bot.riot.abrir()
    await bot.riot.cerrar()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limite", type=int, default=0)
    args = p.parse_args()

    xtts_python = bot.BASE_DIR / bot.XTTS_PYTHON
    referencia = bot.BASE_DIR / bot.XTTS_REFERENCIA
    if bot.VOZ_MOTOR != "xtts" or not xtts_python.is_file() or not referencia.is_file():
        sys.exit("Esto es solo para VOZ_MOTOR=xtts con XTTS_PYTHON y XTTS_REFERENCIA configurados en .env")

    asyncio.run(cargar_campeones())
    if not bot.riot.campeones:
        sys.exit("No se pudieron bajar los campeones de Data Dragon (¿sin internet o con el filtro web activo?)")

    vinculos = list(bot.datos["vinculos"].values())
    textos = set(bot.REMATES_PEOR + bot.REMATES_MEJOR)
    for campeon in bot.riot.campeones:
        for vinculo in vinculos + [None]:
            for gano in (False, True):
                textos.add(presentacion(vinculo, campeon, gano))

    pendientes = [{"texto": t, "salida": str(bot.ruta_xtts(t))} for t in sorted(textos)
                  if not bot.ruta_xtts(t).exists()]
    print(f"{len(bot.riot.campeones)} campeones · {len(vinculos)} vinculados · {len(textos)} audios en total"
          f" · {len(textos) - len(pendientes)} ya guardados · {len(pendientes)} por generar", flush=True)
    if args.limite:
        pendientes = pendientes[:args.limite]
    if not pendientes:
        return

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(pendientes, f, ensure_ascii=False)
    try:
        subprocess.run([str(xtts_python), "-W", "ignore", str(bot.BASE_DIR / "voz_xtts.py"),
                        "--lote", f.name, "--referencia", str(referencia),
                        "--temperatura", bot.XTTS_TEMPERATURA], check=False)
    finally:
        Path(f.name).unlink(missing_ok=True)
    faltan = sum(not Path(t["salida"]).exists() for t in pendientes)
    print("Listo." if not faltan else f"Terminó con {faltan} audios sin generar; se pueden reintentar corriéndolo de nuevo.")


if __name__ == "__main__":
    main()
