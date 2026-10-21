import os
import logging
import re
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CITO_API_KEY = os.getenv("CITO_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PARIS = ZoneInfo("Europe/Paris")
SCHEDULE_URL = "https://api.citoapi.com/api/v1/lol/schedule"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.6-flash:generateContent"
)

# Sources officielles du format Worlds 2026
WORLDS_PRIMER_URL = "https://lolesports.com/en-GB/news/worlds-2026-primer"
WORLDS_UPDATE_URL = "https://lolesports.com/en-US/news/msi-and-worlds-updates"

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

MAX_RECAP_CHARS = 3400



def format_date(value):
    """Convertit une date Cito en datetime avec le fuseau Europe/Paris."""
    if value is None or value == "":
        return None

    try:
        # Horodatage Unix en secondes ou millisecondes.
        if isinstance(value, (int, float)):
            timestamp = float(value)

            if abs(timestamp) > 100_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(PARIS)

        text_value = str(value).strip()

        # Certains champs contiennent un horodatage numérique en texte.
        if text_value.isdigit():
            timestamp = float(text_value)

            if abs(timestamp) > 100_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(PARIS)

        # Dates ISO 8601, avec ou sans fuseau horaire.
        normalized = text_value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(PARIS)

    except (ValueError, TypeError, OverflowError, OSError):
        return None


def event_start(event):
    """Trouve la date du match dans les différents formats Cito."""
    if not isinstance(event, dict):
        return None

    date_keys = (
        "startTime", "start_time",
        "scheduledAt", "scheduled_at",
        "start", "date",
        "startDate", "start_date",
        "beginAt", "begin_at",
        "startTimestamp", "start_timestamp",
        "timestamp", "kickoff",
        "gameDate", "game_date",
        "scheduled", "dateTime", "datetime",
    )

    # Cherche d'abord les champs de date directement présents.
    for key in date_keys:
        value = event.get(key)

        if isinstance(value, dict):
            for nested_key in (
                "dateTime", "datetime", "timestamp",
                "value", "date", "startTime", "start_time",
            ):
                parsed = format_date(value.get(nested_key))
                if parsed:
                    return parsed
        else:
            parsed = format_date(value)
            if parsed:
                return parsed

    # Certaines réponses Cito regroupent les informations temporelles.
    for container_key in ("schedule", "time", "start", "game", "match"):
        nested = event.get(container_key)

        if isinstance(nested, dict):
            parsed = event_start(nested)
            if parsed:
                return parsed

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
    """Extrait une liste de matchs depuis les formats de réponse Cito les plus courants."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    # Les réponses peuvent envelopper les événements dans data ou results.
    containers = [payload]
    for outer_key in ("data", "results", "response"):
        nested = payload.get(outer_key)
        if isinstance(nested, dict):
            containers.append(nested)
        elif isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]

    event_keys = (
        "events", "matches", "schedule", "games",
        "fixtures", "results", "items",
    )
    for container in containers:
        for key in event_keys:
            value = container.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                for nested_key in event_keys:
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, list):
                        return [
                            item for item in nested_value
                            if isinstance(item, dict)
                        ]

    # Certaines API renvoient directement un objet-match unique.
    date_keys = (
        "startTime", "start_time", "scheduledAt", "scheduled_at",
        "start", "date", "startDate", "start_date", "beginAt", "begin_at",
    )
    if any(key in payload for key in date_keys):
        return [payload]

    logger.warning(
        "Format de calendrier Cito non reconnu. Clés reçues : %s",
        list(payload.keys())[:30],
    )
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
    """Récupère le score final depuis les différents formats Cito."""

    score = event.get("score")

    # Format : {"team1": 3, "team2": 0}
    if isinstance(score, dict):
        team1_score = score.get("team1")
        team2_score = score.get("team2")

        if team1_score is not None and team2_score is not None:
            return f"{team1_score}–{team2_score}"

        # Format : {"display": "3–0"} ou {"value": "3–0"}
        display = score.get("display") or score.get("value")
        if display is not None:
            return str(display)

    # Format : score directement placé sur les équipes.
    teams = event.get("teams")

    if isinstance(teams, list) and len(teams) >= 2:
        if isinstance(teams[0], dict) and isinstance(teams[1], dict):
            score1 = teams[0].get("score")
            score2 = teams[1].get("score")

            if score1 is not None and score2 is not None:
                return f"{score1}–{score2}"

    # Autres formats possibles.
    if isinstance(score, (int, float, str)) and str(score).strip():
        return str(score)

    result = event.get("result")

    if isinstance(result, str) and result.strip():
        return result.strip()

    if isinstance(result, dict):
        team1_score = result.get("team1")
        team2_score = result.get("team2")

        if team1_score is not None and team2_score is not None:
            return f"{team1_score}–{team2_score}"

        display = result.get("display") or result.get("value")
        if display is not None:
            return str(display)

    return ""

def build_fallback_recap(match):
    """Résumé factuel gratuit, sans dépendance à un service IA."""

    team1 = match["team1"]
    team2 = match["team2"]
    score = match["score"]
    state = match["state"]
    stage = match["stage"]
    match_date = date_fr(match["date"])

    if state == "terminé":
        if score:
            recap = (
                f"## 🏁 Résultat final confirmé\n\n"
                f"**{team1} {score} {team2}**\n\n"
                f"La rencontre de la phase **{stage}** est terminée. "
                f"Le score final transmis par la source est **{score}**. "
                f"Ce résultat permet de confirmer l'issue de la rencontre, "
                f"sans spéculer sur son déroulement.\n\n"
                f"📅 **Date :** {match_date}\n\n"
                f"🔎 Les statistiques détaillées, les drafts et les "
                f"moments clés ne seront mentionnés que si la source "
                f"fournit effectivement ces informations."
            )
        else:
            recap = (
                f"## 🏁 Rencontre terminée\n\n"
                f"**{team1} contre {team2}**\n\n"
                f"La source indique que la rencontre est terminée, "
                f"mais ne fournit pas de score final exploitable. "
                f"Je préfère ne pas inventer le résultat.\n\n"
                f"📅 **Date :** {match_date}\n"
                f"🏆 **Phase :** {stage}"
            )

        return recap

    if state == "en cours":
        return (
            f"## 🔴 Rencontre en cours\n\n"
            f"**{team1} contre {team2}**\n\n"
            f"La rencontre est indiquée comme étant en cours. "
            f"Le score disponible est : **{score or 'non disponible'}**. "
            f"Aucun résultat final ne sera annoncé avant confirmation.\n\n"
            f"📅 {match_date} • 🏆 {stage}"
        )

    return (
        f"## 📅 Rencontre programmée\n\n"
        f"**{team1} contre {team2}**\n\n"
        f"La rencontre est prévue le **{match_date}**, "
        f"dans la phase **{stage}**. "
        f"Le résultat n'est pas encore confirmé.\n\n"
        f"Les informations seront enrichies lorsque la source "
        f"publiera les données du match."
    )

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
        space = shortened.rfind(" ")
        if space > 0:
            shortened = shortened[:space].rstrip() + "…"
        else:
            shortened = shortened.rstrip() + "…"

    return shortened.strip()


async def fetch_schedule(selected_date):
    if not CITO_API_KEY:
        raise RuntimeError(
            "La variable CITO_API_KEY manque sur FadeHost."
        )

    # Marge d'un jour de chaque côté pour tenir compte
    # du décalage entre UTC et l'heure française.
    date_from = (selected_date - timedelta(days=1)).isoformat()
    date_to = (selected_date + timedelta(days=1)).isoformat()

    params = {
        "from": date_from,
        "to": date_to,
        "state": "completed",
        "leagueId": "lol-worlds",
        "limit": 100,
        "offset": 0,
    }

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            SCHEDULE_URL,
            headers={
                "x-api-key": CITO_API_KEY,
                "Accept": "application/json",
            },
            params=params,
        ) as response:
            if response.status != 200:
                details = await response.text()
                logger.error(
                    "Erreur calendrier Cito HTTP %s : %s",
                    response.status,
                    details[:500],
                )
                raise RuntimeError(
                    f"Erreur calendrier Cito : HTTP {response.status}"
                )

            payload = await response.json(content_type=None)

    events = extract_events(payload)

    logger.info(
        "Calendrier Cito historique : %s événement(s) récupéré(s) "
        "pour la période %s à %s.",
        len(events),
        date_from,
        date_to,
    )

    return events


async def fetch_upcoming_schedule():
    """Récupère le calendrier des Worlds, y compris les matchs à venir."""
    if not CITO_API_KEY:
        raise RuntimeError(
            "La variable CITO_API_KEY manque sur FadeHost."
        )

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
                details = await response.text()
                logger.error(
                    "Erreur calendrier futur Cito HTTP %s : %s",
                    response.status,
                    details[:500],
                )
                raise RuntimeError(
                    f"Erreur calendrier Cito : HTTP {response.status}"
                )

            payload = await response.json(content_type=None)

    events = extract_events(payload)

    logger.info(
        "Calendrier Cito futur : %s événement(s) récupéré(s).",
        len(events),
    )

    return events


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


# ---------------------------------------------------------
# /ping
# ---------------------------------------------------------

@bot.tree.command(name="ping", description="Vérifie si BOT LOL ESPORT répond.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 **WORLDS HUB est opérationnel !**",
        ephemeral=True,
    )


# ---------------------------------------------------------
# /format
# Guide détaillé et débutant des Worlds 2026
# ---------------------------------------------------------

@bot.tree.command(
    name="format",
    description="Guide complet du format officiel des Worlds 2026.",
)
async def format_worlds(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)

    pages = [
        {
            "title": "🏆 WORLDS 2026 | 1/5 • Comprendre le tournoi",
            "description": (
                "**Bienvenue dans le guide des Worlds !**\n"
                "Les Worlds sont le championnat du monde annuel de "
                "League of Legends. Les meilleures équipes des ligues "
                "régionales s'y affrontent pour le titre mondial.\n\n"
                "## 🗺️ Le parcours en trois phases\n"
                "**1. Play-Ins • 15 au 18 octobre**\n"
                "Quatre équipes jouent un tableau à double élimination. "
                "Une seule équipe en sort qualifiée.\n\n"
                "**2. Phase suisse • 23 au 31 octobre**\n"
                "Seize équipes jouent un maximum de cinq manches. "
                "Il faut trois victoires pour accéder à la phase finale. "
                "Trois défaites entraînent l'élimination.\n\n"
                "**3. Phase finale • 3 au 14 novembre**\n"
                "Huit équipes s'affrontent en élimination directe, des "
                "quarts de finale jusqu'à la grande finale.\n\n"
                "## 👥 Combien d'équipes ?\n"
                "19 équipes participent au tournoi. Quatre commencent en "
                "Play-Ins. La gagnante rejoint les quinze équipes déjà "
                "qualifiées pour la phase suisse, soit seize équipes.\n\n"
                "Les Worlds 2026 se déroulent aux États-Unis."
            ),
            "footer": "Dates et format : LoL Esports / Riot Games",
        },
        {
            "title": "⚔️ WORLDS 2026 | 2/5 • Les Play-Ins",
            "description": (
                "## 🎯 Quel est le but ?\n"
                "Quatre équipes se disputent **une seule place** pour "
                "la phase suisse. Chaque match de Play-Ins est un **BO5**.\n\n"
                "## 🔁 Que signifie « double élimination » ?\n"
                "Dans un tableau à double élimination, une équipe ne quitte "
                "pas le tournoi après sa première défaite. Elle peut continuer "
                "dans le tableau inférieur et tenter de gagner sa place.\n\n"
                "En revanche, une deuxième défaite élimine l'équipe. "
                "Le parcours exact dépend du tableau officiel et des résultats.\n\n"
                "## 🧠 Exemple simple\n"
                "• Une équipe gagne son premier match : elle continue dans "
                "le tableau correspondant à sa victoire.\n"
                "• Elle perd un match : elle peut encore avoir une possibilité "
                "de rester en compétition.\n"
                "• Elle subit une deuxième défaite : son tournoi s'arrête.\n\n"
                "## 📌 À retenir\n"
                "• 4 équipes au départ.\n"
                "• Des séries en BO5.\n"
                "• 1 seule équipe qualifiée pour la phase suisse.\n"
                "• Les trois autres équipes ne rejoignent pas la phase suisse."
            ),
            "footer": "Play-Ins : 15–18 octobre 2026 • Riot Games",
        },
        {
            "title": "🧮 WORLDS 2026 | 3/5 • La phase suisse",
            "description": (
                "## 🎯 L'objectif\n"
                "Seize équipes commencent la phase suisse. Chaque équipe "
                "cherche à atteindre **3 victoires** avant de subir **3 défaites**.\n\n"
                "• **3 victoires :** qualification pour la phase finale.\n"
                "• **3 défaites :** élimination du tournoi.\n"
                "• Au maximum, une équipe joue 5 matchs dans cette phase.\n\n"
                "## 🔄 Comment les adversaires sont-ils choisis ?\n"
                "Au premier tour, les confrontations sont déterminées par "
                "un tirage organisé selon les groupes de tirage officiels. "
                "Deux équipes de la même région ne s'affrontent pas au "
                "premier tour.\n\n"
                "À partir du deuxième tour, les équipes sont appariées avec "
                "des adversaires ayant le même bilan : par exemple, une équipe "
                "à 1 victoire et 0 défaite rencontre une autre équipe au bilan "
                "1–0. Les équipes ne rejouent pas contre un adversaire déjà "
                "rencontré pendant cette phase.\n\n"
                "Les rencontres entre équipes d'une même région sont possibles "
                "à partir du deuxième tour.\n\n"
                "## 🎮 BO1 ou BO3 ?\n"
                "**BO1 :** un seul match joué, le gagnant remporte la rencontre.\n"
                "**BO3 :** la première équipe à gagner deux parties remporte "
                "la série. Les matchs qui déterminent la qualification ou "
                "l'élimination sont en BO3 ; les autres sont en BO1."
            ),
            "footer": "Phase suisse : 23–31 octobre 2026 • Riot Games",
        },
        {
            "title": "🏁 WORLDS 2026 | 4/5 • La phase finale",
            "description": (
                "## 👑 Les huit dernières équipes\n"
                "Les huit équipes qualifiées depuis la phase suisse disputent "
                "la phase finale. Le tableau fonctionne en **élimination directe**.\n\n"
                "Cela signifie qu'une défaite met fin au parcours de l'équipe "
                "dans le tournoi. Il n'existe pas de tableau inférieur à ce stade.\n\n"
                "## 🥊 Les trois étapes\n"
                "**Quarts de finale :** 8 équipes, 4 équipes gagnantes.\n\n"
                "**Demi-finales :** 4 équipes, 2 équipes gagnantes.\n\n"
                "**Grande finale :** les 2 dernières équipes se disputent "
                "le titre de championne du monde.\n\n"
                "Toutes les séries de la phase finale sont en **BO5**.\n\n"
                "## 🎲 Comment se fait le tirage des quarts ?\n"
                "Selon les règles annoncées pour 2026, les deux équipes "
                "ayant terminé la phase suisse avec un bilan de 3–0 sont "
                "appariées à deux équipes ayant terminé en 3–2. Les quatre "
                "autres équipes qualifiées sont ensuite tirées au sort "
                "pour former les deux autres quarts de finale.\n\n"
                "## 📅 Les dates\n"
                "• Quarts de finale : 3–6 novembre.\n"
                "• Demi-finales : 7–8 novembre.\n"
                "• Grande finale : 14 novembre 2026."
            ),
            "footer": "Phase finale : novembre 2026 • Riot Games",
        },
        {
            "title": "📖 WORLDS 2026 | 5/5 • Le dictionnaire du débutant",
            "description": (
                "## 🗣️ Les termes à connaître\n"
                "**BO1 – Best of 1 :** une seule partie. Le gagnant remporte "
                "le match.\n\n"
                "**BO3 – Best of 3 :** série en deux manches gagnantes. "
                "Il faut gagner 2 parties pour remporter la série. "
                "Maximum : 3 parties.\n\n"
                "**BO5 – Best of 5 :** série en trois manches gagnantes. "
                "Il faut gagner 3 parties. Maximum : 5 parties.\n\n"
                "**Phase suisse :** format où les équipes rencontrent des "
                "adversaires ayant un bilan comparable, sans affronter "
                "toutes les autres équipes.\n\n"
                "**Bilan 2–1 :** deux victoires et une défaite.\n\n"
                "**Qualification :** l'équipe gagne le droit de passer "
                "à la phase suivante.\n\n"
                "**Élimination :** l'équipe n'a plus la possibilité de "
                "remporter le tournoi.\n\n"
                "**Tirage / seed :** le tirage établit les confrontations ; "
                "un seed désigne la place ou le statut attribué à une équipe "
                "selon les règles de la compétition.\n\n"
                "## 🧠 Un exemple pour la phase suisse\n"
                "Une équipe gagne ses trois premiers matchs : elle atteint "
                "3–0 et se qualifie. Une autre gagne deux matchs puis perd "
                "trois fois avant d'atteindre trois victoires : elle termine "
                "à 2–3 et est éliminée. Une équipe peut donc être éliminée "
                "avant d'avoir joué cinq matchs.\n\n"
                "## 🔎 Sources officielles\n"
                f"[Worlds 2026 Primer]({WORLDS_PRIMER_URL})\n"
                f"[Mise à jour officielle du format]({WORLDS_UPDATE_URL})\n\n"
                "Ce guide décrit le format annoncé par Riot Games. Les "
                "horaires et les confrontations doivent être vérifiés dans "
                "le calendrier officiel."
            ),
            "footer": "WORLDS HUB • Guide explicatif • Saison 2026",
        },
    ]

    try:
        for page in pages:
            embed = discord.Embed(
                title=page["title"],
                description=page["description"],
                color=discord.Color.from_rgb(25, 170, 220),
            )
            embed.set_footer(text=page["footer"])
            await interaction.followup.send(embed=embed, ephemeral=True)

    except discord.HTTPException:
        logger.exception("Erreur d'envoi du guide /format")
        await interaction.followup.send(
            "⚠️ Le guide n'a pas pu être envoyé en entier. "
            "Relance `/format` dans quelques instants.",
            ephemeral=True,
        )


# ---------------------------------------------------------
# /prochains_matchs
# ---------------------------------------------------------

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
        events = await fetch_upcoming_schedule()
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

        start = event_start(event)

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


# ---------------------------------------------------------
# /resultats
# ---------------------------------------------------------

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
        events = await fetch_schedule(selected_date)
    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
        logger.exception("Erreur calendrier pour /resultats")
        await interaction.followup.send(
            f"❌ Impossible de récupérer les matchs : {exc}",
            ephemeral=True,
        )
        return
    
    logger.info("DEBUG RESULTATS : date recherchée = %s", selected_date)
    logger.info("DEBUG RESULTATS : %s événements reçus", len(events))

    for i, event in enumerate(events):
        if isinstance(event, dict):
            logger.info(
                "DEBUG RESULTATS #%s : %s",
                i + 1,
                event
            )

    matches = []

    for event in events:
        if not isinstance(event, dict):
            continue

        start = event_start(event)

        if not start or start.date() != selected_date:
            continue

        # /resultats doit afficher uniquement les rencontres terminées.
        state = str(event_state(event) or "").strip().lower()
        if state not in {"terminé", "termine", "completed", "complete", "finished", "ended"}:
            continue

        # Empêche l'affichage d'autres compétitions si l'API
        # ignore ou n'applique pas son filtre de ligue.
        league_marker = " ".join(
            str(event.get(key) or "")
            for key in (
                "leagueId",
                "leagueName",
                "tournamentId",
                "tournamentName",
            )
        ).lower()

        if "worlds" not in league_marker and "world championship" not in league_marker:
            continue

        # Les scores bruts de Cito sont conservés pour éviter de les perdre.
        team1_data = event.get("team1") or {}
        team2_data = event.get("team2") or {}
        score1 = team1_data.get("score")
        score2 = team2_data.get("score")

        score_text = (
            f"{score1} - {score2}"
            if score1 is not None and score2 is not None
            else event_score(event)
        )

        matches.append({
            "date": start,
            "team1": extract_team(event, 1),
            "team2": extract_team(event, 2),
            "stage": event_stage(event),
            "state": event_state(event),
            "score": score_text,
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

        except Exception:
            logger.exception(
                "Échec du résumé Gemini. Utilisation du résumé factuel."
            )
            text = build_fallback_recap(match)

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
            text="WORLDS HUB • RESULTATS Cito • Résumé factuel si IA indisponible"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

# ============================================================
# COMMANDE /PUBLIER
# ============================================================

@bot.tree.command(
    name="publier",
    description="Publie une annonce personnalisée dans worlds-2026.",
)
@app_commands.describe(
    titre="Titre principal de l'annonce.",
    texte="Contenu du message.",
    sous_titre="Sous-titre facultatif.",
    couleur="Couleur de l'annonce.",
    image_url="URL facultative d'une image.",
    style="Présentation immersive ou embed classique.",
)
@app_commands.choices(
    couleur=[
        app_commands.Choice(name="Or Worlds", value="or"),
        app_commands.Choice(name="Bleu", value="bleu"),
        app_commands.Choice(name="Rouge", value="rouge"),
        app_commands.Choice(name="Vert", value="vert"),
        app_commands.Choice(name="Violet", value="violet"),
        app_commands.Choice(name="Blanc", value="blanc"),
    ],
    style=[
        app_commands.Choice(
            name="Immersif : grands titres et texte Markdown",
            value="immersive",
        ),
        app_commands.Choice(
            name="Embed : annonce encadrée classique",
            value="embed",
        ),
    ],
)
@app_commands.default_permissions(manage_messages=True)
async def publier(
    interaction: discord.Interaction,
    titre: app_commands.Range[str, 1, 180],
    texte: app_commands.Range[str, 1, 3500],
    sous_titre: app_commands.Range[str, 1, 250] = "",
    couleur: str = "or",
    image_url: str = "",
    style: str = "immersive",
):
    # Vérification : seul Mathcry peut publier
    OWNER_ID = 483957046278881301

    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "⛔ Commande réservée au propriétaire de WORLDS HUB.",
            ephemeral=True,
        )
        return


    # Recherche du salon
    channel = (
        discord.utils.get(
            interaction.guild.text_channels,
            name="worlds-2026",
        )
        if interaction.guild
        else None
    )

    if channel is None:
        await interaction.response.send_message(
            "❌ Je ne trouve pas le salon **#worlds-2026** "
            "sur ce serveur.",
            ephemeral=True,
        )
        return

    # Palette de couleurs
    palette = {
        "or": discord.Color.gold(),
        "bleu": discord.Color.blue(),
        "rouge": discord.Color.red(),
        "vert": discord.Color.green(),
        "violet": discord.Color.purple(),
        "blanc": discord.Color.from_rgb(240, 240, 240),
    }

    selected_color = palette.get(couleur, discord.Color.gold())

    titre = titre.strip()
    texte = texte.strip()
    sous_titre = sous_titre.strip()
    image_url = image_url.strip()

    # Vérification simple de l'URL de l'image
    if image_url and not re.match(r"^https?://", image_url, re.IGNORECASE):
        await interaction.response.send_message(
            "⚠️ L'image doit utiliser une URL commençant par `https://` "
            "ou `http://`.",
            ephemeral=True,
        )
        return

    # --------------------------------------------------------
    # STYLE IMMERSIF
    # Les titres Markdown sont affichés en grand dans Discord.
    # --------------------------------------------------------
    if style == "immersive":
        content_parts = [
            f"# {titre}",
        ]

        if sous_titre:
            content_parts.append(f"## {sous_titre}")

        content_parts.append(texte)

        if image_url:
            content_parts.append(image_url)

        content = "\n\n".join(content_parts)

        # Discord limite le contenu d'un message à 2000 caractères.
        if len(content) > 2000:
            await interaction.response.send_message(
                "⚠️ Le message immersif dépasse la limite de 2 000 "
                "caractères de Discord. Raccourcis le titre, le sous-titre "
                "ou le texte, puis réessaie.",
                ephemeral=True,
            )
            return

        try:
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'envoyer des messages "
                "dans le salon worlds-2026.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as error:
            logger.exception("Erreur de publication immersive : %s", error)

            await interaction.response.send_message(
                "❌ Discord n'a pas accepté la publication. "
                "Vérifie la longueur du message et les permissions du bot.",
                ephemeral=True,
            )
            return

    # --------------------------------------------------------
    # STYLE EMBED
    # --------------------------------------------------------
    else:
        embed = discord.Embed(
            title=titre,
            description=texte[:4096],
            color=selected_color,
            timestamp=datetime.now(timezone.utc),
        )

        if sous_titre:
            embed.set_author(name=sous_titre)

        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text="WORLDS HUB • Worlds 2026")

        try:
            await channel.send(embed=embed)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Je n'ai pas la permission d'envoyer des messages "
                "dans le salon worlds-2026.",
                ephemeral=True,
            )
            return

        except discord.HTTPException as error:
            logger.exception("Erreur de publication embed : %s", error)

            await interaction.response.send_message(
                "❌ Discord n'a pas accepté la publication. "
                "Vérifie les champs et les permissions du bot.",
                ephemeral=True,
            )
            return

    # Confirmation privée
    await interaction.response.send_message(
        f"✅ Annonce publiée dans {channel.mention} "
        f"avec le style **{style}**.",
        ephemeral=True,
    )


# ============================================================
# DEMARRAGE
# ============================================================

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "La variable d'environnement DISCORD_TOKEN est manquante."
        )

    bot.run(TOKEN)
