import os
import logging
import re
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CITO_API_KEY = os.getenv("CITO_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PARIS = ZoneInfo("Europe/Paris")
SCHEDULE_URL = "https://api.citoapi.com/api/v1/lol/leagues/lol-worlds/schedule"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.6-flash:generateContent"
)

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

# Limite prudente pour le texte de chaque résumé Discord.
MAX_RECAP_CHARS = 3400


def format_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PARIS)
    except (ValueError, TypeError):
        return None


def date_fr(dt):
    return (
        f"{JOURS[dt.weekday()].capitalize()} {dt.day} "
        f"{MOIS[dt.month - 1]} {dt.year} à {dt:%H} h {dt:%M}"
    )


def team_name(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "shortName", "short_name", "code", "abbreviation"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if isinstance(value.get("team"), dict):
            return team_name(value["team"])
    return "Équipe à déterminer"


def extract_events(payload):
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("events", "matches", "schedule", "games"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def extract_team(event, number):
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


def event_stage(event):
    return str(
        event.get("blockName")
        or event.get("block_name")
        or event.get("stage")
        or "Worlds 2026"
    )


def event_state(event):
    state = str(event.get("state") or event.get("status") or "").lower()
    if state in ("completed", "complete", "finished", "ended"):
        return "terminé"
    if state in ("in_progress", "in-progress", "live", "running"):
        return "en cours"
    if state in ("unstarted", "scheduled", "not_started", ""):
        return "à venir"
    return state or "inconnu"


def event_score(event):
    score = event.get("score")
    if isinstance(score, dict):
        return str(score.get("display") or score.get("value") or "")
    if score is not None:
        return str(score)
    result = event.get("result")
    if isinstance(result, str):
        return result
    return ""


def safe_text(text, limit=MAX_RECAP_CHARS):
    """Évite les textes trop longs et privilégie une fin de phrase."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    shortened = text[:limit - 1]
    boundaries = [
        shortened.rfind(". "),
        shortened.rfind("! "),
        shortened.rfind("? "),
        shortened.rfind("\n"),
    ]
    boundary = max(boundaries)

    if boundary >= int(limit * 0.65):
        shortened = shortened[:boundary + 1]
    else:
        # Si aucune phrase complète n'est disponible, coupe au dernier espace.
        space = shortened.rfind(" ")
        if space > 0:
            shortened = shortened[:space].rstrip() + "…"
        else:
            shortened = shortened.rstrip() + "…"

    return shortened.strip()


async def fetch_schedule():
    if not CITO_API_KEY:
        raise RuntimeError("La variable CITO_API_KEY manque sur FadeHost.")

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            SCHEDULE_URL,
            headers={
                "x-api-key": CITO_API_KEY,
                "Accept": "application/json",
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Erreur calendrier Cito : HTTP {response.status}"
                )
            payload = await response.json()

    return extract_events(payload)


async def generate_recap(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("La variable GEMINI_API_KEY manque sur FadeHost.")

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.55,
                    "maxOutputTokens": 1800,
                },
            },
        ) as response:
            if response.status != 200:
                details = await response.text()
                logger.error(
                    "Erreur Gemini HTTP %s: %s",
                    response.status,
                    details[:500],
                )
                raise RuntimeError(
                    f"Gemini a refusé la requête (HTTP {response.status}). "
                    "Vérifie la disponibilité du service et les quotas."
                )

            payload = await response.json()

    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini n'a renvoyé aucun résumé exploitable.")

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "\n".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()

    if not text:
        reason = candidate.get("finishReason", "inconnu")
        raise RuntimeError(
            f"Gemini n'a pas produit de texte exploitable (motif : {reason})."
        )

    finish_reason = candidate.get("finishReason", "")
    if finish_reason == "MAX_TOKENS":
        logger.warning("Gemini a atteint sa limite de génération.")

    return safe_text(text)


def build_prompt(match):
    if match["state"] == "terminé":
        mode = (
            "Le match est terminé. Résume uniquement les résultats et événements "
            "réellement présents dans les données. Si le score détaillé ou les "
            "moments clés manquent, indique-le. Ne reconstitue jamais le déroulé."
        )
    elif match["state"] == "en cours":
        mode = (
            "Le match est en cours. Ne donne aucun résultat final non confirmé. "
            "Présente seulement les informations disponibles et précise les "
            "éléments manquants."
        )
    else:
        mode = (
            "Le match est à venir. Écris un avant-match énergique. Ne prétends "
            "pas connaître l'avenir. N'invente ni enjeu de qualification, ni "
            "élimination, ni historique, ni forme récente, ni actualité. "
            "Si ces informations ne sont pas dans les données, dis qu'elles "
            "restent à confirmer. Le format du match est inconnu s'il n'est "
            "pas explicitement fourni."
        )

    raw_data = str(match["raw"])[:5000]

    return f"""
Tu es WORLDS HUB, journaliste esport spécialisé dans League of Legends.
Écris en français avec un ton dynamique, précis et naturel, adapté à Discord.

RÈGLES ABSOLUES DE FIABILITÉ :
- Les seules sources disponibles sont les données fournies ci-dessous.
- Tu n'as pas d'accès garanti aux actualités ou aux statistiques en direct.
- N'invente aucun score, format Bo1/Bo3/Bo5, classement, résultat, historique,
  enjeu, qualification, élimination, statistique, citation ou événement.
- Le nom d'une phase ne suffit pas à prouver les enjeux d'un match.
- N'annonce pas qu'un match est décisif sans preuve explicite.
- Ne transforme pas une donnée absente en fait probable.
- Distingue clairement les faits confirmés et les informations inconnues.
- Si une information importante manque, indique brièvement « non confirmé
  par les données disponibles ».
- Ne répète pas ces avertissements à chaque paragraphe.
- Ne prétends pas avoir consulté des sources que tu n'as pas reçues.

RENCONTRE
Équipe 1 : {match['team1']}
Équipe 2 : {match['team2']}
Date française : {date_fr(match['date'])}
Phase indiquée par l'API : {match['stage']}
État indiqué par l'API : {match['state']}
Score indiqué par l'API : {match['score'] or 'non disponible'}
Format du match : non confirmé, sauf présence explicite dans les données brutes.

DONNÉES BRUTES FOURNIES PAR L'API :
{raw_data}

CONSIGNE POUR CETTE RENCONTRE :
{mode}

STRUCTURE SOUHAITÉE :
1. Un titre accrocheur.
2. Une fiche du match avec uniquement les informations confirmées.
3. Une analyse adaptée à l'état du match.
4. Une courte conclusion qui distingue les certitudes des inconnues.

Garde le style éditorial de WORLDS HUB, avec quelques emojis bien placés.
Évite les longues introductions, les répétitions et les formulations creuses.
Vise environ 2200 à 2800 caractères au maximum.
Termine toutes tes phrases. Ne t'arrête pas en plein milieu d'une idée.
"""


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


@bot.tree.command(name="ping", description="Vérifie si BOT LOL ESPORT répond.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 **WORLDS HUB est opérationnel !**",
        ephemeral=True,
    )


@bot.tree.command(
    name="prochains_matchs",
    description="Affiche les prochaines rencontres des Worlds 2026.",
)
@app_commands.describe(nombre="Nombre de matchs à afficher")
@app_commands.choices(nombre=[
    app_commands.Choice(name="3 matchs", value=3),
    app_commands.Choice(name="5 matchs", value=5),
    app_commands.Choice(name="10 matchs", value=10),
])
async def prochains_matchs(
    interaction: discord.Interaction,
    nombre: app_commands.Choice[int] | None = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        events = await fetch_schedule()
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
        logger.exception("Erreur récupération calendrier")
        await interaction.followup.send(
            f"❌ Impossible de récupérer le calendrier : {exc}",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    upcoming = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start = format_date(
            event.get("startTime") or event.get("start_time")
            or event.get("scheduledAt") or event.get("scheduled_at")
        )

        if not start or start.astimezone(timezone.utc) <= now:
            continue
        if event_state(event) != "à venir":
            continue

        upcoming.append({
            "date": start,
            "team1": extract_team(event, 1),
            "team2": extract_team(event, 2),
            "stage": event_stage(event),
        })

    upcoming.sort(key=lambda item: item["date"])
    upcoming = upcoming[:(nombre.value if nombre else 5)]

    if not upcoming:
        await interaction.followup.send(
            "📅 Aucun prochain match trouvé dans le calendrier des Worlds 2026.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🏆 WORLDS HUB | Prochains matchs",
        description=(
            "**Championnat du monde de League of Legends 2026**\n"
            "🕒 Horaires en heure française."
        ),
        color=discord.Color.from_rgb(25, 170, 220),
    )

    for match in upcoming:
        embed.add_field(
            name=f"{match['team1']} ⚔️ {match['team2']}",
            value=f"📅 {date_fr(match['date'])}\n🏁 {match['stage']}",
            inline=False,
        )

    embed.set_footer(text="Calendrier Cito • Fuseau Europe/Paris")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="resultats",
    description="Récapitulatif IA des matchs des Worlds pour une date.",
)
@app_commands.describe(
    jour="Jour du mois (ex. 15)",
    mois="Mois",
    annee="Année (2026)",
)
@app_commands.choices(mois=[
    app_commands.Choice(name="Janvier", value=1),
    app_commands.Choice(name="Février", value=2),
    app_commands.Choice(name="Mars", value=3),
    app_commands.Choice(name="Avril", value=4),
    app_commands.Choice(name="Mai", value=5),
    app_commands.Choice(name="Juin", value=6),
    app_commands.Choice(name="Juillet", value=7),
    app_commands.Choice(name="Août", value=8),
    app_commands.Choice(name="Septembre", value=9),
    app_commands.Choice(name="Octobre", value=10),
    app_commands.Choice(name="Novembre", value=11),
    app_commands.Choice(name="Décembre", value=12),
])
async def resultats(
    interaction: discord.Interaction,
    jour: app_commands.Range[int, 1, 31],
    mois: app_commands.Choice[int],
    annee: app_commands.Range[int, 2026, 2026] = 2026,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        selected_date = date(annee, mois.value, jour)
    except ValueError:
        await interaction.followup.send(
            "❌ Cette date n'existe pas. Vérifie le jour et le mois.",
            ephemeral=True,
        )
        return

    try:
        events = await fetch_schedule()
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
        logger.exception("Erreur calendrier pour /resultats")
        await interaction.followup.send(
            f"❌ Impossible de récupérer les matchs : {exc}",
            ephemeral=True,
        )
        return

    matches = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start = format_date(
            event.get("startTime") or event.get("start_time")
            or event.get("scheduledAt") or event.get("scheduled_at")
        )

        if not start or start.date() != selected_date:
            continue

        matches.append({
            "date": start,
            "team1": extract_team(event, 1),
            "team2": extract_team(event, 2),
            "stage": event_stage(event),
            "state": event_state(event),
            "score": event_score(event),
            "raw": event,
        })

    if not matches:
        await interaction.followup.send(
            f"📅 Aucun match trouvé pour le {jour} "
            f"{MOIS[mois.value - 1]} {annee} dans le calendrier Cito.",
            ephemeral=True,
        )
        return

    matches.sort(key=lambda item: item["date"])

    # Chaque match est envoyé séparément pour respecter les limites Discord.
    await interaction.followup.send(
        content=(
            f"📰 **WORLDS 2026 | "
            f"{jour:02d}/{mois.value:02d}/{annee}**"
        ),
        ephemeral=True,
    )

    for match in matches[:5]:
        try:
            text = await generate_recap(build_prompt(match))
        except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
            logger.exception("Erreur génération Gemini")
            text = (
                "⚠️ Le résumé IA n'a pas pu être généré.\n\n"
                f"**{match['team1']} contre {match['team2']}**\n"
                f"📅 {date_fr(match['date'])}\n"
                f"🏁 Phase : {match['stage']}\n"
                f"État : {match['state']}\n"
                f"Score : {match['score'] or 'non disponible'}\n\n"
                "Les informations détaillées ne sont pas disponibles pour le moment."
            )

        embed = discord.Embed(
            title=f"🏆 {match['team1']} vs {match['team2']}",
            description=safe_text(text, MAX_RECAP_CHARS),
            color=discord.Color.from_rgb(25, 170, 220),
        )

        embed.add_field(
            name="📅 Rencontre",
            value=(
                f"{date_fr(match['date'])}\n"
                f"🏁 {match['stage']}\n"
                f"État : {match['state']}"
            ),
            inline=False,
        )

        if match["score"]:
            embed.add_field(
                name="🎯 Score communiqué par l'API",
                value=match["score"][:1000],
                inline=False,
            )

        embed.set_footer(
            text="WORLDS HUB • Données Cito • Faits non confirmés à vérifier"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Le secret DISCORD_TOKEN est manquant.")
    bot.run(TOKEN)
