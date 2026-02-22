import os
import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
import json
from rapidfuzz import process
import re
from datetime import datetime, timezone, timedelta
import aiohttp

TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

BASED_URL = "https://api.awakening.wiki/items"
LIMIT = 1000
MAX_PAGES = 4

ITEMS = []
pool = None

# Regex and pattern match
line_pattern_qty = re.compile(r"^([+\-$~])\s*(\d+)\s+(.+)$")

async def fetch_all_items():
    offset = 0
    all_items = []
    pages = 0
    async with aiohttp.ClientSession() as session:
        while pages<MAX_PAGES:
            params = {
                "limit": LIMIT,
                "offset": offset,
                "shuffle": 0,
                "fields": "Id,name,item_tags,short_description"
            }

            async with session.get(BASED_URL, params=params) as resp:
                print(resp.status, resp.url)
                resp.raise_for_status()
                data = await resp.json()

            items = data["list"]
            if not items:
                break

            all_items.extend(items)

            if len(items) < LIMIT:
                break

            offset += LIMIT
            pages += 1

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
    return None


# ---------- DB LOAD ----------

async def load_items():
    global ITEMS
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT item_name
            FROM items_new
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

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        #self.tree = app_commands.CommandTree(self)
        self.bot_settings = {}

    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL)
        bot.pool = pool
        await load_items()
        await self.load_extension("report_commands")
        await self.tree.sync()
        print("Bot ready")

    async def on_ready(self):
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bot_settings")
            self.bot_settings = {
                row["server_id"]: {
                    "input": row["donation_input_channel"],
                    "output": row["donation_output_channel"]
                }
                for row in rows
            }

    async def update_scoreboard(self, ctx, conn):
        guild = str(ctx.guild)
        if guild is None:
            return
        server_id = guild.id

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
                    SUM(d.quantity * i.donation_value) AS total_value
                    FROM donations d
                    JOIN donation_values i ON d.item = i.item_name
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
                    member = await ctx.guild.fetch_member(user_id)
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
        channel_name = self.bot_settings.get("scoreboard_channel")
        message_name = self.bot_settings.get("scoreboard_message")

        if not channel_name:
            # No scoreboard configured
            return

        channel = self.get_channel(int(channel_name))

        if message_name is None:
            # Channel set but message not created yet
            message = await channel.send(content)

            async with self.pool.acquire() as conn:
                await conn.execute("""
                    UPDATE bot_settings
                    SET scoreboard_message = $1
                    WHERE server_id = $2
                """, str(message.id), server_id)
        else:
            try:
                message = await channel.fetch_message(int(message_name))
                await message.edit(content=content)
            except:
                # Message deleted manually, recreate
                message = await channel.send(content)

                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE bot_settings
                        SET scoreboard_message = $1
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
            INSERT INTO donation_values (server_id, item_name, donation_value)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET donation_value = EXCLUDED.donation_value
        """, int(server_id), item_name, qty)
    
    elif symbol == "!":
        pass

#Discord Operations requiring @bot
bot = Bot()

#process messages in donation channel
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    
    settings = bot.bot_settings.get(message.guild.id)
    if not settings:
        return

    if message.channel.id != settings["input"]:
        return

    output_channel = bot.get_channel(settings["output"])
    if not output_channel:
        return

    lines = message.content.splitlines()

    results = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for line in lines:
                parsed = parse_line(line)
                if not parsed:
                    results.append(f"`{line}` → ❌ Invalid format, use like this: `+90 Iron Ingot`")
                    continue

                symbol, qty, item_text = parsed
                if symbol in ("~", "$", "!"):
                    if not message.author.guild_permissions.administrator:
                        results.append("❌ You are not allowed to use ~")
                        continue

                guess = guess_item(item_text)

                if guess:
                    if symbol == "+":
                        results.append(f"Donated {abs(qty)} **{guess}**")
                    elif symbol == "-":
                        results.append(f"Withdrew {abs(qty)} **{guess}**")
                    elif symbol == "~":
                        results.append(f"Adjusted total quantity of **{guess}** to {qty}")
                    elif symbol == "$":
                        results.append(f"Set donation value of **{guess}** to {qty}")
                    await handle_db(symbol, qty, guess, message, conn)
                    bot.update_scoreboard(message, conn)
                else:
                    results.append(f"{qty:+} `{item_text}` → ❌ No match")
    
    if results:
        await output_channel.send(
            f"Recorded the following Transactions from {message.author.mention}:\n" +
            "\n".join(results)
        )
    await bot.process_commands(message)

@bot.tree.command(name="set_scoreboard_channel",description="Set the channel where the weekly scoreboard will be posted")
@app_commands.checks.has_permissions(administrator=True)
async def set_scoreboard_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    server_id = interaction.guild.id

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (server_id, scoreboard_channel, scoreboard_message)
            VALUES ($1, $2, NULL)
            ON CONFLICT (server_id)
            DO UPDATE SET
                scoreboard_channel = EXCLUDED.scoreboard_channel,
                scoreboard_message = NULL
        """, server_id, str(channel.id))

    await interaction.response.send_message(
        f"Scoreboard channel set to {channel.mention}. "
        "A new scoreboard message will be created on the next update.",
        ephemeral=True
    )

@bot.tree.command(name="set_donation_ichannel", description="set the input channel for donations")
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

@bot.tree.command(name="set_donation_ochannel", description="set the output channel for donations")
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


@bot.tree.command(name="sync_items", description="collect item list from dune wiki (use sparingly)")
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
        if not item.get("name"):
            continue  # skip broken wiki entries

        tags = item.get("item_tags")
        parsed = json.loads(tags) if isinstance(tags, str) else []

        records.append((
            item["Id"],
            item["name"],
            item.get("short_description"),
            json.dumps(parsed)
        ))

        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE TABLE items_new;")
            await conn.copy_records_to_table(
                "items_new",
                records=records,
                columns=["id", "item_name", "short_description", "item_tags"]
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

# CREATE TABLE donation_values(
#     server_id BIGINT NOT NULL,
#     id INT NOT NULL,
#     item_name TEXT NOT NULL,
#     donation_value NUMERIC(12,2),
#     PRIMARY KEY (server_id, id)
# )