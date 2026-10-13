import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CITO_API_KEY = os.getenv("CITO_API_KEY")

PARIS = ZoneInfo("Europe/Paris")
SCHEDULE_URL = "https://api.citoapi.com/api/v1/lol/leagues/lol-worlds/schedule"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worlds_hub")

JOURS = [
    "lundi", "mardi", "mercredi", "jeudi",
    "vendredi", "samedi", "dimanche"
]

MOIS = [
    "janvier", "février", "mars", "avril",
    "mai", "juin", "juillet", "août",
    "septembre", "octobre", "novembre", "décembre"
]


def format_date(value):
    if not value:
        return None

    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(PARIS)
    except (ValueError, TypeError):
        return None


def date_fr(date):
    jour = JOURS[date.weekday()]
    mois = MOIS[date.month - 1]
    return f"{jour.capitalize()} {date.day} {mois} à {date:%H} h {date:%M}"


def team_name(value):
    if isinstance(value, str) and value.strip():
        return value.strip()

    if isinstance(value, dict):
        for key in ("name", "shortName", "short_name", "code", "abbreviation"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        nested = value.get("team")
        if isinstance(nested, dict):
            return team_name(nested)

    return "Équipe à déterminer"


def extract_events(payload):
    data = payload.get("data", payload)

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ("events", "matches", "schedule", "games"):
        value = data.get(key)
        if isinstance(value, list):
            return value

    return []


def extract_team(event, number):
    # Plusieurs formats de réponse API sont pris en charge.
    candidates = [
        event.get(f"team{number}"),
        event.get(f"team_{number}"),
        event.get(f"opponent{number}"),
        event.get(f"opponent_{number}"),
    ]

    teams = event.get("teams")
    if isinstance(teams, list) and len(teams) >= number:
        candidates.append(teams[number - 1])

    for candidate in candidates:
        name = team_name(candidate)
        if name != "Équipe à déterminer":
            return name

    return "Équipe à déterminer"


class WorldsHub(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Commandes Discord synchronisées.")

    async def on_ready(self):
        logger.info("BOT LOL ESPORT connecté : %s", self.user)


bot = WorldsHub()


@bot.tree.command(
    name="ping",
    description="Vérifie si BOT LOL ESPORT répond."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 **WORLDS HUB est opérationnel !**",
        ephemeral=True
    )


@bot.tree.command(
    name="prochains_matchs",
    description="Affiche les prochaines rencontres des Worlds 2026."
)
@app_commands.describe(nombre="Nombre de matchs à afficher")
@app_commands.choices(nombre=[
    app_commands.Choice(name="3 matchs", value=3),
    app_commands.Choice(name="5 matchs", value=5),
    app_commands.Choice(name="10 matchs", value=10),
])
async def prochains_matchs(
    interaction: discord.Interaction,
    nombre: app_commands.Choice[int] | None = None
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not CITO_API_KEY:
        await interaction.followup.send(
            "❌ La clé API du calendrier n'est pas configurée sur l'hébergement.",
            ephemeral=True
        )
        return

    try:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                SCHEDULE_URL,
                headers={
                    "x-api-key": CITO_API_KEY,
                    "Accept": "application/json"
                }
            ) as response:
                if response.status == 401 or response.status == 403:
                    await interaction.followup.send(
                        "❌ Accès au calendrier refusé. Vérifie la clé API Cito.",
                        ephemeral=True
                    )
                    return

                if response.status != 200:
                    logger.error("Erreur Cito : HTTP %s", response.status)
                    await interaction.followup.send(
                        f"❌ Le calendrier est temporairement indisponible (erreur {response.status}).",
                        ephemeral=True
                    )
                    return

                payload = await response.json()

    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.exception("Impossible de récupérer le calendrier Cito")
        await interaction.followup.send(
            "❌ Impossible de récupérer le calendrier. Réessaie dans quelques instants.",
            ephemeral=True
        )
        return

    events = extract_events(payload)
    now = datetime.now(timezone.utc)
    upcoming = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start = format_date(
            event.get("startTime")
            or event.get("start_time")
            or event.get("scheduledAt")
            or event.get("scheduled_at")
        )

        if not start or start.astimezone(timezone.utc) <= now:
            continue

        state = str(event.get("state", "")).lower()
        if state not in ("unstarted", "scheduled", "not_started", ""):
            continue

        team1 = extract_team(event, 1)
        team2 = extract_team(event, 2)

        stage = (
            event.get("blockName")
            or event.get("block_name")
            or event.get("stage")
            or "Worlds 2026"
        )

        upcoming.append({
            "date": start,
            "team1": team1,
            "team2": team2,
            "stage": str(stage),
        })

    upcoming.sort(key=lambda match: match["date"])
    upcoming = upcoming[:(nombre.value if nombre else 5)]

    if not upcoming:
        await interaction.followup.send(
            "📅 Aucun prochain match n'a été trouvé dans le calendrier des Worlds 2026.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🏆 WORLDS HUB | Prochains matchs",
        description=(
            "**Championnat du monde de League of Legends 2026**\n"
            "🕒 Tous les horaires sont indiqués en heure française."
        ),
        color=discord.Color.from_rgb(25, 170, 220)
    )

    for match in upcoming:
        embed.add_field(
            name=f"{match['team1']}  ⚔️  {match['team2']}",
            value=(
                f"📅 {date_fr(match['date'])}\n"
                f"🏁 {match['stage']}"
            ),
            inline=False
        )

    embed.set_footer(
        text="Calendrier Cito • Fuseau horaire : Europe/Paris"
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Le secret DISCORD_TOKEN est manquant.")

    bot.run(TOKEN)
