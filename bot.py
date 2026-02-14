import os
import discord
from discord import app_commands

GUILD_ID = os.getenv("DEV_SERVER_ID")

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await self.tree.sync(guild=guild)
            print("Synced to dev guild")
        else:
            await self.tree.sync()
            print("Synced globally")

bot = Bot()

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

bot.run(os.environ["DISCORD_TOKEN"])