"""Calcula un puntaje por jugador y elige al mejor/peor de un equipo en una partida de Match-V5.

Hay dos tipos de estadísticas:
- Generales (KDA, KP, daño, farm, visión): se comparan dentro del propio equipo,
  contra el mejor del equipo en esa estadística (0 a 1).
- De rol (oro @15, daño a torres, aguante, objetivos, utilidad): se comparan contra
  el rival de la misma posición en el otro equipo (0,5 = igual, el doble = 1, la mitad = 0).
Cada rol tiene sus propios pesos. Si una estadística no se puede calcular (ARAM, partida
de menos de 15 minutos, sin rival de línea), se ignora y los demás pesos se reparten.
"""
import math
from dataclasses import dataclass, field

# Modos donde "el mejor/peor del equipo" no tiene sentido (Arena por duplas, Swarm es PvE)
MODOS_IGNORADOS = {"CHERRY", "STRAWBERRY"}

PESOS_POR_ROL = {
    "TOP":     {"kda": 0.25, "kp": 0.10, "dano": 0.20, "farm": 0.10, "oro15": 0.15, "torres": 0.10, "aguante": 0.10},
    "JUNGLE":  {"kda": 0.25, "kp": 0.25, "objetivos": 0.20, "oro15": 0.10, "vision": 0.10, "dano": 0.10},
    "MIDDLE":  {"kda": 0.30, "kp": 0.15, "dano": 0.25, "oro15": 0.15, "farm": 0.10, "vision": 0.05},
    "BOTTOM":  {"kda": 0.30, "dano": 0.30, "farm": 0.15, "kp": 0.10, "oro15": 0.10, "torres": 0.05},
    "UTILITY": {"kda": 0.25, "kp": 0.25, "vision": 0.20, "utilidad": 0.20, "dano": 0.10},
}
# Sin posiciones (ARAM y similares): fórmula general
PESOS_GENERALES = {"kda": 0.35, "kp": 0.20, "dano": 0.20, "farm": 0.15, "vision": 0.10}

ROLES = {"TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}

ORO15_TOPE = 3000  # +3000 de oro al minuto 15 vale 1; -3000, 0


@dataclass
class Jugador:
    puuid: str
    nombre: str  # Riot ID sin el tag
    campeon: str  # id interno (ej: MonkeyKing)
    posicion: str  # TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY o "" (ARAM)
    kills: int
    deaths: int
    assists: int
    farm_min: float
    dano_min: float
    vision_min: float
    kp: float  # participación en kills del equipo (0 a 1)
    cs: int = 0
    oro: int = 0
    dano: int = 0  # daño total a campeones
    dano_pct: float = 0.0  # parte del daño del equipo (0 a 1)
    vision: int = 0  # puntaje de visión de Riot
    oro15_diff: float | None = None  # oro al minuto 15 menos el del rival de línea
    puntaje: float = 0.0
    notas: dict = field(default_factory=dict)  # nota (0 a 1) de cada estadística usada

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)

    @property
    def kda_texto(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"

    @property
    def es_support(self) -> bool:
        return self.posicion == "UTILITY"

    @property
    def rol(self) -> str:
        return ROLES.get(self.posicion, "")


@dataclass
class TotalesEquipo:
    kills: int
    oro: int
    torres: int
    dragones: int
    barones: int
    larvas: int
    heraldos: int = 0
    inhibidores: int = 0


@dataclass
class Resultado:
    match_id: str
    modo: str
    cola: int  # queueId de Riot (420 = Solo/Dúo, 440 = Flex, 450 = ARAM...)
    fin: int  # timestamp de fin (ms)
    minutos: float
    gano: bool
    equipo: list[Jugador]  # ordenado de mejor a peor
    propio: TotalesEquipo
    rival: TotalesEquipo

    @property
    def peor(self) -> Jugador:
        return self.equipo[-1]

    @property
    def mejor(self) -> Jugador:
        return self.equipo[0]

    @property
    def destacado(self) -> Jugador:
        """El que se anuncia: el mejor si ganaron, el peor si perdieron."""
        return self.mejor if self.gano else self.peor


# ---------------------------------------------------------------- notas de 0 a 1

def _relativo(valor: float, maximo: float) -> float:
    """Contra el mejor del equipo: el mejor saca 1, el resto la proporción."""
    return valor / maximo if maximo > 0 else 1.0


def _contra_rival(mio: float, rival: float) -> float:
    """Contra el rival de línea: igual = 0,5; el doble = 1; la mitad = 0."""
    if mio <= 0 and rival <= 0:
        return 0.5
    if rival <= 0:
        return 1.0
    if mio <= 0:
        return 0.0
    return min(1.0, max(0.0, 0.5 + 0.5 * math.log2(mio / rival)))


def _oro15(diff: float) -> float:
    return min(1.0, max(0.0, 0.5 + diff / (2 * ORO15_TOPE)))


# ---------------------------------------------------------------- estadísticas de rol (valores crudos)

def _torres(p: dict) -> float:
    return p.get("damageDealtToBuildings", 0)


def _aguante(p: dict) -> float:
    return p.get("totalDamageTaken", 0) + p.get("damageSelfMitigated", 0)


def _objetivos(p: dict) -> float:
    c = p.get("challenges", {})
    return (c.get("dragonTakedowns", 0) + c.get("baronTakedowns", 0)
            + c.get("riftHeraldTakedowns", 0) + c.get("voidMonsterKill", 0))


def _curacion(p: dict) -> float:
    return p.get("totalHealsOnTeammates", 0) + p.get("totalDamageShieldedOnTeammates", 0)


def _control(p: dict) -> float:
    return p.get("timeCCingOthers", 0)


def _vision(p: dict) -> float:
    return p.get("visionScore", 0) + p.get("wardsKilled", 0) + p.get("detectorWardsPlaced", 0)


def _oro_al_minuto(timeline: dict | None, participant_id: int, minuto: int = 15) -> float | None:
    if not timeline:
        return None
    frames = timeline["info"]["frames"]
    if len(frames) <= minuto:  # la partida duró menos
        return None
    return frames[minuto]["participantFrames"][str(participant_id)]["totalGold"]


# ---------------------------------------------------------------- análisis

def analizar(partida: dict, puuid: str, timeline: dict | None = None) -> Resultado | None:
    """Analiza el equipo del jugador `puuid`. Devuelve None si la partida no se puede evaluar."""
    info = partida["info"]
    participantes = info["participants"]
    if info.get("gameMode") in MODOS_IGNORADOS:
        return None
    if any(p.get("gameEndedInEarlySurrender") for p in participantes):  # remake
        return None

    yo = next((p for p in participantes if p["puuid"] == puuid), None)
    if yo is None:
        return None
    del_equipo = [p for p in participantes if p["teamId"] == yo["teamId"]]
    if len(del_equipo) < 2:
        return None

    minutos = max(1.0, info["gameDuration"] / 60)
    kills_equipo = sum(p["kills"] for p in del_equipo)
    dano_equipo = sum(p["totalDamageDealtToChampions"] for p in del_equipo)

    def rival_de(p: dict) -> dict | None:
        pos = p.get("teamPosition", "")
        if not pos:
            return None
        return next((e for e in participantes if e["teamId"] != p["teamId"] and e.get("teamPosition") == pos), None)

    crudos = {}  # puuid -> (participante, rival)
    equipo = []
    for p in del_equipo:
        rival = rival_de(p)
        crudos[p["puuid"]] = (p, rival)
        oro_mio = _oro_al_minuto(timeline, p["participantId"])
        oro_rival = _oro_al_minuto(timeline, rival["participantId"]) if rival else None
        equipo.append(Jugador(
            puuid=p["puuid"],
            nombre=p.get("riotIdGameName") or p.get("summonerName") or "?",
            campeon=p["championName"],
            posicion=p.get("teamPosition", ""),
            kills=p["kills"],
            deaths=p["deaths"],
            assists=p["assists"],
            farm_min=(p["totalMinionsKilled"] + p["neutralMinionsKilled"]) / minutos,
            dano_min=p["totalDamageDealtToChampions"] / minutos,
            vision_min=_vision(p) / minutos,
            kp=(p["kills"] + p["assists"]) / max(1, kills_equipo),
            cs=p["totalMinionsKilled"] + p["neutralMinionsKilled"],
            oro=p.get("goldEarned", 0),
            dano=p["totalDamageDealtToChampions"],
            dano_pct=p["totalDamageDealtToChampions"] / max(1, dano_equipo),
            vision=p.get("visionScore", 0),
            oro15_diff=(oro_mio - oro_rival) if oro_mio is not None and oro_rival is not None else None,
        ))

    # Máximos del equipo para las estadísticas generales
    no_supports = [j for j in equipo if not j.es_support]
    max_kda = max(j.kda for j in equipo)
    max_kp = max(j.kp for j in equipo)
    max_dano = max(j.dano_min for j in equipo)
    max_farm = max((j.farm_min for j in no_supports), default=0)
    # La visión del support se compara con todo el equipo; la del resto, solo entre no supports
    max_vision_equipo = max(j.vision_min for j in equipo)
    max_vision_resto = max((j.vision_min for j in no_supports), default=0)

    for j in equipo:
        p, rival = crudos[j.puuid]
        notas = {
            "kda": _relativo(j.kda, max_kda),
            "kp": _relativo(j.kp, max_kp),
            "dano": _relativo(j.dano_min, max_dano),
            "farm": _relativo(j.farm_min, max_farm),
            "vision": _relativo(j.vision_min, max_vision_equipo if j.es_support else max_vision_resto),
        }
        if rival:
            notas["torres"] = _contra_rival(_torres(p), _torres(rival))
            notas["aguante"] = _contra_rival(_aguante(p), _aguante(rival))
            notas["objetivos"] = _contra_rival(_objetivos(p), _objetivos(rival))
            notas["utilidad"] = (_contra_rival(_curacion(p), _curacion(rival))
                                 + _contra_rival(_control(p), _control(rival))) / 2
        if j.oro15_diff is not None:
            notas["oro15"] = _oro15(j.oro15_diff)

        pesos = PESOS_POR_ROL.get(j.posicion, PESOS_GENERALES)
        usados = {m: peso for m, peso in pesos.items() if m in notas}
        total = sum(usados.values())
        j.notas = {m: notas[m] for m in usados}
        j.puntaje = 100 * sum(peso * notas[m] for m, peso in usados.items()) / total

    equipo.sort(key=lambda j: j.puntaje, reverse=True)
    return Resultado(
        match_id=partida["metadata"]["matchId"],
        modo=info.get("gameMode", ""),
        cola=info.get("queueId", 0),
        fin=info.get("gameEndTimestamp", 0),
        minutos=minutos,
        gano=yo["win"],
        equipo=equipo,
        propio=_totales(info, participantes, yo["teamId"]),
        rival=_totales(info, participantes, 300 - yo["teamId"]),  # los equipos son 100 y 200
    )


def _totales(info: dict, participantes: list[dict], team_id: int) -> TotalesEquipo:
    jugadores = [p for p in participantes if p["teamId"] == team_id]
    equipo = next((t for t in info.get("teams", []) if t["teamId"] == team_id), {})
    objetivos = equipo.get("objectives", {})

    def cant(nombre: str) -> int:
        return objetivos.get(nombre, {}).get("kills", 0)

    return TotalesEquipo(
        kills=sum(p["kills"] for p in jugadores),
        oro=sum(p.get("goldEarned", 0) for p in jugadores),
        torres=cant("tower"),
        dragones=cant("dragon"),
        barones=cant("baron"),
        larvas=cant("horde"),
        heraldos=cant("riftHerald"),
        inhibidores=cant("inhibitor"),
    )
