import os
import discord
import asyncpg
from rapidfuzz import process
import re
from datetime import datetime, timezone
TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

INPUT_CHANNEL_ID = 1472226231830450261   # channel users type in
OUTPUT_CHANNEL_ID = 1473069420548198544  # where guesses get posted

ITEMS = []
pool = None

# Regex and pattern match
line_pattern = re.compile(r"^([+\-~])\s*(\d+)\s+(.+)$")

def parse_line(line: str):
    match = line_pattern.match(line.strip())
    if not match:
        return None

    sign, qty, item_text = match.groups()
    qty = int(qty)

    if sign == "-":
        qty = -qty

    return sign, qty, item_text.strip()


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

    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL)
        await load_items()
        print(f"Loaded {len(ITEMS)} items")


async def handle_db(symbol, qty, item_name,  message: discord.Message, conn):
    server_id = str(message.guild.id)
    if symbol == "~":
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET quantity = EXCLUDED.quantity
        """, server_id, item_name, qty)

    else:
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET quantity = inventory.quantity + EXCLUDED.quantity
        """, server_id, item_name, qty)
    if symbol != "~":
        now = datetime.now(timezone.utc)
        await conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, server_id, message.author.id, item_name, qty, now, False)




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

        for line in lines:
            parsed = parse_line(line)
            if not parsed:
                results.append(f"`{line}` → ❌ Invalid format")
                continue

            symbol, qty, item_text = parsed
            if symbol == "~":
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
    


bot.run(TOKEN)