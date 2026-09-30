# Post-Game Analysis Bot

A private, non-commercial Discord bot for a small group of friends who play League of Legends together on the **LAS** server.

When a linked player who is in one of our Discord voice channels finishes a match, the bot fetches the post-game data from the Riot API, scores every member of that player's team and posts a short recap:

- **Loss** → it names the lowest-scoring player of the team 💀
- **Win** → it names the top performer of the team 🏆

The recap is posted as a text message with a per-player breakdown and is also read aloud in the voice channel with text-to-speech. The bot's messages and commands are in Spanish.

After a loss, if the lowest-scoring player is an opted-in member, they get a Discord role (by default "El más manco") until another opted-in member scores lowest in a later loss. Players who haven't opted in never get the role.

## Riot API usage

| Endpoint | Purpose | When |
|---|---|---|
| `ACCOUNT-V1` `/riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}` | Resolve the PUUID of a player | Once, when the player opts in |
| `MATCH-V5` `/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1` | Detect a newly finished match | Every 90 s, only for linked players currently in a voice channel |
| `MATCH-V5` `/lol/match/v5/matches/{matchId}` | Post-game stats | Once per finished match |
| `MATCH-V5` `/lol/match/v5/matches/{matchId}/timeline` | Gold at minute 15 | Once per finished match |
| `CHAMPION-MASTERY-V4` `/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count=20` | Each linked player's most played champions, to pre-generate their voice clips | Only when `pregenerar_voces.py` is run manually |
| Data Dragon `champion.json` | Champion display names | At startup |

- Only **completed** matches are analyzed. The bot uses no live-game or Spectator data and provides no in-game advantage or real-time information.
- Remakes and non-standard modes (Arena, Swarm) are ignored.
- Expected volume is a few requests per minute at peak. `429` responses are handled by waiting for `Retry-After`.

## Privacy and consent

- Players opt in explicitly with `/vincular Name#TAG` (link) and can remove their data at any time with `/desvincular` (unlink).
- **Only opted-in players are named**, using their summoner name. Every other participant of the match (teammates or opponents outside the group) is referred to only by their position, for example "el random de top" ("the random top laner"). Their Riot IDs and PUUIDs are never shown, logged or stored.
- The only stored data (`datos.json`, local) is the link between a Discord user ID and the player's Riot ID/PUUID (plus an optional pronunciation hint for the spoken announcement), the ID of the last processed match, a list of already announced match IDs (to avoid duplicates) and, per Discord server, which linked member currently holds the "worst player" role. Match data is not stored or shared.
- The API key is kept in a local `.env` file that is never committed or shared.

## How the score works

Each player gets a score from 0 to 100, computed from two kinds of stats:

- **General stats** (KDA, kill participation, damage to champions per minute, CS per minute, vision per minute) are compared **within the player's own team**: the best value in the team scores 1 and the others get the proportion.
- **Role stats** are compared **against the opponent in the same position**: equal scores 0.5, double scores 1, half scores 0.
  - Gold difference at 15 minutes (±3000 gold maps to 0–1)
  - Damage to buildings
  - Toughness (damage taken + damage self-mitigated)
  - Objective participation (dragons, Baron, Rift Herald, Voidgrubs)
  - Utility (healing and shielding on teammates, plus crowd control time)

KDA is `(kills + assists) / max(1, deaths)`, so assists count as much as kills.

### Weights by role

| Role | KDA | KP | Damage | CS | Vision | Gold @15 | Buildings | Toughness | Objectives | Utility |
|---|---|---|---|---|---|---|---|---|---|---|
| Top | 25% | 10% | 20% | 10% | | 15% | 10% | 10% | | |
| Jungle | 25% | 25% | 10% | | 10% | 10% | | | 20% | |
| Mid | 30% | 15% | 25% | 10% | 5% | 15% | | | | |
| ADC | 30% | 10% | 30% | 15% | | 10% | 5% | | | |
| Support | 25% | 25% | 10% | | 20% | | | | | 20% |
| No roles (ARAM) | 35% | 20% | 20% | 15% | 10% | | | | | |

If a stat cannot be computed (a match shorter than 15 minutes, no lane opponent, the timeline is unavailable), it is skipped and the remaining weights are rescaled.

## Commands

| Command | Description |
|---|---|
| `/vincular riot_id [usuario] [pronunciacion]` | Link a Riot ID (`Name#TAG`) to yourself or to another member; optionally set how the voice should say the name |
| `/desvincular [usuario]` | Remove the link and its stored data |
| `/vinculados` | List the linked players in the server |
| `/analizar [usuario]` | Analyze someone's last match on demand |

## Setup

Requirements: Python 3.10 or newer (3.12 recommended), a Discord bot token and a Riot API key.

1. **Discord bot:** create an application at <https://discord.com/developers/applications> and copy the bot token. Invite it with the `bot` and `applications.commands` scopes and the *View Channels*, *Send Messages*, *Embed Links*, *Connect*, *Speak* and *Manage Roles* permissions (the last one only for the "worst player" role).
2. **Install:**
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate    Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Configure:** copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` and `RIOT_API_KEY`. The other settings (text channel, spoken messages, voice, polling interval) are documented in the file.
4. **Run:**
   ```bash
   python bot.py
   ```

FFmpeg is bundled through `imageio-ffmpeg`, so there is nothing else to install.

### Optional: local voice cloning with XTTS-v2

By default the spoken announcement uses `edge-tts`. With `VOZ_MOTOR=xtts` the bot instead clones a voice from a short reference recording using [XTTS-v2](https://github.com/idiap/coqui-ai-TTS), running locally on the GPU:

- XTTS lives in its own Python environment (`pip install coqui-tts` plus a CUDA build of PyTorch 2.8), so the bot itself stays lightweight. `XTTS_PYTHON` points to that environment's `python.exe` and `XTTS_REFERENCIA` to a 6–15 s clean recording of a single speaker (not included in this repository).
- For each new sentence the bot runs `voz_xtts.py` as a separate process that loads the model, generates the audio and exits, so the GPU is only used for about 30 seconds after a match. The analysis of the reference voice is computed once and cached next to it. Generated audio is cached like any other announcement.
- If XTTS fails or takes longer than `XTTS_TIMEOUT_SEGUNDOS`, that announcement falls back to `edge-tts`. A slow generation is not discarded: it keeps running in the background and its audio is cached for next time (with a 15-minute safety cap in case XTTS hangs).
- The spoken announcement is split into an intro (player and champion) and a closing line picked at random from `REMATES_PEOR` / `REMATES_MEJOR`, played back to back. Each piece is cached on its own, so a closing line is generated once and reused for everyone. Cached clips are organised by person in `audios/` (e.g. `audios/<summoner>/<champion> - derrota - <code>.wav`, `audios/_random/`, `audios/_remates/`); the short code comes from the text, voice and temperature, so changing any of them makes the bot generate a fresh clip.
- With `VOZ_MODO=partes` the intro itself is assembled from three cached pieces joined into one clip at announcement time: the opening ("El más manco del equipo fue"), the name (said with extra emphasis and volume, `NOMBRE_GANANCIA`) and "con <champion>", with configurable pauses (`PAUSA_ANUNCIO` before the name for a drum-roll effect, `PAUSA_PARTES`, `PAUSA_REMATE`). About 190 clips cover every player with every champion. `VOZ_MODO=enteras` generates each full intro as one clip instead, which sounds more natural but needs a clip per player, champion and result.
- `pregenerar_voces.py` (or `windows/pregenerar-voces.bat`) generates intros in advance: by default each linked player's 20 highest-mastery champions, after a loss and after a win, plus all closing lines (about 200 clips, ~15 minutes on a GTX 1660). It also generates the generic clips for players who haven't opted in (one per position, e.g. "el random de top"). `--todos` covers every champion for each linked player (about 1,700 clips, ~2 hours, ~450 MB). It loads the model once, skips what is already cached and can be stopped and resumed; run it again after linking someone new.
- `pregenerar_voces.py --verificar` (or `windows/verificar-voces.bat`) transcribes the cached clips with [Whisper](https://github.com/openai/whisper) (installed in the XTTS environment with `pip install openai-whisper`) and compares them with the expected sentence, letter by letter and ignoring spaces: low coverage means missing words and a high excess means repetitions or invented words, both typical XTTS glitches. With `--corregir` (what the `.bat` does) the flagged clips are regenerated and checked again, up to three times. The report is written to `verificacion_voces.txt`.
- The XTTS-v2 model is licensed under the Coqui Public Model License (non-commercial use only).

### Running it permanently

- **Windows:** `windows/activar-inicio-automatico.bat` registers a scheduled task that starts the bot at logon through `supervisor.py`, which restarts it if it exits. `detener-bot.bat`, `iniciar-bot.bat`, `estado-bot.bat` and `quitar-inicio-automatico.bat` control it.
- **Linux (Ubuntu/Debian):** `bash deploy/instalar.sh` installs the dependencies and a `systemd` service (`bot-analisis`).

## Project structure

| File | Contents |
|---|---|
| `bot.py` | Discord bot: commands, match polling, announcements (text and voice) |
| `riot.py` | Minimal Riot API client (Account-V1, Match-V5) and Data Dragon |
| `analisis.py` | Scoring and best/worst selection |
| `voz_xtts.py` | Optional XTTS-v2 voice generator, run as a separate process (one sentence or a batch) |
| `pregenerar_voces.py` | Generates spoken announcements in advance with XTTS, or checks them (`--verificar`) |
| `verificar_xtts.py` | Transcribes clips with Whisper and scores them against the expected text |
| `supervisor.py` | Restarts the bot if it exits |
| `windows/`, `deploy/` | Scripts to run it permanently on Windows or Linux |

## Legal

Post-Game Analysis Bot isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
