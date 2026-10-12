import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CITO_API_KEY = os.getenv("CITO_API_KEY")

PARIS = ZoneInfo("Europe/Paris")
SCHEDULE_URL = (
    "https://api.citoapi.com/api/v1/lol/leagues/lol-worlds/schedule"
)


def team_name(value):
    if isinstance(value, dict):
        return value.get("name") or value.get("shortName") or "À déterminer"
    if isinstance(value, str) and value.strip():
        return value
    return "À déterminer"


def format_date(value):
    if not value:
        return None

    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)

        return date.astimezone(PARIS)
    except (ValueError, TypeError):
        return None


class LoLEsportBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Commandes Discord synchronisées.")

    async def on_ready(self):
        print(f"BOT LOL ESPORT connecté : {self.user}")


bot = LoLEsportBot()


@bot.tree.command(
    name="ping",
    description="Vérifie si BOT LOL ESPORT répond."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 BOT LOL ESPORT est opérationnel !",
        ephemeral=True
    )


@bot.tree.command(
    name="prochains_matchs",
    description="Affiche les prochaines rencontres des Worlds 2026."
)
@app_commands.describe(nombre="Nombre de matchs à afficher, de 1 à 10")
@app_commands.choices(nombre=[
    app_commands.Choice(name="3 matchs", value=3),
    app_commands.Choice(name="5 matchs", value=5),
    app_commands.Choice(name="10 matchs", value=10),
])
async def prochains_matchs(
    interaction: discord.Interaction,
    nombre: app_commands.Choice[int] = None
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    if not CITO_API_KEY:
        await interaction.followup.send(
            "❌ La clé CITO_API_KEY n'est pas configurée.",
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
                if response.status != 200:
                    await interaction.followup.send(
                        f"❌ Le calendrier est indisponible "
                        f"(erreur API {response.status}).",
                        ephemeral=True
                    )
                    return

                payload = await response.json()

    except (aiohttp.ClientError, TimeoutError, ValueError):
        await interaction.followup.send(
            "❌ Impossible de récupérer le calendrier pour le moment.",
            ephemeral=True
        )
        return

    data = payload.get("data", payload)
    events = data.get("events", [])

    now = datetime.now(timezone.utc)
    upcoming = []

    for event in events:
        start = format_date(
            event.get("startTime") or event.get("start_time")
        )

        if not start or start.astimezone(timezone.utc) <= now:
            continue

        state = str(event.get("state", "")).lower()
        if state not in ("unstarted", "scheduled", "not_started", ""):
            continue

        team1 = team_name(event.get("team1"))
        team2 = team_name(event.get("team2"))

        upcoming.append({
            "date": start,
            "team1": team1,
            "team2": team2,
            "stage": event.get("blockName") or "Worlds 2026",
        })

    upcoming.sort(key=lambda match: match["date"])
    limit = nombre.value if nombre else 5
    upcoming = upcoming[:limit]

    if not upcoming:
        await interaction.followup.send(
            "📅 Aucun prochain match disponible dans le calendrier "
            "des Worlds 2026.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🏆 BOT LOL ESPORT | Prochains matchs",
        description=(
            "Les prochaines rencontres des Worlds 2026\n"
            "🕒 Horaires affichés en heure française."
        ),
        color=discord.Color.blurple()
    )

    for match in upcoming:
        date_text = match["date"].strftime("%A %d/%m à %H:%M")
        embed.add_field(
            name=f"{match['team1']} vs {match['team2']}",
            value=f"📅 {date_text}\n🏁 {match['stage']}",
            inline=False
        )

    embed.set_footer(
        text="Horaires issus du calendrier Cito • Fuseau Europe/Paris"
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Secret DISCORD_TOKEN manquant.")

    bot.run(TOKEN)
