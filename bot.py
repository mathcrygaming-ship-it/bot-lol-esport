
import os
import sys
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")


class LoLEsportBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Commandes Discord synchronisees.")

    async def on_ready(self):
        print(f"Connexion Discord reussie : {self.user}")

        if "--test" in sys.argv:
            print("Test termine. Arret du bot.")
            await self.close()


bot = LoLEsportBot()


@bot.tree.command(
    name="ping",
    description="Verifie si BOT LOL ESPORT repond."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 BOT LOL ESPORT est operationnel !"
    )


@bot.tree.command(
    name="publier_test",
    description="Teste la publication dans le salon Worlds 2026."
)
async def publier_test(interaction: discord.Interaction):
    if not CHANNEL_ID or not CHANNEL_ID.isdigit():
        await interaction.response.send_message(
            "❌ Identifiant du salon manquant ou invalide.",
            ephemeral=True
        )
        return

    channel = bot.get_channel(int(CHANNEL_ID))

    if channel is None:
        try:
            channel = await bot.fetch_channel(int(CHANNEL_ID))
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Impossible de trouver le salon. Verifie son identifiant et les permissions du bot.",
                ephemeral=True
            )
            return

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ Le salon configure n'est pas un salon textuel.",
            ephemeral=True
        )
        return

    try:
        await channel.send(
            "🏆 **BOT LOL ESPORT | Test de publication**\n"
            "La connexion au salon Worlds 2026 fonctionne !"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Le bot n'a pas la permission d'envoyer des messages dans ce salon.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"✅ Message publie dans {channel.mention} !",
        ephemeral=True
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Secret DISCORD_TOKEN manquant.")

    bot.run(TOKEN)

