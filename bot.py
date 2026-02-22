import os
import discord
from discord import app_commands
import asyncpg
import json
from rapidfuzz import process
import re
from datetime import datetime, timezone, timedelta
import aiohttp


TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

INPUT_CHANNEL_ID = 1472226231830450261   # channel users type in
OUTPUT_CHANNEL_ID = 1473069420548198544  # where guesses get posted
BASE_URL = "https://api.awakening.wiki/items"
LIMIT = 1000

ITEMS = []
pool = None

# Regex and pattern match
line_pattern_qty = re.compile(r"^([+\-$])\s*(\d+)\s+(.+)$")
line_pattern_toggle = re.compile(r"^(!)\s+(.+)$")



async def fetch_all_items():
    offset = 0
    all_items = []

    async with aiohttp.ClientSession() as session:
        while True:
            params = {
                "limit": LIMIT,
                "offset": offset,
                "shuffle": 0,
                "fields": "Id,name,item_tags,short_description"
            }

            async with session.get(BASE_URL, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if not data["list"]:
                break

            all_items.extend(data["list"])

            if len(data) < LIMIT:
                break

            offset += LIMIT

    return all_items



def parse_line(line: str):
    line = line.strip()

    # Quantity operations
    match = line_pattern_qty.match(line)
    if match:
        symbol, qty, item_text = match.groups()
        qty = int(qty)

        if symbol == "-":
            qty = -qty

        return symbol, qty, item_text.strip()

    # Toggle operation
    match = line_pattern_toggle.match(line)
    if match:
        symbol, item_text = match.groups()
        return symbol, None, item_text.strip()

    return None


# ---------- DB LOAD ----------

async def load_items():
    global ITEMS
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT item_name
            FROM items
            WHERE is_contributable = TRUE
        """)
        ITEMS = [row["item_name"] for row in rows]


# ---------- FUZZY MATCH ----------

def guess_item(user_input: str):
    if not ITEMS:
        return None

    match = process.extractOne(user_input, ITEMS)
    if match:
        best, score, _ = match
        if score > 65:   # tune this
            return best
    return None


# ---------- DISCORD ----------

class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL)
        await load_items()
        await self.tree.sync()
        print("Bot ready")

    async def update_scoreboard(self, interaction: discord.Interaction):
        server_id = str(interaction.guild.id)

        # --- Calculate start of current Tuesday ---
        now = datetime.now(timezone.utc)
        days_since_tuesday = (now.weekday() - 1) % 7
        period_start = (now - timedelta(days=days_since_tuesday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        # --- Get totals ---
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT d.user_id,
                    SUM(d.quantity * i.donate_value) AS total_value
                    FROM donations d
                    JOIN items i ON d.item = i.item_name
                    WHERE d.server_id = $1
                        AND d.donation_date >= $2
                        AND NOT d.is_adjustment
                    GROUP BY d.user_id
                    ORDER BY total_value DESC;
            """, server_id, period_start)

        # --- Build content ---
        if not rows:
            content = "🏆 **Weekly Scoreboard (since Tuesday)**\n\nNo donations yet."
        else:
            lines = ["🏆 **Weekly Scoreboard (since Tuesday)**\n"]

            for rank, row in enumerate(rows, start=1):
                user_id = int(row["user_id"])
                total = row["total_value"]

                try:
                    member = await interaction.guild.fetch_member(user_id)
                    name = member.display_name
                except:
                    name = str(user_id)

                if rank == 1:
                    lines.append(f"🏅{rank}. {name} — {total}")
                elif rank == 2:
                    lines.append(f"🥈{rank}. {name} — {total}")
                elif rank == 3:
                    lines.append(f"🥉{rank}. {name} — {total}")
                else:
                    lines.append(f"{rank}. {name} — {total}")

            content = "\n".join(lines)

        # --- Check for existing scoreboard message ---
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow("""
                SELECT channel_id, message_id
                FROM scoreboard_messages
                WHERE server_id = $1
            """, server_id)

        if not existing:
            # No scoreboard configured
            return

        channel = self.get_channel(int(existing["channel_id"]))

        if existing["message_id"] is None:
            # Channel set but message not created yet
            message = await channel.send(content)

            async with self.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE scoreboard_messages
                    SET message_id = $1
                    WHERE server_id = $2
                """, str(message.id), server_id)
        else:
            try:
                message = await channel.fetch_message(int(existing["message_id"]))
                await message.edit(content=content)
            except:
                # Message deleted manually, recreate
                message = await channel.send(content)

                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE scoreboard_messages
                        SET message_id = $1
                        WHERE server_id = $2
                    """, str(message.id), server_id)

async def handle_db(symbol, qty, item_name,  message: discord.Message, conn):
    server_id = str(message.guild.id)
    
    if symbol in ("+", "-"):
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET quantity = inventory.quantity + EXCLUDED.quantity
        """, server_id, item_name, qty)
        now = datetime.now(timezone.utc)
        await conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, server_id, message.author.id, item_name, qty, now, False)

    elif symbol == "~":
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET quantity = EXCLUDED.quantity
        """, server_id, item_name, qty)

    elif symbol == "$":
        await conn.execute("""
            INSERT INTO items (item_name, donate_value)
            VALUES ($1, $2)
            ON CONFLICT (item_name)
            DO UPDATE SET donate_value = EXCLUDED.donate_value
        """, item_name, qty)
    
    elif symbol == "!":
        await conn.execute("""
            INSERT INTO items (item_name, is_contributable)
            VALUES ($1, TRUE)
            ON CONFLICT (item_name)
            DO UPDATE SET is_contributable = NOT EXCLUDED.is_contributable
        """, item_name)





bot = Bot()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.channel.id != INPUT_CHANNEL_ID:
        return

    output_channel = bot.get_channel(OUTPUT_CHANNEL_ID)
    if not output_channel:
        return

    lines = message.content.splitlines()

    results = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for line in lines:
                parsed = parse_line(line)
                if not parsed:
                    results.append(f"`{line}` → ❌ Invalid format, use like '+90 Iron Ingot'")
                    continue

                symbol, qty, item_text = parsed
                if symbol in ("~", "$", "!"):
                    if not message.author.guild_permissions.administrator:
                        results.append("❌ You are not allowed to use ~")
                        continue

                guess = guess_item(item_text)

                if guess:
                    results.append(f"{qty:+} **{guess}**")
                    await handle_db(symbol, qty, guess, message, conn)
                else:
                    results.append(f"{qty:+} `{item_text}` → ❌ No match")

    # for result in results:
    #      await output_channel.send(f"adding {parse_line(result)}")
        
    if results:
        await output_channel.send(
            f"Donations from {message.author.mention}:\n" +
            "\n".join(results)
        )
@bot.tree.command(name="set_donation_ichannel")
@app_commands.describe(channel="Channel where users submit donations")
async def set_donation_ichannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be an admin to use this.",
            ephemeral=True
        )
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (server_id, donation_input_channel)
            VALUES ($1, $2)
            ON CONFLICT (server_id)
            DO UPDATE SET donation_input_channel = EXCLUDED.donation_input_channel
        """,
            interaction.guild.id,
            channel.id
        )

    await interaction.response.send_message(
        f"Donation input channel set to {channel.mention}",
        ephemeral=True
    )

@bot.tree.command(name="set_donation_ochannel")
@app_commands.describe(channel="Channel where guesses are posted")
async def set_donation_ochannel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be an admin to use this.",
            ephemeral=True
        )
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (server_id, donation_output_channel)
            VALUES ($1, $2)
            ON CONFLICT (server_id)
            DO UPDATE SET donation_output_channel = EXCLUDED.donation_output_channel
        """,
            interaction.guild.id,
            channel.id
        )

    await interaction.response.send_message(
        f"Donation output channel set to {channel.mention}",
        ephemeral=True
    )

#    
import json

@bot.tree.command(name="sync_items")
async def sync_items(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You must be an admin to use this.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    items = await fetch_all_items()
    records = []

    for item in items:
        tags = item.get("item_tags")
        parsed = json.loads(tags) if isinstance(tags, str) else []
        records.append((
            item["Id"],
            item["name"],
            item["short_description"],
            json.dumps(parsed)  # <-- THIS is the important bit
        ))

        async with pool.acquire() as conn:
            await conn.copy_records_to_table(
                "items_new",
                records=records,
                columns=["id", "name", "short_description", "item_tags"]
            )

    await interaction.followup.send(f"Fetched {len(records)} items.")


bot.run(TOKEN)





# create table bot_settings
# (
#     server_id TEXT,
#     donation_input_channel TEXT,
#     donation_output_channel TEXT,
#     UNIQUE(server_id, donation_input_channel),
#     UNIQUE(server_id, donation_output_channel)
# )