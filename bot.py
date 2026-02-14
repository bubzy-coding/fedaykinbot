import os
import discord
import json
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

with open("items_list.json", "r", encoding=("utf-8")) as f:
    ITEMS = json.load(f)


item_choices = [
    app_commands.Choice(name=item, value=item)
    for item in ITEMS
]


@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

@bot.tree.command(name="donate", description="Donate items")
async def donate(
    interaction: discord.Interaction,
    item: str,
    quantity: int
):
    await interaction.response.send_message(
        f"You donated {quantity} {item}",
        ephemeral=True
    )

@donate.autocomplete("item")
async def item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item, value=item)
        for item in ITEMS if current.lower() in item.lower()
    ][:25]

bot.run(os.environ["DISCORD_TOKEN"])