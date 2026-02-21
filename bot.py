import os
import discord
import asyncpg
from rapidfuzz import process

TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

INPUT_CHANNEL_ID = 1472226231830450261   # channel users type in
OUTPUT_CHANNEL_ID = 1473069420548198544  # where guesses get posted

ITEMS = []
pool = None


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

    for line in lines:
        clean = line.strip()
        if not clean:
            continue

        guess = guess_item(clean)
        if guess:
            results.append(f"`{clean}` → **{guess}**")
        else:
            results.append(f"`{clean}` → ❌ No match")

    if results:
        await output_channel.send(
            f"Guesses from {message.author.mention}:\n" +
            "\n".join(results)
        )


bot.run(TOKEN)