"""Genera de antemano los audios XTTS de los anuncios más probables.

Por defecto: para cada jugador vinculado, sus N campeones con más maestría (20), en
derrota y en victoria; los genéricos de los sin vincular ("el random de top", uno por
posición); y todos los remates. Los demás campeones de los vinculados se generan en el
momento del anuncio. Con --todos genera todos los campeones para cada vinculado.

Saltea los que ya están guardados, así se puede cortar y retomar, o volver a correr
después de vincular a alguien nuevo. Conviene correrlo cuando no se está jugando: usa
la placa de video.

Uso: python pregenerar_voces.py [--mejores 20] [--todos] [--limite N]
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


def presentacion(vinculo: dict | None, campeon: str, gano: bool, posicion: str = "") -> str:
    """La presentación hablada, igual a la que armaría el bot en un anuncio real."""
    j = Jugador(puuid=vinculo["puuid"] if vinculo else "-", nombre=vinculo["riot_id"].split("#")[0] if vinculo else "-",
                campeon=campeon, posicion=posicion, kills=0, deaths=0, assists=0,
                farm_min=0, dano_min=0, vision_min=0, kp=0)
    vacio = TotalesEquipo(0, 0, 0, 0, 0, 0)
    return bot.armar_mensaje(Resultado("-", "", 0, 0, 0, gano, [j], vacio, vacio), hablado=True)


async def campeones_por_vinculo(vinculos: list[dict], mejores: int, todos: bool) -> dict[str, list[str]]:
    """Qué campeones generar para cada vinculado (clave: puuid)."""
    await bot.riot.abrir()
    try:
        if not bot.riot.campeones:
            return {}
        if todos:
            return {v["puuid"]: list(bot.riot.campeones) for v in vinculos}
        resultado = {}
        for v in vinculos:
            resultado[v["puuid"]] = await bot.riot.mejores_campeones(v["puuid"], mejores)
            nombres = ", ".join(bot.riot.nombre_campeon(c) for c in resultado[v["puuid"]])
            print(f"{v['riot_id']}: {nombres}", flush=True)
        return resultado
    finally:
        await bot.riot.cerrar()


INFORME = bot.BASE_DIR / "verificacion_voces.txt"
UMBRAL_DUDOSO = 0.85  # parecido entre lo esperado y lo que entendió Whisper


def verificar(xtts_python: Path, tareas: list[dict], limite: int):
    """Transcribe los audios con Whisper y deja un informe con los que no coinciden con su texto."""
    if limite:
        tareas = tareas[:limite]
    print(f"Verificando {len(tareas)} audios con Whisper...", flush=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(tareas, f, ensure_ascii=False)
    resultados = []
    try:
        proceso = subprocess.Popen([str(xtts_python), "-W", "ignore", str(bot.BASE_DIR / "verificar_xtts.py"), f.name],
                                   stdout=subprocess.PIPE, text=True, encoding="utf-8")
        for linea in proceso.stdout:
            r = json.loads(linea)
            resultados.append(r)
            marca = "  " if r["parecido"] >= UMBRAL_DUDOSO else "⚠️"
            print(f"{len(resultados)}/{len(tareas)} {marca} {r['parecido']:.2f} · {r['oido']}", flush=True)
        proceso.wait()
    finally:
        Path(f.name).unlink(missing_ok=True)

    resultados.sort(key=lambda r: r["parecido"])
    dudosos = [r for r in resultados if r["parecido"] < UMBRAL_DUDOSO]
    with INFORME.open("w", encoding="utf-8") as inf:
        inf.write(f"{len(resultados)} audios verificados · {len(dudosos)} dudosos (parecido < {UMBRAL_DUDOSO})\n")
        inf.write("Ordenados del menos parecido al más parecido.\n\n")
        for r in resultados:
            inf.write(f"{r['parecido']:.2f} · {r['duracion']}s · {Path(r['salida']).name}\n"
                      f"   esperado: {r['texto']}\n   entendido: {r['oido']}\n\n")
    print(f"\n{len(dudosos)} dudosos de {len(resultados)}. Informe completo: {INFORME}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mejores", type=int, default=20, help="campeones con más maestría por vinculado")
    p.add_argument("--todos", action="store_true", help="todos los campeones para cada vinculado")
    p.add_argument("--limite", type=int, default=0, help="generar solo los primeros N (para probar)")
    p.add_argument("--verificar", action="store_true",
                   help="en vez de generar, transcribir con Whisper los ya generados y marcar los dudosos")
    args = p.parse_args()

    xtts_python = bot.BASE_DIR / bot.XTTS_PYTHON
    referencia = bot.BASE_DIR / bot.XTTS_REFERENCIA
    if bot.VOZ_MOTOR != "xtts" or not xtts_python.is_file() or not referencia.is_file():
        sys.exit("Esto es solo para VOZ_MOTOR=xtts con XTTS_PYTHON y XTTS_REFERENCIA configurados en .env")

    vinculos = list(bot.datos["vinculos"].values())
    try:
        elegidos = asyncio.run(campeones_por_vinculo(vinculos, args.mejores, args.todos))
    except bot.RiotError as e:
        sys.exit(f"No se pudo consultar a Riot: {e}")
    if not bot.riot.campeones:
        sys.exit("No se pudieron bajar los campeones de Data Dragon (¿sin internet o con el filtro web activo?)")

    # texto -> pista para Whisper al verificar (los nombres propios que se dicen)
    textos = {r: "" for r in bot.REMATES_PEOR + bot.REMATES_MEJOR}
    for vinculo in vinculos:
        nombre = vinculo.get("pronunciacion") or vinculo["riot_id"].split("#")[0]
        for campeon in elegidos.get(vinculo["puuid"], []):
            for gano in (False, True):
                textos[presentacion(vinculo, campeon, gano)] = f"{nombre}, {bot.riot.nombre_campeon(campeon)}."
    # Sin vincular: uno por posición ("el random de top"), más el de ARAM (sin posición)
    for posicion in list(bot.POSICIONES_RANDOM) + [""]:
        for gano in (False, True):
            textos[presentacion(None, "-", gano, posicion)] = "random, top, jungla, mid, ADC, support."

    if args.verificar:
        verificar(xtts_python, [{"texto": t, "salida": str(bot.ruta_xtts(t)), "pista": textos[t]}
                                for t in sorted(textos) if bot.ruta_xtts(t).exists()], args.limite)
        return

    pendientes = [{"texto": t, "salida": str(bot.ruta_xtts(t))} for t in sorted(textos)
                  if not bot.ruta_xtts(t).exists()]
    print(f"{len(vinculos)} vinculados · {len(textos)} audios en total"
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
