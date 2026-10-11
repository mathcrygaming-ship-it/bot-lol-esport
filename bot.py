
import os
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")


class LoLEsportBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Commandes Discord synchronisées.")

bot = LoLEsportBot()


@bot.event
async def on_ready():
    print(f"Connecté à Discord : {bot.user}")


@bot.tree.command(
    name="ping",
    description="Vérifie si BOT LOL ESPORT répond."
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        "🏆 BOT LOL ESPORT est en ligne !"
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Secret DISCORD_TOKEN manquant.")
    bot.run(TOKEN)
