import os
import discord
import json
from discord import app_commands
import sqlite3
from datetime import datetime


conn = sqlite3.connect("donations.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    item TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    donation_date TEXT NOT NULL
)
""")
conn.commit()

conn.execute("""
CREATE INDEX IF NOT EXISTS idx_server_date
ON donations (server_id, donation_date);
""")

GUILD_ID = os.getenv("DEV_SERVER_ID")

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))

            # Clear existing commands for this guild
            self.tree.clear_commands(guild=guild)

            # Copy global commands to guild
            self.tree.copy_global_to(guild=guild)

            # Sync explicitly to guild
            await self.tree.sync(guild=guild)

            print("Force re-synced to dev guild")
        else:
            await self.tree.sync()
            print("Synced globally")

bot = Bot()

with open("items_list.json", "r", encoding=("utf-8")) as f:
    ITEMS = json.load(f)

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)


@bot.tree.command(name="donate", description="Donate items")
async def donate(
    interaction: discord.Interaction,
    item: str,
    quantity: int
):
    if quantity <= 0:
        await interaction.response.send_message(
            "Quantity must be positive.",
            ephemeral=True
        )
        return

    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    now = datetime.utcnow().isoformat()

    with conn:
        conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date)
            VALUES (?, ?, ?, ?, ?)
        """, (server_id, user_id, item, quantity, now))

    await interaction.response.send_message(
        f"You donated {quantity} {item}",
        ephemeral=True
    )


@bot.tree.command(name="report", description="View donation report")
async def report(
    interaction: discord.Interaction,
    start_date: str = None,
    end_date: str = None
):
    server_id = str(interaction.guild.id)

    query = """
        SELECT item, SUM(quantity)
        FROM donations
        WHERE server_id = ?
    """
    params = [server_id]

    if start_date:
        query += " AND donation_date >= ?"
        params.append(start_date)

    if end_date:
        query += " AND donation_date <= ?"
        params.append(end_date)

    query += " GROUP BY item ORDER BY item"

    with conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        await interaction.response.send_message(
            "No donations in that range.",
            ephemeral=True
        )
        return

    lines = ["**Donation Report:**"]
    for item, qty in rows:
        lines.append(f"{item}: {qty}")

    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True
    )

@donate.autocomplete("item")
async def item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item, value=item)
        for item in ITEMS if current.lower() in item.lower()
    ][:25]

bot.run(os.environ["DISCORD_TOKEN"])