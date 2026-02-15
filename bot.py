import os
import random
import psycopg
import psycopg.errors
import asyncpg
import discord
from discord import app_commands
from datetime import datetime, timezone

DATABASE_URL = os.environ["DATABASE_URL"]
DEV_GUILD_ID = os.getenv("DEV_SERVER_ID")
FALLBACK_GUILD_ID = 1466549361432461436

def get_connection():
    conn = psycopg.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


with get_connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS donations (
                id SERIAL PRIMARY KEY,
                server_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                item TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                donation_date TIMESTAMPTZ NOT NULL,
                is_adjustment NOT NULL`
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT item_name, is_contributable FROM items;
            """)
            ITEMS = [{"item_name": row[0], "is_contributable": row[1]} for row in cur.fetchall()]
            #access these as ITEMS[0]["item name"] == 'blah'

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
            
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            await self.tree.sync(guild=guild)
            print("Synced to DEV_SERVER_ID")
            return
        
        if any(g.id == FALLBACK_GUILD_ID for g in self.guilds):
            guild = discord.Object(id=FALLBACK_GUILD_ID)
            await self.tree.sync(guild=guild)
            print("Synced to fallback specific server")
            return

        await self.tree.sync()
        print("Synced globally")

bot = Bot()

# with open("items_list.json", "r", encoding=("utf-8")) as f:
#     ITEMS = json.load(f)

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
    
    async with bot.pool.acquire() as conn:
        await conn.execute("""
                INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
                VALUES ($1, $2, $3, $4, $5, $6)""", server_id, user_id, item, quantity, now, False)

        await interaction.response.send_message(
            f"{interaction.user.display_name} donated {quantity} {item}"
        )

@bot.tree.command(name="balance_items", description="Balance items in inventory")
@app_commands.checks.has_permissions(administrator=True)
async def balance_items(
    interaction: discord.Interaction,
    item: str,
    quantity: int
):
    server_id = str(interaction.guild.id)
    user_id = str(interaction.user.id)
    now = datetime.now(timezone.utc)
    
    async with bot.pool.acquire() as conn:
        await conn.execute("""
                INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, server_id, user_id, item, quantity, now, True)

        row = await conn.fetchrow(
            """
            SELECT SUM(quantity) AS total_items
            FROM donations
            WHERE server_id = $1
            AND item = $2
            """,
            server_id,
            item
        )

        total_items = row["total_items"] or 0
            

        await interaction.response.send_message(
            f"adjusted {item} by {quantity}, current inventory {total_items}",
            ephemeral=True
        )

@bot.tree.command(name="toggle_items", description="Toggle donatable items")
@app_commands.checks.has_permissions(administrator=True)
async def toggle_items(
    interaction: discord.Interaction,
    item: str,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Toggle
            cur.execute("""
                UPDATE items
                SET is_contributable = NOT is_contributable
                WHERE item_name = %s
                RETURNING is_contributable
            """, (item,))

            result = cur.fetchone()

    if result is None:
        await interaction.response.send_message(
            f"Item '{item}' not found.",
            ephemeral=True
        )
        return

    item_state = result[0]

    if item_state:
        message = f"Item {item} is now valid for donations."
    else:
        message = f"Item {item} is no longer valid for donations."

    await interaction.response.send_message(message, ephemeral=True)

@donate.autocomplete("item")
async def donate_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item[0], value=item[0])
        for item in ITEMS
        if item[1]  # is_contributable == True
        and current.lower() in item[0].lower()
    ][:25]

@balance_items.autocomplete("item")
async def balance_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item[0], value=item[0])
        for item in ITEMS
        if item[1]  # is_contributable == True
        and current.lower() in item[0].lower()
    ][:25]

@toggle_items.autocomplete("item")
async def toggle_item_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=item[0], value=item[0])
        for item in ITEMS
        if current.lower() in item[0].lower()
    ][:25]


@bot.tree.command(name="report_user", description="View donation report")
async def report_user(
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
        WHERE server_id = $1
          AND is_adjustment = FALSE
    """

    params = [server_id]
    param_index = 2  # because $1 is already used

    if start_date:
        try:
            start_date = parse_date(start_date)
        except ValueError:
            await interaction.response.send_message(
                "Invalid date format. Use YYYY-MM-DD.",
                ephemeral=True
            )
            return

        query += f" AND donation_date >= ${param_index}"
        params.append(start_date)
        param_index += 1

    if end_date:
        try:
            end_date = parse_date(end_date)
        except ValueError:
            await interaction.response.send_message(
                "Invalid date format. Use YYYY-MM-DD.",
                ephemeral=True
            )
            return

        query += f" AND donation_date <= ${param_index}"
        params.append(end_date)
        param_index += 1

    query += """
        GROUP BY user_id, item
        ORDER BY user_id, item
    """

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        await interaction.response.send_message(
            "No donations in that range.",
            ephemeral=True
        )
        return

    lines = ["**Donation Report:**"]

    for row in rows:
        user_id = row["user_id"]
        item = row["item"]
        qty = row["total_quantity"]

        try:
            user = await bot.fetch_user(int(user_id))
            name = user.display_name
        except:
            name = user_id

        lines.append(f"{name} donated: {qty} of {item}")

    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="inventory", description="View inventory report")
async def inventory_report(
    interaction: discord.Interaction,
    start_date: str = None,
    end_date: str = None
):
    server_id = str(interaction.guild.id)

    query = """
        SELECT
            item,
            SUM(quantity) AS total_quantity
        FROM donations
        WHERE server_id = $1
    """

    params = [server_id]
    param_index = 2

    if start_date:
        try:
            start_date = parse_date(start_date)
        except ValueError:
            await interaction.response.send_message(
                "Invalid date format. Use YYYY-MM-DD.",
                ephemeral=True
            )
            return

        query += f" AND donation_date >= ${param_index}"
        params.append(start_date)
        param_index += 1

    if end_date:
        try:
            end_date = parse_date(end_date)
        except ValueError:
            await interaction.response.send_message(
                "Invalid date format. Use YYYY-MM-DD.",
                ephemeral=True
            )
            return

        query += f" AND donation_date <= ${param_index}"
        params.append(end_date)
        param_index += 1

    query += """
        GROUP BY item
        ORDER BY item
    """

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        await interaction.response.send_message(
            "No inventory in that range.",
            ephemeral=True
        )
        return

    lines = ["**Inventory Report:**"]

    for row in rows:
        item = row["item"]
        qty = row["total_quantity"] or 0
        lines.append(f"{item}: {qty}")

    await interaction.response.send_message("\n".join(lines))
    
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

    with get_connection() as conn:
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
        with get_connection() as conn:
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

    with get_connection() as conn:
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
    with get_connection() as conn:
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