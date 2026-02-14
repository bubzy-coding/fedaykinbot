import os
import discord
from discord import app_commands

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()  # sync globally

bot = Bot()

@bot.tree.command(name="ping", description="Check if bot works")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)
    
bot.run(os.environ["DISCORD_TOKEN"])