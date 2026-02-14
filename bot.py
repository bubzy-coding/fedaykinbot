import os
import random
import psycopg
import psycopg.errors
import discord
import json
from discord import app_commands
from datetime import datetime, timezone

DATABASE_URL = os.environ["DATABASE_URL"]
GUILD_ID = os.getenv("DEV_SERVER_ID")

conn = psycopg.connect(DATABASE_URL)
conn.autocommit = True

with conn.cursor() as cur:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS donations (
            id SERIAL PRIMARY KEY,
            server_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            item TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            donation_date TIMESTAMPTZ NOT NULL
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS lottery_entries (
            server_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            number INTEGER NOT NULL,
            PRIMARY KEY (server_id, user_id),
            UNIQUE (server_id, number)
        );
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_server_date
    ON donations (server_id, donation_date);
    """)


    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_server_number
    ON lottery_entries (server_id, number);
    """)



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

#-----------------
# Donations
#-----------------

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
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date)
            VALUES (%s, %s, %s, %s, %s)
        """, (server_id, user_id, item, quantity, now))

    await interaction.response.send_message(
        f"{interaction.user.display_name} donated {quantity} {item}"
    )

@donate.autocomplete("item")
async def item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item, value=item)
        for item in ITEMS if current.lower() in item.lower()
    ][:25]


@bot.tree.command(name="report", description="View donation report")
async def report(
    interaction: discord.Interaction,
    start_date: str = None,
    end_date: str = None
):
    server_id = str(interaction.guild.id)

    query = """
        SELECT user_id,
               item,
               SUM(quantity) AS total_quantity
        FROM donations
        WHERE server_id = %s
    """
    params = [server_id]

    if start_date:
        query += " AND donation_date >= %s"
        params.append(start_date)

    if end_date:
        query += " AND donation_date <= %s"
        params.append(end_date)

    query += """
        GROUP BY user_id, item
        ORDER BY user_id, item
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    if not rows:
        await interaction.response.send_message(
            "No donations in that range.",
            ephemeral=True
        )
        return

    lines = ["**Donation Report:**"]

    for user_id, item, qty in rows:
        member = interaction.guild.get_member(int(user_id))
        name = member.display_name if member else user_id
        lines.append(f"{name} donated: {qty} of {item}")

    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True
    )

#-----------------
# Help / Ping
#-----------------


@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)


@bot.tree.command(name="help", description="Show available commands")
async def help_command(interaction: discord.Interaction):

    help_text = """
**Available Commands**

🪵 **Donations**
/donate <item> <quantity>  
Donate an item to the server pool.

/report [start_date] [end_date]  
View donation totals.  
Date format: YYYY-MM-DD

🎟️ **Lottery**
/lottery <number>  
Enter the lottery (pick 1–50, once per round).

/lottery_draw  
Admin only. Draws the winning number and resets entries.

/lottery_reset  
Admin only. Clears all current lottery entries.

⚙️ **Utility**
/ping  
Check if the bot is alive.

/help  
Show this message.
"""

    await interaction.response.send_message(help_text, ephemeral=True)

#-----------------
# Lottery
#-----------------

@bot.tree.command(name="lottery_reset", description="Reset the current lottery")
@app_commands.checks.has_permissions(administrator=True)
async def lottery_reset(interaction: discord.Interaction):

    server_id = str(interaction.guild.id)

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM lottery_entries WHERE server_id = %s",
            (server_id,)
        )

    await interaction.response.send_message(
        "🎟️ Lottery has been reset for this server.",
        ephemeral=True
    )

@bot.tree.command(name="lottery", description="Enter the lottery (1-50)")
async def lottery(
    interaction: discord.Interaction,
    number: int
):
    if number < 1 or number > 50:
        await interaction.response.send_message(
            "Pick a number between 1 and 50.",
            ephemeral=True
        )
        return

    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lottery_entries (server_id, user_id, number)
                VALUES (%s, %s, %s)
            """, (server_id, user_id, number))
    except psycopg.errors.UniqueViolation:
        await interaction.response.send_message(
            "You have already entered or that number is taken.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"{interaction.user.mention} entered the lottery with number {number} 🎟️"
    )


@bot.tree.command(name="lottery_draw", description="Draw the lottery winner")
@app_commands.checks.has_permissions(administrator=True)
async def lottery_draw(interaction: discord.Interaction):

    server_id = str(interaction.guild.id)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT user_id, number
            FROM lottery_entries
            WHERE server_id = %s
        """, (server_id,))
        rows = cur.fetchall()
    if not rows:
        await interaction.response.send_message(
            "No lottery entries yet.",
            ephemeral=True
        )
        return

    winning_number = random.randint(1, 50)

    winner = None
    for user_id, number in rows:
        if number == winning_number:
            winner = user_id
            break

    # Clear entries after draw
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM lottery_entries WHERE server_id = %s",
            (server_id,)
        )

    if winner:
        await interaction.response.send_message(
            f"🎉 Winning number: {winning_number}\n"
            f"<@{winner}> wins!"
        )
    else:
        await interaction.response.send_message(
            f"🎲 Winning number: {winning_number}\n"
            "No winner this time!"
        )


bot.run(os.environ["DISCORD_TOKEN"])