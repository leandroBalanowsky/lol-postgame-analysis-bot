"""BOT ANALISIS: cuando termina una partida de LoL de alguien conectado a voz, anuncia al peor
de su equipo si perdieron, o al mejor si ganaron."""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
import edge_tts
import imageio_ffmpeg
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from analisis import Jugador, Resultado, TotalesEquipo, analizar
from riot import ClaveInvalida, Riot, RiotError, SinConexion

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
# Canal de texto para el resumen; vacío = el chat del canal de voz donde están
CANAL_TEXTO_ID = int(os.getenv("CANAL_TEXTO_ID") or 0)
# Derrota: se anuncia al peor del equipo. Victoria: al mejor.
# {jugador} es "Invocador con Campeón" si está vinculado, o "el random de <posición>" si no.
# {jugador_enfasis} es lo mismo con el nombre entre "¡!" ("¡Invocador! con Campeón"), para la voz.
MENSAJE_PEOR = os.getenv(
    "MENSAJE_PEOR",
    "El peor del equipo fue {jugador}: {kills} kills, {muertes} muertes y {asistencias} asistencias",
)
MENSAJE_MEJOR = os.getenv(
    "MENSAJE_MEJOR",
    "El mejor del equipo fue {jugador}: {kills} kills, {muertes} muertes y {asistencias} asistencias",
)
# Remates: frases que se agregan al final, elegidas al azar. Varias separadas por "|".
# Se generan como un audio aparte, así cada remate se genera una sola vez y sirve para todos.
REMATES_PEOR = [r.strip() for r in os.getenv("REMATES_PEOR", "").split("|") if r.strip()]
REMATES_MEJOR = [r.strip() for r in os.getenv("REMATES_MEJOR", "").split("|") if r.strip()]
VOZ = os.getenv("VOZ", "es-AR-TomasNeural")
VELOCIDAD = os.getenv("VELOCIDAD", "+0%")
TONO = os.getenv("TONO", "+0Hz")
# Motor de voz: "edge" (edge-tts, en la nube) o "xtts" (XTTS-v2 en la PC, clonando una voz).
# Con xtts, si falla o tarda demasiado, se usa edge-tts para ese anuncio.
VOZ_MOTOR = os.getenv("VOZ_MOTOR", "edge").strip().lower()
XTTS_PYTHON = os.getenv("XTTS_PYTHON", "").strip()  # python.exe del entorno con coqui-tts
XTTS_REFERENCIA = os.getenv("XTTS_REFERENCIA", "").strip()  # audio de la voz a clonar
XTTS_TEMPERATURA = os.getenv("XTTS_TEMPERATURA", "0.75").strip()
XTTS_TIMEOUT = int(os.getenv("XTTS_TIMEOUT_SEGUNDOS") or 180)
# Cómo se arma la voz con XTTS:
#   enteras: la presentación entera en un audio ("El carreador del equipo fue ¡Adroco! con Swain.")
#   partes: inicio + nombre + campeón en audios separados que se unen al anunciar. Con muy pocos
#           audios (~190) se cubre cualquier jugador con cualquier campeón.
VOZ_MODO = os.getenv("VOZ_MODO", "enteras").strip().lower()
PAUSA_ANUNCIO = float(os.getenv("PAUSA_ANUNCIO") or 0.8)  # entre "…del equipo fue" y el nombre
PAUSA_PARTES = float(os.getenv("PAUSA_PARTES") or 0.15)  # entre el nombre y "con Campeón"
PAUSA_REMATE = float(os.getenv("PAUSA_REMATE") or 0.5)  # antes del remate
NOMBRE_GANANCIA = float(os.getenv("NOMBRE_GANANCIA") or 1.3)  # volumen del nombre respecto del resto
INICIO_TEMPERATURA = os.getenv("INICIO_TEMPERATURA", "0.85").strip()  # más expresividad en "El carreador…fue"
REMATE_TEMPERATURA = os.getenv("REMATE_TEMPERATURA", "0.85").strip()  # y en el remate
NOMBRE_SUFIJO = os.getenv("NOMBRE_SUFIJO", "").strip()  # se agrega al nombre en la voz: "kun" -> "¡¡Adroco kun!!"
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS") or 90)  # cada cuánto revisa partidas nuevas
# Partidas que terminaron hace más que esto no se anuncian (ej: el bot estuvo apagado)
MAX_ANTIGUEDAD_MIN = int(os.getenv("MAX_ANTIGUEDAD_MIN") or 20)
# Rol para el peor vinculado de la última derrota; lo conserva hasta que otro vinculado salga peor.
# Vacío = sin rol. Si no existe en el servidor, el bot lo crea (necesita el permiso Gestionar roles).
ROL_PEOR = os.getenv("ROL_PEOR", "El más manco").strip()

BASE_DIR = Path(__file__).parent
DATOS = BASE_DIR / "datos.json"
CACHE_DIR = BASE_DIR / "audios"
CACHE_DIR.mkdir(exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

handlers = [RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
if sys.stderr:  # con pythonw (sin consola) no hay stderr
    handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.voice_states = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
riot = Riot(RIOT_API_KEY or "")

# Un lock por servidor para no pisar anuncios de voz
locks = {}


# ---------------------------------------------------------------- datos guardados

def cargar_datos() -> dict:
    if DATOS.exists():
        return json.loads(DATOS.read_text(encoding="utf-8"))
    return {"vinculos": {}, "anunciadas": []}


def guardar_datos():
    temporal = DATOS.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    temporal.replace(DATOS)


datos = cargar_datos()  # {"vinculos": {discord_id: {riot_id, puuid, ultima}}, "anunciadas": [...]}


def vinculo_de_puuid(puuid: str) -> str | None:
    """Discord ID vinculado a esa cuenta de Riot, si hay."""
    return next((did for did, v in datos["vinculos"].items() if v["puuid"] == puuid), None)


# ---------------------------------------------------------------- voz

def sintetizar(texto: str, destino: Path):
    """Llama a edge-tts en su propio hilo y event loop: si se cuelga, no traba al bot."""
    temporal = destino.with_suffix(".tmp")
    asyncio.run(edge_tts.Communicate(texto, VOZ, rate=VELOCIDAD, pitch=TONO).save(str(temporal)))
    temporal.replace(destino)


async def generar_audio_edge(texto: str) -> Path:
    clave = f"{VOZ}|{VELOCIDAD}|{TONO}|{texto}"
    ruta = CACHE_DIR / "_edge" / (hashlib.md5(clave.encode()).hexdigest() + ".mp3")
    if not ruta.exists():
        ruta.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.wait_for(asyncio.to_thread(sintetizar, texto, ruta), timeout=20)
    return ruta


xtts_lock = asyncio.Lock()  # una sola generación a la vez: la placa de video no da para dos


@dataclass(frozen=True)
class Frase:
    """Un audio a decir: el texto, y la carpeta y el nombre legibles con que se guarda.

    Ej: Frase("El más manco del equipo fue ¡Churlen! con Ahri.", "Churlenscuincle", "Ahri - derrota")
    se guarda en audios/Churlenscuincle/Ahri - derrota - 3fa2c1.wav
    """
    texto: str
    carpeta: str = "_otros"  # puede tener subcarpetas: "_partes/nombres"
    nombre: str = ""
    recortar: bool = False  # sin silencio al principio ni al final (para unirla con otras partes)
    ganancia: float = 1.0  # volumen (ej: 1.3 para el nombre, con más énfasis)
    temperatura: str | None = None  # si no se indica, XTTS_TEMPERATURA (más alta = más expresiva)


def _nombre_archivo(texto: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", texto).strip()[:60] or "audio"


def ruta_xtts(frase: Frase) -> Path:
    """Dónde se guarda el audio XTTS de una frase.

    El código al final del nombre sale del texto, la voz y la temperatura: si cambia
    cualquiera de ellos, el audio guardado deja de usarse y se genera uno nuevo.
    """
    referencia = (BASE_DIR / XTTS_REFERENCIA).resolve()
    clave = f"xtts|{referencia.name}|{referencia.stat().st_mtime}|{XTTS_TEMPERATURA}|{frase.texto}"
    if frase.recortar or frase.ganancia != 1.0:
        clave += f"|recortar={frase.recortar}|ganancia={frase.ganancia}"
    if frase.temperatura:
        clave += f"|temperatura={frase.temperatura}"
    codigo = hashlib.md5(clave.encode()).hexdigest()[:6]
    carpeta = CACHE_DIR.joinpath(*(_nombre_archivo(c) for c in frase.carpeta.split("/")))
    return carpeta / f"{_nombre_archivo(frase.nombre or frase.texto)} - {codigo}.wav"


XTTS_TOPE_SEGUNDOS = 15 * 60  # si XTTS se cuelga, se corta igual: no puede ocupar la placa para siempre
xtts_en_curso: dict[Path, asyncio.Task] = {}  # generaciones en marcha, por archivo de destino


async def _generar_xtts(frase: Frase, ruta: Path):
    """Corre XTTS en un proceso aparte, que carga el modelo, genera, guarda el audio y se cierra."""
    opciones = ["--recortar", "--silencio-final", "0"] if frase.recortar else []
    opciones += ["--ganancia", str(frase.ganancia)] if frase.ganancia != 1.0 else []
    async with xtts_lock:
        if ruta.exists():  # lo generó otra generación mientras esperaba
            return
        ruta.parent.mkdir(parents=True, exist_ok=True)
        inicio = time.monotonic()
        proceso = await asyncio.create_subprocess_exec(
            str((BASE_DIR / XTTS_PYTHON).resolve()), "-W", "ignore", str(BASE_DIR / "voz_xtts.py"),
            "--texto", frase.texto, "--salida", str(ruta),
            "--referencia", str((BASE_DIR / XTTS_REFERENCIA).resolve()),
            "--temperatura", frase.temperatura or XTTS_TEMPERATURA, *opciones,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # sin ventana de consola
        )
        try:
            _, error = await asyncio.wait_for(proceso.communicate(), timeout=XTTS_TOPE_SEGUNDOS)
        except asyncio.TimeoutError:
            proceso.kill()
            await proceso.wait()
            raise RuntimeError(f"XTTS se colgó más de {XTTS_TOPE_SEGUNDOS // 60} minutos y se cortó")
        if proceso.returncode != 0 or not ruta.exists():
            ultima = error.decode("utf-8", "replace").strip().splitlines()[-1:] or ["sin detalle"]
            raise RuntimeError(f"XTTS falló (código {proceso.returncode}): {ultima[0]}")
        log.info("Audio XTTS generado en %.0fs: %s", time.monotonic() - inicio, frase.texto)


def _fin_generacion(ruta: Path, tarea: asyncio.Task):
    xtts_en_curso.pop(ruta, None)
    if not tarea.cancelled() and tarea.exception():
        log.warning("Generación XTTS en segundo plano: %s", tarea.exception())


async def generar_audio_xtts(frase: Frase) -> Path:
    """Audio XTTS de la frase: guardado, o generado esperando como mucho XTTS_TIMEOUT segundos.

    Si tarda más, se lanza TimeoutError pero la generación sigue en segundo plano y el audio
    queda guardado para la próxima vez.
    """
    ruta = ruta_xtts(frase)
    if ruta.exists():
        return ruta
    tarea = xtts_en_curso.get(ruta)
    if tarea is None:  # si ya se está generando (ej: anuncio anterior que tardó), se espera esa
        tarea = asyncio.create_task(_generar_xtts(frase, ruta))
        xtts_en_curso[ruta] = tarea
        tarea.add_done_callback(lambda t: _fin_generacion(ruta, t))
    # shield: si se vence la espera, no se cancela la generación
    await asyncio.wait_for(asyncio.shield(tarea), timeout=XTTS_TIMEOUT)
    return ruta


async def generar_audio(frase: Frase) -> Path:
    texto = frase.texto
    if VOZ_MOTOR == "xtts":
        try:
            return await generar_audio_xtts(frase)
        except asyncio.TimeoutError:
            log.warning("XTTS tarda más de %ss: este anuncio sale con edge-tts y el audio se sigue "
                        "generando para la próxima vez", XTTS_TIMEOUT)
        except Exception as e:
            log.warning("No se pudo generar con XTTS (%s); se usa edge-tts para este anuncio", e)
    return await generar_audio_edge(texto)


async def reproducir(vc: discord.VoiceClient, ruta: Path):
    terminado = asyncio.Event()
    loop = asyncio.get_running_loop()
    vc.play(
        discord.FFmpegPCMAudio(str(ruta), executable=FFMPEG),
        after=lambda _: loop.call_soon_threadsafe(terminado.set),
    )
    try:
        await asyncio.wait_for(terminado.wait(), timeout=60)
    except asyncio.TimeoutError:
        vc.stop()
        raise


async def conectar(canal: discord.VoiceChannel) -> discord.VoiceClient:
    """Devuelve una conexión de voz sana en el canal, descartando una que haya quedado rota."""
    vc = canal.guild.voice_client
    if vc is not None and not vc.is_connected():
        await vc.disconnect(force=True)
        vc = None
    if vc is None:
        return await canal.connect(timeout=20)
    if vc.channel != canal:
        await vc.move_to(canal)
    return vc


def unir_wavs(piezas: list[Path | float]) -> Path | None:
    """Une audios WAV y silencios (en segundos) en un solo archivo, que queda guardado.

    Devuelve None si no se pueden unir (por ejemplo, si una parte salió con edge-tts en mp3):
    en ese caso se reproducen una tras otra.
    """
    rutas = [p for p in piezas if isinstance(p, Path)]
    if not rutas or any(r.suffix != ".wav" for r in rutas):
        return None
    clave = "|".join(p.name if isinstance(p, Path) else f"{p:.2f}" for p in piezas)
    destino = CACHE_DIR / "_armados" / (hashlib.md5(clave.encode()).hexdigest() + ".wav")
    if destino.exists():
        return destino
    with wave.open(str(rutas[0]), "rb") as w:
        formato = w.getparams()
    destino.parent.mkdir(exist_ok=True)
    temporal = destino.with_suffix(".tmp")
    compatibles = True
    with wave.open(str(temporal), "wb") as salida:
        salida.setparams(formato)
        for pieza in piezas:
            if isinstance(pieza, Path):
                with wave.open(str(pieza), "rb") as w:
                    if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != \
                            (formato.nchannels, formato.sampwidth, formato.framerate):
                        compatibles = False
                        break
                    salida.writeframes(w.readframes(w.getnframes()))
            else:
                salida.writeframes(b"\x00" * int(formato.framerate * pieza) * formato.sampwidth * formato.nchannels)
    if not compatibles:  # formatos distintos: no se pueden unir
        temporal.unlink(missing_ok=True)
        return None
    temporal.replace(destino)  # ya cerrado: Windows no deja renombrar un archivo abierto
    return destino


async def hablar(canal: discord.VoiceChannel, partes: list[Frase | float]):
    """Entra al canal, dice las partes una tras otra y se va.

    Cada parte es un audio aparte (así se reutilizan: el remate sirve para cualquier jugador);
    los números son pausas en segundos. Las partes se unen en un solo audio antes de hablar.
    """
    lock = locks.setdefault(canal.guild.id, asyncio.Lock())
    async with lock:
        if not any(not m.bot for m in canal.members):
            return
        # todo listo antes de entrar a hablar
        piezas = [await generar_audio(p) if isinstance(p, Frase) else p for p in partes]
        unido = unir_wavs(piezas)
        if not any(not m.bot for m in canal.members):  # se fueron mientras se generaba el audio
            return
        vc = await conectar(canal)
        try:
            log.info("Diciendo en #%s: %s", canal.name, " ".join(p.texto for p in partes if isinstance(p, Frase)))
            for pieza in [unido] if unido else piezas:
                if isinstance(pieza, Path):
                    await reproducir(vc, pieza)
                else:
                    await asyncio.sleep(pieza)
        finally:
            await vc.disconnect()


# ---------------------------------------------------------------- anuncios

def nombre_vinculado(j: Jugador, hablado: bool = False) -> str | None:
    """Nombre de invocador de un jugador que se vinculó (dio su consentimiento); None si no.

    Se toma de la partida, así refleja cambios de nombre. A los no vinculados (desconocidos
    de la partida) nunca se los nombra: solo se usa su campeón. Para la voz (hablado=True)
    se usa la pronunciación que haya cargado con /vincular, si tiene.
    """
    did = vinculo_de_puuid(j.puuid)
    if not did:
        return None
    if hablado and datos["vinculos"][did].get("pronunciacion"):
        return datos["vinculos"][did]["pronunciacion"]
    return j.nombre


# Cómo se nombra a un jugador sin vincular según su posición. En minúscula (salvo ADC) para
# que la voz no lo deletree.
POSICIONES_RANDOM = {"TOP": "top", "JUNGLE": "jungla", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}
# Para la voz, las que suenan mejor escritas de otra forma
POSICIONES_HABLADAS = {**POSICIONES_RANDOM, "BOTTOM": "adece"}


def random_generico(j: Jugador, hablado: bool = False) -> str:
    """Nombre genérico de un jugador sin vincular: "el random de top" (o "del equipo" en ARAM)."""
    posicion = (POSICIONES_HABLADAS if hablado else POSICIONES_RANDOM).get(j.posicion)
    return f"el random de {posicion}" if posicion else "el random del equipo"


def jugador_texto(j: Jugador, enfasis: bool = False, hablado: bool = False) -> str:
    """Cómo se lo nombra en voz y texto: "Invocador con Campeón" o "el random de <posición>".

    Con énfasis, el nombre va entre signos de admiración ("¡Invocador! con Campeón"), que la
    voz lee con más fuerza. Al jugador sin vincular no se lo nombra ni se dice su campeón, así
    su audio es uno solo por posición y sirve para cualquier partida.
    """
    campeon = riot.nombre_campeon(j.campeon)
    nombre = nombre_vinculado(j, hablado)
    if enfasis:
        return f"¡{nombre}! con {campeon}" if nombre else f"¡{random_generico(j, hablado)}!"
    return f"{nombre} con {campeon}" if nombre else random_generico(j, hablado)


COLAS = {
    420: "Clasificatoria Solo/Dúo", 440: "Clasificatoria Flexible", 400: "Normal (reclutamiento)",
    430: "Normal (a ciegas)", 490: "Partida rápida", 450: "ARAM", 900: "URF", 1900: "URF",
}
NOMBRES_NOTAS = {
    "kda": "KDA", "kp": "participación", "dano": "daño", "farm": "farm", "vision": "visión",
    "oro15": "oro @15", "torres": "daño a torres", "aguante": "aguante", "objetivos": "objetivos",
    "utilidad": "utilidad",
}


def _miles(n: float) -> str:
    return f"{n / 1000:.1f}k"


def _linea_equipo(t: TotalesEquipo) -> str:
    return (f"{t.kills} kills · {_miles(t.oro)} oro · 🗼 Torres {t.torres} · 🏛️ Inhibidores {t.inhibidores}"
            f" · 🐉 Dragones {t.dragones} · 🟣 Larvas {t.larvas} · 🦀 Heraldo {t.heraldos} · 👿 Barones {t.barones}")


def _ficha(j: Jugador) -> str:
    """Estadísticas de un jugador, en tres renglones."""
    lineas = [
        f"⚔️ **{j.kda_texto}** (KDA {j.kda:.1f}) · KP {j.kp:.0%}",
        f"🗡️ {_miles(j.dano)} daño ({j.dano_pct:.0%} del equipo) · 💰 {_miles(j.oro)} oro",
        f"🌾 {j.cs} CS ({j.farm_min:.1f}/min) · 👁️ {j.vision} visión",
    ]
    if j.oro15_diff is not None:
        lineas[-1] += f" · 📈 {j.oro15_diff:+,.0f} oro @15".replace(",", ".")
    return "\n".join(lineas)


def _por_que(j: Jugador) -> str:
    """Las dos estadísticas en las que mejor y peor le fue al destacado (nota de 0 a 10)."""
    orden = sorted(j.notas.items(), key=lambda x: x[1], reverse=True)

    def texto(notas):
        return ", ".join(f"{NOMBRES_NOTAS.get(m, m)} ({v * 10:.0f}/10)" for m, v in notas)

    return f"✅ Lo mejor: {texto(orden[:2])}\n❌ Lo peor: {texto(orden[-2:][::-1])}"


def armar_embed(res: Resultado) -> discord.Embed:
    cola = COLAS.get(res.cola, res.modo.title())
    embed = discord.Embed(
        title=("✅ Victoria" if res.gano else "❌ Derrota") + f" · {cola} · {res.minutos:.0f} min",
        description=f"**Equipo:** {_linea_equipo(res.propio)}\n**Rival:** {_linea_equipo(res.rival)}",
        color=discord.Color.green() if res.gano else discord.Color.red(),
        timestamp=datetime.fromtimestamp(res.fin / 1000, tz=timezone.utc) if res.fin else None,
    )
    for j in res.equipo:
        icono = ("🏆" if res.gano else "💀") if j is res.destacado else "▫️"
        campeon = riot.nombre_campeon(j.campeon) + (f" ({j.rol})" if j.rol else "")
        nombre = nombre_vinculado(j)
        quien = f"{nombre} · {campeon}" if nombre else f"{campeon} · sin vincular"
        embed.add_field(name=f"{icono} {quien} — {j.puntaje:.0f} pts", value=_ficha(j), inline=False)

    d = res.destacado
    titulo = "el carreador" if res.gano else "el más manco"
    embed.add_field(name=f"🔎 Por qué {jugador_texto(d)} es {titulo}", value=_por_que(d), inline=False)
    embed.set_footer(text=f"{res.match_id} · puntaje por rol: contra su equipo y su rival de línea")
    return embed


async def pasar_rol_peor(guild: discord.Guild, j: Jugador) -> discord.Member | None:
    """Da el rol ROL_PEOR al peor si está vinculado y se lo saca a quien lo tenía.

    Devuelve el miembro que recibió el rol, o None si no hubo cambio (peor sin vincular,
    no está en el servidor, ya lo tenía, o el bot no tiene permiso).
    """
    did = vinculo_de_puuid(j.puuid)
    if not ROL_PEOR or not did:
        return None
    try:
        miembro = guild.get_member(int(did)) or await guild.fetch_member(int(did))
    except discord.NotFound:
        return None
    try:
        rol = discord.utils.get(guild.roles, name=ROL_PEOR)
        if rol is None:
            rol = await guild.create_role(name=ROL_PEOR, colour=discord.Colour(0x8B5A2B),
                                          reason="Rol para el peor de la última derrota")
            log.info("Rol %s creado en %s", ROL_PEOR, guild.name)
        if rol in miembro.roles:
            return None

        # Sacárselo al anterior (guardado en datos, porque el bot no ve a todos los miembros)
        # y a cualquier otro que lo tenga y el bot conozca
        anteriores = {m for m in rol.members if m.id != miembro.id}
        previo = datos.setdefault("rol_peor", {}).get(str(guild.id))
        if previo and previo != did:
            try:
                anteriores.add(guild.get_member(int(previo)) or await guild.fetch_member(int(previo)))
            except discord.NotFound:
                pass
        for m in anteriores:
            await m.remove_roles(rol, reason="Otro salió peor en una derrota")

        await miembro.add_roles(rol, reason="Peor del equipo en la última derrota")
    except discord.Forbidden:
        log.warning("Sin permiso para gestionar el rol %s en %s (hace falta 'Gestionar roles' y que "
                    "el rol del bot esté por encima)", ROL_PEOR, guild.name)
        return None
    datos["rol_peor"][str(guild.id)] = did
    guardar_datos()
    log.info("Rol %s pasó a %s en %s", ROL_PEOR, miembro.display_name, guild.name)
    return miembro


def frase_presentacion(res: Resultado) -> Frase:
    """La presentación hablada, guardada en la carpeta de la persona (o en _random)."""
    j = res.destacado
    resultado = "victoria" if res.gano else "derrota"
    persona = nombre_vinculado(j)  # nombre de invocador real, aunque la voz use la pronunciación
    if persona:
        return Frase(armar_mensaje(res, hablado=True), persona, f"{riot.nombre_campeon(j.campeon)} - {resultado}")
    posicion = POSICIONES_RANDOM.get(j.posicion, "equipo")
    return Frase(armar_mensaje(res, hablado=True), "_random", f"{posicion} - {resultado}")


def frase_remate(remate: str, gano: bool) -> Frase:
    """El remate, con más expresividad que el resto (REMATE_TEMPERATURA)."""
    return Frase(remate, "_remates", f"{'victoria' if gano else 'derrota'} - {remate}",
                 temperatura=REMATE_TEMPERATURA)


def inicio_del_mensaje(gano: bool) -> str | None:
    """Lo que va antes del nombre ("El más manco del equipo fue"), si el mensaje se puede partir."""
    plantilla = MENSAJE_MEJOR if gano else MENSAJE_PEOR
    if plantilla.count("{jugador_enfasis}") != 1:
        return None
    antes, despues = plantilla.split("{jugador_enfasis}")
    if "{" in antes or despues.strip() not in ("", ".", "!"):  # algo más después del nombre: no se parte
        return None
    return antes.strip()


def frase_inicio(gano: bool) -> Frase:
    """El inicio, dicho con efusividad: entre signos de admiración y con más expresividad."""
    return Frase(f"¡{inicio_del_mensaje(gano)}!", "_partes/inicios", "victoria" if gano else "derrota",
                 recortar=True, temperatura=INICIO_TEMPERATURA)


def frase_nombre(j: Jugador) -> Frase:
    """El nombre con énfasis: doble admiración, más volumen y el sufijo ("¡¡Adroco kun!!").

    Al sin vincular, su posición ("¡¡el random de top kun!!").
    """
    persona = nombre_vinculado(j)
    dicho = nombre_vinculado(j, hablado=True) if persona else random_generico(j, hablado=True)
    dicho = f"{dicho} {NOMBRE_SUFIJO}".strip()
    return Frase(f"¡¡{dicho}!!", "_partes/nombres", persona or random_generico(j),
                 recortar=True, ganancia=NOMBRE_GANANCIA)


def frase_campeon(campeon: str) -> Frase:
    nombre = riot.nombre_campeon(campeon)
    return Frase(f"con {nombre}.", "_partes/campeones", nombre, recortar=True)


def partes_anuncio(res: Resultado, remate: str) -> list[Frase | float]:
    """Los audios del anuncio y las pausas entre ellos (en segundos)."""
    final = [PAUSA_REMATE, frase_remate(remate, res.gano)] if remate else []
    if VOZ_MOTOR != "xtts" or VOZ_MODO != "partes" or inicio_del_mensaje(res.gano) is None:
        return [frase_presentacion(res)] + final
    j = res.destacado
    campeon = [PAUSA_PARTES, frase_campeon(j.campeon)] if nombre_vinculado(j) else []
    return [frase_inicio(res.gano), PAUSA_ANUNCIO, frase_nombre(j)] + campeon + final


def armar_mensaje(res: Resultado, hablado: bool) -> str:
    """El mensaje del anuncio, sin el remate. hablado=True usa la pronunciación de los nombres."""
    j = res.destacado
    texto = (MENSAJE_MEJOR if res.gano else MENSAJE_PEOR).format(
        jugador=jugador_texto(j, hablado=hablado),
        jugador_enfasis=jugador_texto(j, enfasis=True, hablado=hablado),
        nombre=nombre_vinculado(j, hablado) or random_generico(j, hablado),
        campeon=riot.nombre_campeon(j.campeon),
        kills=j.kills, muertes=j.deaths, asistencias=j.assists,
        kda=j.kda_texto, puntaje=round(j.puntaje),
    )
    # "¡el random de top!." -> "¡el random de top!" (cuando {jugador_enfasis} va antes de un punto)
    return texto.replace("!.", "!")


async def anunciar(res: Resultado, guild: discord.Guild, canal_voz: discord.VoiceChannel | None,
                   canal_texto: discord.abc.Messageable | None):
    await riot.asegurar_campeones()
    j = res.destacado
    remates = REMATES_MEJOR if res.gano else REMATES_PEOR
    remate = random.choice(remates) if remates else ""
    texto = f"{armar_mensaje(res, hablado=False)} {remate}".strip()
    partes_voz = partes_anuncio(res, remate)
    contenido = f"📢 {texto}"
    if not res.gano:
        try:
            nuevo = await pasar_rol_peor(guild, j)
            if nuevo:
                contenido += f"\n🎖️ {nuevo.mention} ahora es **{ROL_PEOR}** hasta que otro lo supere."
        except Exception:
            log.exception("No se pudo pasar el rol %s", ROL_PEOR)

    destino = client.get_channel(CANAL_TEXTO_ID) if CANAL_TEXTO_ID else canal_texto
    if destino:
        try:
            await destino.send(content=contenido, embed=armar_embed(res),
                               allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException:
            log.exception("No se pudo mandar el resumen de texto")
    if canal_voz:
        try:
            await hablar(canal_voz, partes_voz)
        except Exception:
            log.exception("No se pudo anunciar por voz")


def clave_anuncio(res: Resultado) -> str:
    """Identifica partida + equipo, para no anunciar dos veces si juegan varios amigos juntos.

    El equipo se distingue por si ganó o perdió, así no se guarda nada de otros jugadores.
    """
    return f"{res.match_id}:{'victoria' if res.gano else 'derrota'}"


def marcar_anunciada(clave: str):
    datos["anunciadas"] = (datos["anunciadas"] + [clave])[-200:]
    guardar_datos()


# ---------------------------------------------------------------- revisión periódica

async def timeline_o_nada(match_id: str) -> dict | None:
    """La timeline solo aporta el oro @15: si falla, se analiza igual sin ese dato."""
    try:
        return await riot.timeline(match_id)
    except RiotError:
        log.warning("No se pudo obtener la timeline de %s; se analiza sin oro @15", match_id)
        return None


async def revisar_jugador(miembro: discord.Member, canal: discord.VoiceChannel):
    vinculo = datos["vinculos"][str(miembro.id)]
    ultima = await riot.ultima_partida(vinculo["puuid"])
    if ultima is None or ultima == vinculo.get("ultima"):
        return
    vinculo["ultima"] = ultima
    guardar_datos()

    partida = await riot.partida(ultima)
    if partida is None:
        return
    fin_ms = partida["info"].get("gameEndTimestamp", 0)
    if time.time() - fin_ms / 1000 > MAX_ANTIGUEDAD_MIN * 60:
        log.info("Partida %s de %s es vieja, no se anuncia", ultima, miembro.display_name)
        return
    res = analizar(partida, vinculo["puuid"], await timeline_o_nada(ultima))
    if res is None:
        log.info("Partida %s (%s) no se analiza (modo no soportado o remake)", ultima, partida["info"].get("gameMode"))
        return
    clave = clave_anuncio(res)
    if clave in datos["anunciadas"]:
        return
    marcar_anunciada(clave)
    log.info("Partida nueva %s de %s (%s): %s %s (%.0f pts)", ultima, miembro.display_name,
             "victoria" if res.gano else "derrota", "mejor" if res.gano else "peor",
             jugador_texto(res.destacado), res.destacado.puntaje)
    await anunciar(res, canal.guild, canal, canal)


sin_conexion = False  # para avisar una sola vez en el log mientras Riot no responde


@tasks.loop(seconds=INTERVALO)
async def revisar_partidas():
    global sin_conexion
    for guild in client.guilds:
        for canal in guild.voice_channels:
            for miembro in canal.members:
                if miembro.bot or str(miembro.id) not in datos["vinculos"]:
                    continue
                try:
                    await revisar_jugador(miembro, canal)
                except SinConexion as e:
                    if not sin_conexion:
                        log.warning("%s. Reintento cada %ss sin volver a avisar.", e, INTERVALO)
                        sin_conexion = True
                    return
                except ClaveInvalida:
                    log.error("La clave de Riot venció o es inválida: renovala en developer.riotgames.com y reiniciá el bot")
                    return
                except Exception:
                    log.exception("Error revisando la partida de %s", miembro.display_name)
                else:
                    if sin_conexion:
                        log.info("Se recuperó la conexión con Riot")
                        sin_conexion = False


@revisar_partidas.before_loop
async def antes_de_revisar():
    await client.wait_until_ready()


# ---------------------------------------------------------------- comandos

@tree.command(name="vincular", description="Vincula una cuenta de LoL (Nombre#TAG) a un usuario de Discord")
@app_commands.describe(
    riot_id="Tu Riot ID, por ejemplo: Faker#KR1",
    usuario="A quién vincular (por defecto, a vos)",
    pronunciacion="Opcional: cómo tiene que decir tu nombre la voz del bot (ej: xaquilesss → Aquiles)",
)
async def cmd_vincular(interaction: discord.Interaction, riot_id: str, usuario: discord.Member | None = None,
                       pronunciacion: app_commands.Range[str, 1, 50] | None = None):
    usuario = usuario or interaction.user
    if "#" not in riot_id:
        await interaction.response.send_message("Poné el Riot ID completo con el tag, por ejemplo `Faker#KR1`.", ephemeral=True)
        return
    await interaction.response.defer()
    nombre, tag = riot_id.rsplit("#", 1)
    try:
        cuenta = await riot.cuenta(nombre.strip(), tag.strip())
        if cuenta is None:
            await interaction.followup.send(f"No encontré la cuenta **{riot_id}**. Revisá el nombre y el tag.")
            return
        ultima = await riot.ultima_partida(cuenta["puuid"])
    except ClaveInvalida:
        await interaction.followup.send("⚠️ La clave de Riot venció o es inválida. Hay que renovarla.")
        return
    except SinConexion:
        await interaction.followup.send("⚠️ No me puedo conectar con Riot (¿está activo el filtro web?).")
        return
    anterior = datos["vinculos"].get(str(usuario.id), {})
    if pronunciacion is None and anterior.get("puuid") == cuenta["puuid"]:
        pronunciacion = anterior.get("pronunciacion")  # re-vincular la misma cuenta no la borra
    vinculo = {
        "riot_id": f"{cuenta['gameName']}#{cuenta['tagLine']}",
        "puuid": cuenta["puuid"],
        "ultima": ultima,  # las partidas anteriores a vincular no se anuncian
    }
    if pronunciacion:
        vinculo["pronunciacion"] = pronunciacion.strip()
    datos["vinculos"][str(usuario.id)] = vinculo
    guardar_datos()
    extra = f" La voz lo va a decir como **{vinculo['pronunciacion']}**." if pronunciacion else ""
    await interaction.followup.send(
        f"✅ {usuario.mention} quedó vinculado a **{cuenta['gameName']}#{cuenta['tagLine']}**.{extra}")


@tree.command(name="desvincular", description="Quita la cuenta de LoL vinculada")
@app_commands.describe(usuario="A quién desvincular (por defecto, a vos)")
async def cmd_desvincular(interaction: discord.Interaction, usuario: discord.Member | None = None):
    usuario = usuario or interaction.user
    if datos["vinculos"].pop(str(usuario.id), None):
        guardar_datos()
        await interaction.response.send_message(f"🗑️ {usuario.mention} ya no está vinculado.")
    else:
        await interaction.response.send_message(f"{usuario.mention} no tenía una cuenta vinculada.", ephemeral=True)


@tree.command(name="vinculados", description="Lista las cuentas de LoL vinculadas en este servidor")
async def cmd_vinculados(interaction: discord.Interaction):
    lineas = [
        f"• {m.mention} → **{v['riot_id']}**"
        + (f" (se pronuncia: *{v['pronunciacion']}*)" if v.get("pronunciacion") else "")
        for did, v in datos["vinculos"].items()
        if (m := interaction.guild.get_member(int(did)))
    ]
    await interaction.response.send_message("\n".join(lineas) or "Nadie vinculado todavía. Usá `/vincular`.")


@tree.command(name="analizar", description="Analiza la última partida de alguien: el mejor si ganó, el peor si perdió")
@app_commands.describe(usuario="De quién (por defecto, vos)")
async def cmd_analizar(interaction: discord.Interaction, usuario: discord.Member | None = None):
    usuario = usuario or interaction.user
    vinculo = datos["vinculos"].get(str(usuario.id))
    if not vinculo:
        await interaction.response.send_message(f"{usuario.mention} no está vinculado. Usá `/vincular`.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        ultima = await riot.ultima_partida(vinculo["puuid"])
        partida = await riot.partida(ultima) if ultima else None
    except ClaveInvalida:
        await interaction.followup.send("⚠️ La clave de Riot venció o es inválida. Hay que renovarla.")
        return
    except SinConexion:
        await interaction.followup.send("⚠️ No me puedo conectar con Riot (¿está activo el filtro web?).")
        return
    except RiotError:
        log.exception("Error consultando a Riot")
        await interaction.followup.send("⚠️ Riot no respondió bien, probá en un rato.")
        return
    res = analizar(partida, vinculo["puuid"], await timeline_o_nada(ultima)) if partida else None
    if res is None:
        await interaction.followup.send("No encontré una partida analizable (sin partidas, remake o modo no soportado).")
        return
    if CANAL_TEXTO_ID and CANAL_TEXTO_ID != interaction.channel_id:
        await interaction.followup.send(f"📊 Listo, el resultado está en <#{CANAL_TEXTO_ID}>.")
    else:
        await interaction.followup.send("📊 Resultado:")
    voz = usuario.voice.channel if usuario.voice else None
    await anunciar(res, interaction.guild, voz, interaction.channel)


# ---------------------------------------------------------------- arranque

sincronizado = False


@client.event
async def setup_hook():
    await riot.abrir()


@client.event
async def on_ready():
    global sincronizado
    log.info("Conectado como %s", client.user)
    if not sincronizado:
        # Sincronizar por servidor hace que los comandos aparezcan al instante
        for guild in client.guilds:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            log.info("Comandos sincronizados en %s", guild.name)
        sincronizado = True
    if not revisar_partidas.is_running():
        revisar_partidas.start()


@client.event
async def on_guild_join(guild: discord.Guild):
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    if not RIOT_API_KEY:
        raise SystemExit("Falta RIOT_API_KEY en el archivo .env")
    if VOZ_MOTOR == "xtts":
        faltan = [n for n, v in (("XTTS_PYTHON", XTTS_PYTHON), ("XTTS_REFERENCIA", XTTS_REFERENCIA))
                  if not v or not (BASE_DIR / v).is_file()]
        if faltan:
            log.warning("VOZ_MOTOR=xtts pero %s no apunta a un archivo existente: se usa edge-tts",
                        " y ".join(faltan))
            VOZ_MOTOR = "edge"
        else:
            log.info("Voz: XTTS con %s", Path(XTTS_REFERENCIA).name)
    client.run(TOKEN, log_handler=None)
