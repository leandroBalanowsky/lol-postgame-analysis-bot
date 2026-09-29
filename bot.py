"""BOT ANALISIS: cuando termina una partida de LoL de alguien conectado a voz, anuncia al peor
de su equipo si perdieron, o al mejor si ganaron."""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
import edge_tts
import imageio_ffmpeg
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from analisis import Jugador, Resultado, analizar
from riot import ClaveInvalida, Riot, RiotError, SinConexion

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
# Canal de texto para el resumen; vacío = el chat del canal de voz donde están
CANAL_TEXTO_ID = int(os.getenv("CANAL_TEXTO_ID") or 0)
# Derrota: se anuncia al peor del equipo. Victoria: al mejor.
# {jugador} es "Invocador con Campeón" si está vinculado, o "el jugador de Campeón" si no.
MENSAJE_PEOR = os.getenv(
    "MENSAJE_PEOR",
    "El peor del equipo fue {jugador}: {kills} kills, {muertes} muertes y {asistencias} asistencias",
)
MENSAJE_MEJOR = os.getenv(
    "MENSAJE_MEJOR",
    "El mejor del equipo fue {jugador}: {kills} kills, {muertes} muertes y {asistencias} asistencias",
)
VOZ = os.getenv("VOZ", "es-AR-TomasNeural")
VELOCIDAD = os.getenv("VELOCIDAD", "+0%")
TONO = os.getenv("TONO", "+0Hz")
INTERVALO = int(os.getenv("INTERVALO_SEGUNDOS") or 90)  # cada cuánto revisa partidas nuevas
# Partidas que terminaron hace más que esto no se anuncian (ej: el bot estuvo apagado)
MAX_ANTIGUEDAD_MIN = int(os.getenv("MAX_ANTIGUEDAD_MIN") or 20)

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


async def generar_audio(texto: str) -> Path:
    clave = f"{VOZ}|{VELOCIDAD}|{TONO}|{texto}"
    ruta = CACHE_DIR / (hashlib.md5(clave.encode()).hexdigest() + ".mp3")
    if not ruta.exists():
        await asyncio.wait_for(asyncio.to_thread(sintetizar, texto, ruta), timeout=20)
    return ruta


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


async def hablar(canal: discord.VoiceChannel, texto: str):
    """Entra al canal, dice el texto y se va."""
    lock = locks.setdefault(canal.guild.id, asyncio.Lock())
    async with lock:
        if not any(not m.bot for m in canal.members):
            return
        ruta = await generar_audio(texto)
        vc = await conectar(canal)
        try:
            log.info("Diciendo en #%s: %s", canal.name, texto)
            await reproducir(vc, ruta)
        finally:
            await vc.disconnect()


# ---------------------------------------------------------------- anuncios

def nombre_vinculado(j: Jugador) -> str | None:
    """Nombre de invocador de un jugador que se vinculó (dio su consentimiento); None si no.

    Se toma de la partida, así refleja cambios de nombre. A los no vinculados (desconocidos
    de la partida) nunca se los nombra: solo se usa su campeón.
    """
    return j.nombre if vinculo_de_puuid(j.puuid) else None


def jugador_texto(j: Jugador) -> str:
    """Cómo se lo nombra en voz y texto: "Invocador con Campeón" o "el jugador de Campeón"."""
    campeon = riot.nombre_campeon(j.campeon)
    nombre = nombre_vinculado(j)
    return f"{nombre} con {campeon}" if nombre else f"el jugador de {campeon}"


def armar_embed(res: Resultado, guild: discord.Guild) -> discord.Embed:
    lineas = []
    for j in res.equipo:
        if j is res.destacado:
            icono = "🏆" if res.gano else "💀"
        else:
            icono = "▫️"
        stat = f"{j.vision_min:.1f} visión/min" if j.es_support else f"{j.farm_min:.1f} CS/min"
        oro = f" · {j.oro15_diff:+.0f} oro@15" if j.oro15_diff is not None else ""
        campeon = riot.nombre_campeon(j.campeon)
        rol = f" · {j.rol}" if j.rol else ""
        nombre = nombre_vinculado(j)
        quien = f"**{nombre}** ({campeon}{rol})" if nombre else f"**{campeon}**{rol} *(sin vincular)*"
        lineas.append(
            f"{icono} {quien} "
            f"— {j.kda_texto} · {stat} · {j.dano_min:.0f} daño/min · {j.kp:.0%} KP{oro} → **{j.puntaje:.0f}** pts"
        )
    embed = discord.Embed(
        title=("✅ Victoria" if res.gano else "❌ Derrota") + f" · {res.minutos:.0f} min",
        description="\n".join(lineas),
        color=discord.Color.green() if res.gano else discord.Color.red(),
    )
    embed.set_footer(text=f"{res.match_id} · puntaje comparado con el propio equipo")
    return embed


async def anunciar(res: Resultado, guild: discord.Guild, canal_voz: discord.VoiceChannel | None,
                   canal_texto: discord.abc.Messageable | None):
    j = res.destacado
    texto = (MENSAJE_MEJOR if res.gano else MENSAJE_PEOR).format(
        jugador=jugador_texto(j),
        nombre=nombre_vinculado(j) or f"el jugador de {riot.nombre_campeon(j.campeon)}",
        campeon=riot.nombre_campeon(j.campeon),
        kills=j.kills, muertes=j.deaths, asistencias=j.assists,
        kda=j.kda_texto, puntaje=round(j.puntaje),
    )
    destino = client.get_channel(CANAL_TEXTO_ID) if CANAL_TEXTO_ID else canal_texto
    if destino:
        try:
            await destino.send(content=f"🔎 {texto}", embed=armar_embed(res, guild))
        except discord.HTTPException:
            log.exception("No se pudo mandar el resumen de texto")
    if canal_voz:
        try:
            await hablar(canal_voz, texto)
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
@app_commands.describe(riot_id="Tu Riot ID, por ejemplo: Faker#KR1", usuario="A quién vincular (por defecto, a vos)")
async def cmd_vincular(interaction: discord.Interaction, riot_id: str, usuario: discord.Member | None = None):
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
    datos["vinculos"][str(usuario.id)] = {
        "riot_id": f"{cuenta['gameName']}#{cuenta['tagLine']}",
        "puuid": cuenta["puuid"],
        "ultima": ultima,  # las partidas anteriores a vincular no se anuncian
    }
    guardar_datos()
    await interaction.followup.send(f"✅ {usuario.mention} quedó vinculado a **{cuenta['gameName']}#{cuenta['tagLine']}**.")


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
    await interaction.followup.send("Analizando...")
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
    client.run(TOKEN, log_handler=None)
