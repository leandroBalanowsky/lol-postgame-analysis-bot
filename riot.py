"""Cliente mínimo de la API de Riot (Account-V1 y Match-V5) y Data Dragon."""
import asyncio
import logging
from urllib.parse import quote

import aiohttp

log = logging.getLogger("riot")

# LAS: la plataforma es la2 y el ruteo regional (cuentas y partidas) es americas.
REGION = "https://americas.api.riotgames.com"
DDRAGON = "https://ddragon.leagueoflegends.com"


class RiotError(Exception):
    pass


class ClaveInvalida(RiotError):
    """La clave de API venció (las de desarrollo duran 24 h) o es incorrecta."""


class SinConexion(RiotError):
    """No se llega a los servidores de Riot (sin internet, o un filtro web los bloquea)."""


class Riot:
    def __init__(self, clave: str):
        self.clave = clave
        self.sesion: aiohttp.ClientSession | None = None
        self.campeones: dict[str, str] = {}  # id interno (MonkeyKing) -> nombre (Wukong)

    async def abrir(self):
        self.sesion = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            await self._cargar_campeones()
        except Exception:
            log.warning("No se pudieron cargar los nombres de campeones", exc_info=True)

    async def cerrar(self):
        if self.sesion:
            await self.sesion.close()

    async def _get(self, url: str, intentos: int = 3):
        """GET a la API de Riot. Devuelve None si es 404; reintenta si hay rate limit."""
        for _ in range(intentos):
            try:
                async with self.sesion.get(url, headers={"X-Riot-Token": self.clave}) as r:
                    if r.status == 200:
                        return await r.json()
                    if r.status == 404:
                        return None
                    if r.status in (401, 403):
                        raise ClaveInvalida("La clave de Riot es inválida o venció")
                    if r.status == 429:
                        espera = int(r.headers.get("Retry-After", "5"))
                        log.warning("Límite de consultas de Riot alcanzado, esperando %ss", espera)
                        await asyncio.sleep(espera)
                        continue
                    if r.status >= 500:
                        await asyncio.sleep(2)
                        continue
                    raise RiotError(f"Riot respondió {r.status} para {url}")
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                # Incluye el certificado falso de un filtro web (ej: FortiGuard) que bloquea Riot
                raise SinConexion(f"No se puede conectar con Riot: {e}") from e
        raise RiotError(f"Riot no respondió bien tras {intentos} intentos: {url}")

    async def cuenta(self, nombre: str, tag: str) -> dict | None:
        """Busca una cuenta por Riot ID. Devuelve {puuid, gameName, tagLine} o None."""
        url = f"{REGION}/riot/account/v1/accounts/by-riot-id/{quote(nombre)}/{quote(tag)}"
        return await self._get(url)

    async def ultima_partida(self, puuid: str) -> str | None:
        """ID de la última partida del jugador (ej: LA2_1234567890), o None si no tiene."""
        ids = await self._get(f"{REGION}/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=1")
        return ids[0] if ids else None

    async def partida(self, match_id: str) -> dict | None:
        return await self._get(f"{REGION}/lol/match/v5/matches/{match_id}")

    async def timeline(self, match_id: str) -> dict | None:
        """Evolución minuto a minuto de la partida (se usa para el oro al minuto 15)."""
        return await self._get(f"{REGION}/lol/match/v5/matches/{match_id}/timeline")

    async def _cargar_campeones(self):
        async with self.sesion.get(f"{DDRAGON}/api/versions.json") as r:
            version = (await r.json())[0]
        async with self.sesion.get(f"{DDRAGON}/cdn/{version}/data/es_AR/champion.json") as r:
            datos = (await r.json())["data"]
        self.campeones = {c["id"]: c["name"] for c in datos.values()}
        log.info("Cargados %d campeones (versión %s)", len(self.campeones), version)

    async def asegurar_campeones(self):
        """Reintenta bajar los nombres de campeones si no se pudo al arrancar (ej: sin conexión)."""
        if self.campeones:
            return
        try:
            await self._cargar_campeones()
        except Exception as e:
            log.warning("Siguen sin cargarse los nombres de campeones: %s", type(e).__name__)

    def nombre_campeon(self, champion_name: str) -> str:
        return self.campeones.get(champion_name, champion_name)
