import os
import random
import asyncpg
import discord
from discord import app_commands
from datetime import datetime, timezone

DATABASE_URL = os.environ["DATABASE_URL"]
DEV_GUILD_ID = os.getenv("DEV_SERVER_ID")
FALLBACK_GUILD_ID = 1466549361432461436

ITEMS = []

def parse_date(date_str: str):
    return datetime.strptime(date_str, "%Y-%m-%d")

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.pool = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)

        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS donations (
                    id SERIAL PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    item TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    donation_date TIMESTAMPTZ NOT NULL,
                    is_adjustment BOOLEAN NOT NULL
                );
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS lottery_entries (
                    server_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    PRIMARY KEY (server_id, user_id),
                    UNIQUE (server_id, number)
                );
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_server_date
                ON donations (server_id, donation_date);
            """)

            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_server_number
                ON lottery_entries (server_id, number);
            """)

            rows = await conn.fetch("""
                SELECT item_name, is_contributable
                FROM items;
            """)

            global ITEMS
            ITEMS = [
                {"item_name": r["item_name"], "is_contributable": r["is_contributable"]}
                for r in rows
            ]

        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            await self.tree.sync(guild=guild)
            return
        
        if any(g.id == FALLBACK_GUILD_ID for g in self.guilds):
            guild = discord.Object(id=FALLBACK_GUILD_ID)
            await self.tree.sync(guild=guild)
            return

        await self.tree.sync()

bot = Bot()

# -----------------
# Donations
# -----------------

@bot.tree.command(name="donate", description="Donate items")
async def donate(interaction: discord.Interaction, item: str, quantity: int):
    if quantity <= 0:
        await interaction.response.send_message("Quantity must be positive.", ephemeral=True)
        return

    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    now = datetime.now(timezone.utc)
    
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, server_id, user_id, item, quantity, now, False)

    await interaction.response.send_message(
        f"{interaction.user.display_name} donated {quantity} {item}"
    )

@donate.autocomplete("item")
async def donate_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=i["item_name"], value=i["item_name"])
        for i in ITEMS
        if i["is_contributable"] and current.lower() in i["item_name"].lower()
    ][:25]

@bot.tree.command(name="balance_items", description="Balance items in inventory")
@app_commands.checks.has_permissions(administrator=True)
async def balance_items(interaction: discord.Interaction, item: str, quantity: int):
    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    now = datetime.now(timezone.utc)
    
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, server_id, user_id, item, quantity, now, True)

        row = await conn.fetchrow("""
            SELECT SUM(quantity) AS total_items
            FROM donations
            WHERE server_id = $1 AND item = $2
        """, server_id, item)

    total_items = row["total_items"] or 0

    await interaction.response.send_message(
        f"adjusted {item} by {quantity}, current inventory {total_items}",
        ephemeral=True
    )

@balance_items.autocomplete("item")
async def balance_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=i["item_name"], value=i["item_name"])
        for i in ITEMS
        if i["is_contributable"] and current.lower() in i["item_name"].lower()
    ][:25]

@bot.tree.command(name="toggle_items", description="Toggle donatable items")
@app_commands.checks.has_permissions(administrator=True)
async def toggle_items(interaction: discord.Interaction, item: str):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE items
            SET is_contributable = NOT is_contributable
            WHERE item_name = $1
            RETURNING is_contributable
        """, item)

        if row:
            rows = await conn.fetch("SELECT item_name, is_contributable FROM items;")
            global ITEMS
            ITEMS = [
                {"item_name": r["item_name"], "is_contributable": r["is_contributable"]}
                for r in rows
            ]

    if row is None:
        await interaction.response.send_message(f"Item '{item}' not found.", ephemeral=True)
        return

    state = row["is_contributable"]
    msg = (
        f"Item {item} is now valid for donations."
        if state else
        f"Item {item} is no longer valid for donations."
    )

    await interaction.response.send_message(msg, ephemeral=True)

@toggle_items.autocomplete("item")
async def toggle_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=i["item_name"], value=i["item_name"])
        for i in ITEMS
        if current.lower() in i["item_name"].lower()
    ][:25]

# -----------------
# Reports
# -----------------

@bot.tree.command(name="report_user", description="View donation report")
async def report_user(interaction: discord.Interaction, start_date: str = None, end_date: str = None):
    server_id = str(interaction.guild.id)

    query = """
        SELECT user_id, item, SUM(quantity) AS total_quantity
        FROM donations
        WHERE server_id = $1 AND is_adjustment = FALSE
    """

    params = [server_id]
    idx = 2

    if start_date:
        start_date = parse_date(start_date)
        query += f" AND donation_date >= ${idx}"
        params.append(start_date)
        idx += 1

    if end_date:
        end_date = parse_date(end_date)
        query += f" AND donation_date <= ${idx}"
        params.append(end_date)
        idx += 1

    query += " GROUP BY user_id, item ORDER BY user_id, item"

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        await interaction.response.send_message("No donations in that range.", ephemeral=True)
        return

    lines = ["**Donation Report:**"]
    for r in rows:
        lines.append(f"{r['user_id']} donated: {r['total_quantity']} of {r['item']}")

    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="inventory", description="View inventory report")
async def inventory_report(interaction: discord.Interaction, start_date: str = None, end_date: str = None):
    server_id = str(interaction.guild.id)

    query = """
        SELECT item, SUM(quantity) AS total_quantity
        FROM donations
        WHERE server_id = $1
    """

    params = [server_id]
    idx = 2

    if start_date:
        start_date = parse_date(start_date)
        query += f" AND donation_date >= ${idx}"
        params.append(start_date)
        idx += 1

    if end_date:
        end_date = parse_date(end_date)
        query += f" AND donation_date <= ${idx}"
        params.append(end_date)
        idx += 1

    query += " GROUP BY item ORDER BY item"

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        await interaction.response.send_message("No inventory in that range.", ephemeral=True)
        return

    lines = ["**Inventory Report:**"]
    for r in rows:
        lines.append(f"{r['item']}: {r['total_quantity'] or 0}")

    await interaction.response.send_message("\n".join(lines))

# -----------------
# Help / Ping
# -----------------

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

@bot.tree.command(name="help", description="Show available commands")
async def help_command(interaction: discord.Interaction):
    help_text = """
**Available Commands**

🪵 **Donations**
/donate <item> <quantity>

/report [start_date] [end_date]

🎟️ **Lottery**
/lottery <number>
/lottery_draw
/lottery_reset

⚙️ **Utility**
/ping
/help
"""
    await interaction.response.send_message(help_text, ephemeral=True)

# -----------------
# Lottery
# -----------------

@bot.tree.command(name="lottery_reset", description="Reset the current lottery")
@app_commands.checks.has_permissions(administrator=True)
async def lottery_reset(interaction: discord.Interaction):
    server_id = str(interaction.guild.id)

    async with bot.pool.acquire() as conn:
        await conn.execute("DELETE FROM lottery_entries WHERE server_id = $1", server_id)

    await interaction.response.send_message("Lottery reset.", ephemeral=True)

@bot.tree.command(name="lottery", description="Enter the lottery (1-50)")
async def lottery(interaction: discord.Interaction, number: int):
    if number < 1 or number > 50:
        await interaction.response.send_message("Pick a number between 1 and 50.", ephemeral=True)
        return

    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)

    async with bot.pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO lottery_entries (server_id, user_id, number)
                VALUES ($1, $2, $3)
            """, server_id, user_id, number)
        except Exception:
            await interaction.response.send_message(
                "You have already entered or that number is taken.",
                ephemeral=True
            )
            return

    await interaction.response.send_message(
        f"{interaction.user.mention} entered with number {number} 🎟️"
    )

@bot.tree.command(name="lottery_draw", description="Draw the lottery winner")
@app_commands.checks.has_permissions(administrator=True)
async def lottery_draw(interaction: discord.Interaction):
    server_id = str(interaction.guild.id)

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, number FROM lottery_entries WHERE server_id = $1",
            server_id
        )

        await conn.execute("DELETE FROM lottery_entries WHERE server_id = $1", server_id)

    if not rows:
        await interaction.response.send_message("No lottery entries yet.", ephemeral=True)
        return

    winning_number = random.randint(1, 50)
    winner = next((r['user_id'] for r in rows if r['number'] == winning_number), None)

    if winner:
        await interaction.response.send_message(
            f"Winning number: {winning_number}\n<@{winner}> wins!"
        )
    else:
        await interaction.response.send_message(
            f"Winning number: {winning_number}\nNo winner this time!"
        )

bot.run(os.environ["DISCORD_TOKEN"])