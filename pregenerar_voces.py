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


def presentacion(vinculo: dict | None, campeon: str, gano: bool, posicion: str = "") -> "bot.Frase":
    """La presentación hablada (texto y carpeta), igual a la que armaría el bot en un anuncio real."""
    j = Jugador(puuid=vinculo["puuid"] if vinculo else "-", nombre=vinculo["riot_id"].split("#")[0] if vinculo else "-",
                campeon=campeon, posicion=posicion, kills=0, deaths=0, assists=0,
                farm_min=0, dano_min=0, vision_min=0, kp=0)
    vacio = TotalesEquipo(0, 0, 0, 0, 0, 0)
    return bot.frase_presentacion(Resultado("-", "", 0, 0, 0, gano, [j], vacio, vacio))


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
# Un audio es dudoso si a lo que entendió Whisper le falta parte de la frase (cobertura baja)
# o le sobra texto (repeticiones o palabras inventadas). Los límites se eligieron mirando un
# lote real: dejan pasar los errores de Whisper en palabras cortas ("carreador" -> "cariada").
COBERTURA_MINIMA = 0.88
SOBRANTE_MAXIMO = 0.15
INTENTOS_CORREGIR = 3


def es_dudoso(r: dict) -> bool:
    return r["cobertura"] < COBERTURA_MINIMA or r["sobrante"] > SOBRANTE_MAXIMO


def escribir_lote(tareas: list[dict]) -> str:
    """Guarda las tareas en un JSON temporal para pasárselas a un script del entorno de XTTS."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(tareas, f, ensure_ascii=False)
    return f.name


def generar(xtts_python: Path, referencia: Path, tareas: list[dict]):
    """Genera los audios con XTTS (carga el modelo una sola vez para todo el lote)."""
    archivo = escribir_lote(tareas)
    try:
        subprocess.run([str(xtts_python), "-W", "ignore", str(bot.BASE_DIR / "voz_xtts.py"),
                        "--lote", archivo, "--referencia", str(referencia),
                        "--temperatura", bot.XTTS_TEMPERATURA], check=False)
    finally:
        Path(archivo).unlink(missing_ok=True)


def transcribir(xtts_python: Path, tareas: list[dict]) -> list[dict]:
    """Transcribe los audios con Whisper y devuelve cobertura y sobrante de cada uno."""
    print(f"Verificando {len(tareas)} audios con Whisper...", flush=True)
    archivo = escribir_lote(tareas)
    resultados = []
    try:
        proceso = subprocess.Popen([str(xtts_python), "-W", "ignore", str(bot.BASE_DIR / "verificar_xtts.py"), archivo],
                                   stdout=subprocess.PIPE, text=True, encoding="utf-8")
        for linea in proceso.stdout:
            r = json.loads(linea)
            resultados.append(r)
            marca = "⚠️" if es_dudoso(r) else "  "
            print(f"{len(resultados)}/{len(tareas)} {marca} cob {r['cobertura']:.2f} sob {r['sobrante']:.2f}"
                  f" · {r['oido']}", flush=True)
        proceso.wait()
    finally:
        Path(archivo).unlink(missing_ok=True)
    return resultados


def escribir_informe(resultados: list[dict]):
    resultados = sorted(resultados, key=lambda r: min(r["cobertura"], 1 - r["sobrante"]))
    dudosos = [r for r in resultados if es_dudoso(r)]
    with INFORME.open("w", encoding="utf-8") as inf:
        inf.write(f"{len(resultados)} audios verificados · {len(dudosos)} dudosos "
                  f"(cobertura < {COBERTURA_MINIMA} o sobrante > {SOBRANTE_MAXIMO})\n")
        inf.write("cobertura: parte de la frase que se escuchó (baja = faltan palabras)\n"
                  "sobrante: texto de más (alto = repeticiones o palabras inventadas)\n"
                  "Ordenados del más sospechoso al menos sospechoso.\n\n")
        for r in resultados:
            inf.write(f"{'⚠️ ' if es_dudoso(r) else ''}cob {r['cobertura']:.2f} · sob {r['sobrante']:.2f}"
                      f" · {r['duracion']}s · {Path(r['salida']).name}\n"
                      f"   esperado: {r['texto']}\n   entendido: {r['oido']}\n\n")
    print(f"\n{len(dudosos)} dudosos de {len(resultados)}. Informe completo: {INFORME}")
    return dudosos


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mejores", type=int, default=20, help="campeones con más maestría por vinculado")
    p.add_argument("--todos", action="store_true", help="todos los campeones para cada vinculado")
    p.add_argument("--limite", type=int, default=0, help="procesar solo los primeros N (para probar)")
    p.add_argument("--verificar", action="store_true",
                   help="en vez de generar, transcribir con Whisper los ya generados y marcar los dudosos")
    p.add_argument("--corregir", action="store_true",
                   help=f"con --verificar: regenerar los dudosos y volver a verificarlos (hasta {INTENTOS_CORREGIR} veces)")
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

    # frase -> pista para Whisper al verificar (los nombres propios que se dicen)
    textos = {bot.frase_remate(r, gano): "" for gano, remates in ((False, bot.REMATES_PEOR),
                                                                    (True, bot.REMATES_MEJOR)) for r in remates}
    for vinculo in vinculos:
        nombre = vinculo.get("pronunciacion") or vinculo["riot_id"].split("#")[0]
        for campeon in elegidos.get(vinculo["puuid"], []):
            for gano in (False, True):
                textos[presentacion(vinculo, campeon, gano)] = f"{nombre}, {bot.riot.nombre_campeon(campeon)}."
    # Sin vincular: uno por posición ("el random de top"), más el de ARAM (sin posición)
    for posicion in list(bot.POSICIONES_RANDOM) + [""]:
        for gano in (False, True):
            textos[presentacion(None, "-", gano, posicion)] = "random, top, jungla, mid, ADC, support."
    tareas = [{"texto": f.texto, "salida": str(bot.ruta_xtts(f)), "pista": pista}
              for f, pista in sorted(textos.items(), key=lambda x: (x[0].carpeta, x[0].nombre))]

    if args.verificar:
        guardadas = [t for t in tareas if Path(t["salida"]).exists()]
        if args.limite:
            guardadas = guardadas[:args.limite]
        resultados = {r["salida"]: r for r in transcribir(xtts_python, guardadas)}
        dudosos = escribir_informe(list(resultados.values()))
        for intento in range(1, INTENTOS_CORREGIR + 1):
            if not args.corregir or not dudosos:
                break
            print(f"\n== Corrección {intento}/{INTENTOS_CORREGIR}: regenerando {len(dudosos)} audios", flush=True)
            for r in dudosos:
                Path(r["salida"]).unlink(missing_ok=True)
            rehacer = [t for t in guardadas if t["salida"] in {r["salida"] for r in dudosos}]
            generar(xtts_python, referencia, rehacer)
            resultados.update({r["salida"]: r for r in transcribir(xtts_python, rehacer)})
            dudosos = escribir_informe(list(resultados.values()))
        return

    pendientes = [t for t in tareas if not Path(t["salida"]).exists()]
    print(f"{len(vinculos)} vinculados · {len(tareas)} audios en total"
          f" · {len(tareas) - len(pendientes)} ya guardados · {len(pendientes)} por generar", flush=True)
    if args.limite:
        pendientes = pendientes[:args.limite]
    if not pendientes:
        return
    generar(xtts_python, referencia, pendientes)
    faltan = sum(not Path(t["salida"]).exists() for t in pendientes)
    print("Listo." if not faltan else f"Terminó con {faltan} audios sin generar; se pueden reintentar corriéndolo de nuevo.")


if __name__ == "__main__":
    main()
