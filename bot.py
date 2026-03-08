import os
import discord
import random
from discord import app_commands
from discord.ext import commands
import asyncpg
import json
from rapidfuzz import process
import re
from datetime import datetime, timezone, timedelta
import aiohttp
import logging
from decimal import Decimal
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scores = []
background_col = (32, 34, 37)
text_frame_col = (132, 134, 137)
text_col = (47, 49, 54)
orange_col = (255,133,27)
font_height = 35
img = Image.new("RGB", (698, 873), background_col)
draw = ImageDraw.Draw(img)
font =ImageFont.truetype('Dune_Rise.ttf',font_height)
bold_font =ImageFont.truetype('Dune_Rise.ttf',font_height)
emoji_font = ImageFont.truetype('NotoColorEmoji-Regular.ttf', 109)
draw.rectangle((0, 0, 698, 50), fill=(47, 49, 54))
image_offset = 20

text_index = 0
previous_ypos = 1

top_margin = 100   # space for header
row_height = 60

TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

BASED_URL = "https://api.awakening.wiki/items"
LIMIT = 1000
MAX_PAGES = 4

ITEMS = []
pool = None

# Regex and pattern match
line_pattern_qty = re.compile(r"^([%<+\-~])\s*(\d+)\s+(.+)$")
line_pattern_value = re.compile(r"^(\$)\s*(\d+(?:\.\d+)?)\s+(.+)$")
line_pattern_toggle = re.compile(r"^%\s+(.+)$")
line_pattern_add = re.compile(r"^\?\s*(.+)$")


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
                logging.info(resp.status, resp.url)
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

    match = line_pattern_qty.match(line)
    toggle_match = line_pattern_toggle.match(line)
    add_item = line_pattern_add.match(line)
    value_match = line_pattern_value.match(line)

    if toggle_match:
        return "%", 0, toggle_match.group(1).strip()
    if add_item:
        return "?", 0, add_item.group(1).strip()
    if value_match:
        symbol, qty, item_text = value_match.groups()
        return symbol, Decimal(qty), item_text.strip()

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
           SELECT item_name as item_name
            FROM items_new
            WHERE short_description <> 'TBD'
            AND EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(item_tags) AS tag
                WHERE tag LIKE 'Items.CraftedResources%'
                OR tag LIKE 'Items.RefinedResources%'
                OR tag LIKE 'Items.RawResources%'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(item_tags) AS tag
                WHERE tag = 'Items.Consumables.BuildableSets'
            )
            UNION 
            select item_name from extra_items
        """)
        ITEMS = [row["item_name"] for row in rows]


# ---------- FUZZY MATCH ----------

def guess_item(user_input: str):
    if not ITEMS:
        return None

    match = process.extractOne(user_input, ITEMS)
    if match:
        best, score, _ = match
        if score > 90:   # tune this
            return best
    return None


# ---------- DISCORD ----------

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        #self.tree = app_commands.CommandTree(self)
        self.bot_settings = {}

    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL,statement_cache_size=0)
        bot.pool = pool
        await load_items()
        await self.load_extension("report_commands")
        await self.tree.sync()
        logger.info("Bot ready")

    async def on_ready(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM bot_settings")
            self.bot_settings = {
                row["server_id"]: {
                    "input": row["donation_input_channel"],
                    "output": row["donation_output_channel"],
                    "scoreboard_channel": row["scoreboard_channel"],
                    "scoreboard_message": row["scoreboard_message"],
                    "gambling_channel": row["gambling_channel"]
                }
                for row in rows
            }
            logging.info("Loaded bot_settings:", self.bot_settings)

    async def aupdate_scoreboard(self, ctx, conn):
        guild = ctx.guild
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
    
        rows = await conn.fetch("""
            SELECT d.user_id,
                SUM(d.quantity * i.donation_value) AS total_value
                FROM donations d
                JOIN donation_values i ON d.item = i.item_name
                WHERE d.server_id = $1
                    AND d.donation_date >= $2
                    AND NOT d.is_adjustment
                    AND i.server_id = $3
                GROUP BY d.user_id
                ORDER BY total_value DESC;
        """, server_id, period_start, server_id)

        # --- Build content ---
        if not rows:
            content = "🏆 **Weekly Scoreboard (since Tuesday)**\n\nNo donations yet."
        else:
            lines = [f"🏆 **Weekly Scoreboard (since Tuesday)** -updated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}UTC\n"]

            for rank, row in enumerate(rows, start=1):
                user_id = int(row["user_id"])
                total = row["total_value"]

                try:
                    member = guild.get_member(user_id)
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
        settings = self.bot_settings.get(server_id)
        if not settings:
            return
        channel_name = settings.get("scoreboard_channel")
        message_name = settings.get("scoreboard_message")

        if not channel_name:
            # No scoreboard configured
            logging.info("no channel_name")
            return

        channel = self.get_channel(int(channel_name))
        if not channel:
            logging.info("no channel")
            return

        if message_name is None:
            # Channel set but message not created yet
            message = await channel.send(content)


            await conn.execute("""
                UPDATE bot_settings
                SET scoreboard_message = $1
                WHERE server_id = $2
            """, message.id, server_id)
            self.bot_settings[server_id]["scoreboard_message"] = message.id
        else:
            try:
                message = await channel.fetch_message(int(message_name))
                await message.edit(content=content)
            except:
                # Message deleted manually, recreate
                message = await channel.send(content)

                await conn.execute("""
                    UPDATE bot_settings
                    SET scoreboard_message = $1
                    WHERE server_id = $2
                """, message.id, server_id)
                self.bot_settings[server_id]["scoreboard_message"] = message.id

    async def update_scoreboard(self, ctx, conn):
        guild = ctx.guild
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
    
        rows = await conn.fetch("""
            SELECT d.user_id,
                SUM(d.quantity * i.donation_value) AS total_value
                FROM donations d
                JOIN donation_values i ON d.item = i.item_name
                WHERE d.server_id = $1
                    AND d.donation_date >= $2
                    AND NOT d.is_adjustment
                    AND i.server_id = $3
                GROUP BY d.user_id
                ORDER BY total_value DESC;
        """, server_id, period_start, server_id)

        # --- Build content ---
        if not rows:
            content = "🏆 **Weekly Scoreboard (since Tuesday)**\n\nNo donations yet."
        else:
            draw.rounded_rectangle((0, 0, 700, top_margin-20), radius=10,fill=orange_col)
            top_text = 'Donation Leaderboard'
            top_text_length = bold_font.getlength(top_text) +20
            draw.text((700-top_text_length, 30),'Donation Leaderboard',fill=text_col,font=bold_font)


            score_offset = 15
            for index, item in enumerate(rows):
                name = guild.get_member(item["user_id"]).display_name
                score = item["total_value"]
                
                y = top_margin + index * row_height  # row height

                # left accent block
                draw.rounded_rectangle((0, y, 50, y + row_height-score_offset), radius=10,fill=background_col)
                
                #name and score background
                draw.rounded_rectangle((110, y, 700, y + row_height-score_offset), radius=10,fill=text_frame_col)

                text_y = y+(row_height-font_height-(score_offset/2))/2
                score_text = f"{int(score)}"
                score_width = font.getlength(score_text)
                score_x = img.width - score_width 
                print(y+row_height-score_offset)
                print(text_y)
                # text
                draw.text(
                    (10, text_y),
                    str(index+1),
                    fill=(222, 225, 220),
                    font=font
                )
                if index==0:
                    with Pilmoji(img) as pilmoji:
                        pilmoji.text((60, y), '🏅', font=emoji_font, embedded_color=True, emoji_scale_factor=0.4)
                    draw.text((120, text_y), name, font=font, fill=text_col)
                elif index==1:
                    with Pilmoji(img) as pilmoji:
                        pilmoji.text((60, y ), '🥈', font=emoji_font, embedded_color=True, emoji_scale_factor=0.4)
                    draw.text((120, text_y), name, font=font, fill=text_col)
                elif index==2:
                    with Pilmoji(img) as pilmoji:
                        pilmoji.text((60, y), '🥉', font=emoji_font, embedded_color=True, emoji_scale_factor=0.4)
                    draw.text((120, text_y), name, font=font, fill=text_col)
                else:
                    draw.text(
                        (120, text_y),
                        str(name),
                        fill=text_col,
                        font=font
                    )
                draw.text(
                    (score_x, text_y),
                    str(score),
                    fill=text_col,
                    font=font
                )
            img.save("leaderboard.png")

        settings = self.bot_settings.get(server_id)
        if not settings:
            return
        channel_name = settings.get("scoreboard_channel")
        message_name = settings.get("scoreboard_message")

        if not channel_name:
            logging.info("no channel_name")
            return

        channel = self.get_channel(int(channel_name))
        if not channel:
            logging.info("no channel")
            return

        if message_name is None:
            message = await channel.send(file=discord.File("leaderboard.png"))
            await conn.execute("""
                UPDATE bot_settings
                SET scoreboard_message = $1
                WHERE server_id = $2
            """, message.id, server_id)
            self.bot_settings[server_id]["scoreboard_message"] = message.id
        else:
            try:
                message = await channel.fetch_message(int(message_name))
                await message.edit(attachments=[discord.File("leaderboard.png")])
            except:
                message = await channel.send(file=discord.File("leaderboard.png"))
                await conn.execute("""
                    UPDATE bot_settings
                    SET scoreboard_message = $1
                    WHERE server_id = $2
                """, message.id, server_id)
                self.bot_settings[server_id]["scoreboard_message"] = message.id




async def handle_db(symbol, qty, item_name, message: discord.Message, conn):
    server_id = message.guild.id
    user_id = message.author.id
    now = datetime.now(timezone.utc)
    
    # -----------------------------
    # + and - (atomic inventory update)
    # -----------------------------
    if symbol in ("+", "-"):

        # Try atomic update (prevents negative inventory)
        row = await conn.fetchrow("""
            UPDATE inventory
            SET quantity = quantity + $3
            WHERE server_id = $1
              AND item_name = $2
              AND quantity + $3 >= 0
            RETURNING quantity
        """, server_id, item_name, qty)

        if row is None:
            # Withdrawal would go negative OR item missing
            if symbol == "-":
                current = await conn.fetchval("""
                    SELECT quantity
                    FROM inventory
                    WHERE server_id = $1
                      AND item_name = $2
                """, server_id, item_name)

                if current is not None:
                    # Clamp to zero
                    await conn.execute("""
                        UPDATE inventory
                        SET quantity = 0
                        WHERE server_id = $1
                          AND item_name = $2
                    """, server_id, item_name)

                    discrepancy_text = (
                        f"{message.author.display_name} attempted withdrawal: "
                        f"{abs(qty)} of {item_name}, {current} in inventory, adjusted to 0"
                    )

                    await conn.execute("""
                        INSERT INTO discrepancies (server_id, discrepancy_text, date_timestamp)
                        VALUES ($1, $2, $3)
                    """, server_id, discrepancy_text, now)

                    qty = -current  # actual deducted amount

                else:
                    # Item not found at all
                    return

            else:
                # If + and no row updated, item likely doesn't exist
                await conn.execute("""
                    INSERT INTO inventory (server_id, item_name, quantity)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (server_id, item_name)
                    DO UPDATE
                    SET quantity = inventory.quantity + EXCLUDED.quantity
                    RETURNING quantity;
                """, server_id, item_name, qty)

        # Record donation/withdrawal
        await conn.execute("""
            INSERT INTO donations (server_id, user_id, item, quantity, donation_date, is_adjustment)
            VALUES ($1, $2, $3, $4, $5, FALSE)
        """, server_id, user_id, item_name, qty, now)

    # -----------------------------
    # ~ (absolute set inventory)
    # -----------------------------
    elif symbol == "~":
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET quantity = EXCLUDED.quantity
        """, server_id, item_name, qty)

    # -----------------------------
    # $ (set donation value)
    # -----------------------------
    
    elif symbol == "$":
        
        await conn.execute("""
            INSERT INTO donation_values (server_id, item_name, donation_value)
            VALUES ($1, $2, NULLIF($3, 0::numeric))
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET donation_value = NULLIF(EXCLUDED.donation_value, 0);
        """, server_id, item_name, qty)
      
    # -----------------------------
    # < (set required quantity)
    # -----------------------------
    elif symbol == "<":
        await conn.execute("""
            INSERT INTO required_items (server_id, item_name, required_quantity)
            VALUES ($1, $2, $3)
            ON CONFLICT (server_id, item_name)
            DO UPDATE SET required_quantity = EXCLUDED.required_quantity
        """, server_id, item_name, qty)

    # -----------------------------
    # % (set to show in inventory)
    # -----------------------------
    elif symbol == "%":
        await conn.execute("""
            INSERT INTO inventory (server_id, item_name, show_in_report)
        VALUES ($1, $2, TRUE)
        ON CONFLICT (server_id, item_name)
        DO UPDATE
        SET show_in_report = NOT COALESCE(inventory.show_in_report, FALSE)
        """, server_id, item_name)
    # -----------------------------
    # Add Missing Items
    # -----------------------------
    elif symbol == "?":
        await conn.execute("""
        INSERT INTO extra_items (item_name, short_description, item_tags, created_at)
        VALUES ($1,$2,$3,NOW())
        ON CONFLICT (item_name) DO NOTHING
        """,item_name,f"{item_name} added as missing", json.dumps(["Items.AddedByGuild"]))
                         



#Discord Operations requiring @bot
bot = Bot()

#process messages in donation channel
@bot.event
async def on_message(message: discord.Message):
    
    if message.author.bot:
        return
    
    settings = bot.bot_settings.get((message.guild.id))
    if not settings:
        logging.info("no settings loaded")
        return

    input_channel = settings.get("input")
    if not input_channel:
        logging.info(f"No input channel???")
        return
    
    if message.channel.id != int(input_channel):
        logging.info("message not in correct channel")
        return

    output_channel = bot.get_channel(int(settings.get("output")))
    if not output_channel:
        logging.info("no output channel")
        return

    lines = message.content.splitlines()
    did_modify = False
    results = []
    failed = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for line in lines:
                parsed = parse_line(line)
                if not parsed:
                    failed.append(f"`{line}` → ❌ Invalid format, use like this: `+10 Plant Fiber`")
                    continue

                symbol, qty, item_text = parsed
                
                if symbol in ("~", "$", "<", "%", "?"):
                    if not message.author.guild_permissions.administrator:
                        failed.append(f"❌ You are not allowed to use admin-only operation : {symbol}")
                        continue
                if symbol == "?":
                    results.append(f"Added **{item_text}** to inventory as missing item")
                    await handle_db(symbol, 0, item_text, message, conn)
                    ITEMS.append(item_text)
                    print(ITEMS)     

                guess = guess_item(item_text.lower())

                if guess:
                    if guess.lower():# == item_text.lower():
                        if symbol == "+":
                            results.append(f"Donated {abs(qty)} **{guess}**")
                        elif symbol == "-":
                            results.append(f"Withdrew {abs(qty)} **{guess}**")
                        elif symbol == "~":
                            results.append(f"Adjusted total quantity of **{guess}** to {qty}")
                        elif symbol == "$":
                            results.append(f"Set donation value of **{guess}** to {qty}")
                        elif symbol == "<":
                            results.append(f"Set required quantity of **{guess}** to {qty}")
                        elif symbol == "%":
                            results.append(f"Toggled **{guess}** visibility in inventory report")
                        await handle_db(symbol, qty, guess.title(), message, conn)
                        did_modify = True
                    else:
                        if symbol != "?":
                            failed.append(f"Entered `{item_text}`, this is not an item match, did you mean `{guess}`? Please resubmit `{symbol}{qty} {guess}` if this is correct")
                else:
                    if symbol != "?":
                        failed.append(f"{qty:+} `{item_text}` → ❌ No match")
    if did_modify:
        async with pool.acquire() as conn:
            await bot.update_scoreboard(message, conn)
    if failed:
        try:
            await message.author.send(
                "The following issues happened with your command:\n\n"
                + "\n".join(failed)
            )
        except discord.Forbidden:
            await output_channel.send(
                f"{message.author.mention} I can't DM you. so here are your transaction issues:\n" +
                "\n".join(failed)
            )
    if results:
        await output_channel.send(
            f"Recorded the following Transactions from {message.author.mention}:\n" +
            "\n".join(results)
        )
    await bot.process_commands(message)

@bot.tree.command(name="show_discrepancies",description="show any transactions that would take inventory below 0")
@app_commands.checks.has_permissions(administrator=True)
async def show_discrepancies(interaction: discord.Interaction, fetch_rows: int = 20):

    await interaction.response.defer(ephemeral=True)

    server_id = interaction.guild.id

    async with bot.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT discrepancy_text, date_timestamp
            FROM discrepancies
            WHERE server_id = $1
            ORDER BY date_timestamp DESC
            LIMIT $2
        """, server_id, fetch_rows)

    if not rows:
        await interaction.followup.send(
            "No discrepancies recorded."
        )
        return

    text_end = "items" if len(rows) != 1 else "item"

    lines = [
        f"{r['discrepancy_text']} — {r['date_timestamp']}"
        for r in rows
    ]

    message = (
        f"Discrepancy report: last {len(rows)} {text_end}\n\n"
        + "\n".join(lines)
    )

    await interaction.followup.send(message)

@bot.tree.command(name="set_scoreboard_channel",description="Set the channel where the weekly scoreboard will be posted")
@app_commands.checks.has_permissions(administrator=True)
async def set_scoreboard_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    await interaction.response.defer(ephemeral=True)
    server_id = interaction.guild.id

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_settings (server_id, scoreboard_channel, scoreboard_message)
            VALUES ($1, $2, NULL)
            ON CONFLICT (server_id)
            DO UPDATE SET
                scoreboard_channel = EXCLUDED.scoreboard_channel,
                scoreboard_message = NULL
        """, server_id, channel.id)
    
    settings = bot.bot_settings.setdefault(server_id, {})
    settings["scoreboard_channel"] = channel.id
    settings["scoreboard_message"] = None
    
    await interaction.followup.send(
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
    server_id = interaction.guild.id
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
    settings = bot.bot_settings.setdefault(server_id, {})
    settings["input"] = channel.id
    
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
    server_id = interaction.guild.id
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

    settings = bot.bot_settings.setdefault(server_id, {})
    settings["output"] = channel.id
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

# # ── Slots / balance helpers ────────────────────────────────────────────────────

# @bot.tree.command(name="set_gambling_channel",description="Set the channel where the degenerates will reside")
# @app_commands.checks.has_permissions(administrator=True)
# async def set_gambling_channel(
#     interaction: discord.Interaction,
#     channel: discord.TextChannel
# ):
#     await interaction.response.defer(ephemeral=True)
#     server_id = interaction.guild.id

#     async with bot.pool.acquire() as conn:
#         await conn.execute("""
#             INSERT INTO bot_settings (server_id, gambling_channel)
#             VALUES ($1, $2)
#             ON CONFLICT (server_id)
#             DO UPDATE SET
#                 gambling_channel = EXCLUDED.gambling_channel
                
#         """, server_id, channel.id)
    
#     settings = bot.bot_settings.setdefault(server_id, {})
#     settings["gambling_channel"] = channel.id
#     await interaction.followup.send(f"Gambling channel set to {channel.mention}",ephemeral=True)   



# SLOT_SYMBOLS   = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣", "🎰"]
# DAILY_AMOUNT   = 200

# # Payout multipliers (applied to bet amount)
# PAYOUTS = {
#     "7️⃣": 200,
#     "💎": 150,
#     "🎰": 100,
#     "🍇": 60,
#     "🍊": 40,
#     "🍋": 30,
#     "🍒": 20,
# }
# # Two-of-a-kind pays 0.5× bet (net: −0.5 × bet)
# TWO_MATCH_MULTIPLIER = 0.5

# async def ensure_balance_table():
#     async with bot.pool.acquire() as conn:
#         await conn.execute("""
#             CREATE TABLE IF NOT EXISTS user_balance (
#                 user_id   BIGINT NOT NULL,
#                 server_id BIGINT NOT NULL,
#                 coins     INTEGER NOT NULL DEFAULT 0,
#                 last_daily TIMESTAMPTZ,
#                 PRIMARY KEY (user_id, server_id)
#             )
#         """)


# async def get_balance(user_id: int, server_id: int) -> int:
#     async with bot.pool.acquire() as conn:
#          # --- Calculate start of current Tuesday ---
#         now = datetime.now(timezone.utc)
#         days_since_tuesday = (now.weekday() - 1) % 7
#         period_start = (now - timedelta(days=days_since_tuesday)).replace(
#             hour=0, minute=0, second=0, microsecond=0
#         )

#         row = await conn.fetchrow("""
#             SELECT d.user_id,
#                 SUM(d.quantity * i.donation_value) AS total_value
#                 FROM donations d
#                 JOIN donation_values i ON d.item = i.item_name
#                 WHERE d.server_id = $1
#                     AND d.user_id = $2
#                     AND d.donation_date >= $3
#                     AND NOT d.is_adjustment
#                     AND i.server_id = $4
#                 GROUP BY d.user_id
#         """, server_id, user_id, period_start, server_id)

#         if row is None:
#             return 0
#         return row["total_value"]


# async def update_balance(user_id: int, server_id: int, delta: int):
#     async with bot.pool.acquire() as conn:
#         await conn.execute("""
#             INSERT INTO donations (user_id, server_id, item, quantity,donation_date,is_adjustment)
#             VALUES ($1, $2, 'Gambling Token', $3,NOW(),FALSE)            
#         """, user_id, server_id, delta)

# async def get_jackpot(server_id:int):
#     async with bot.pool.acquire() as conn:
#         amount = await conn.fetchval("""
#             SELECT jackpot from 
#             slots_jackpot
#             WHERE server_id = $1
#         """, server_id)
#         await conn.execute("""
#             UPDATE slots_jackpot
#             SET jackpot = 0
#             WHERE server_id = $1
#         """, server_id)
#         return amount
    
    

# def spin_reels() -> list[str]:
#     # Weighted: common symbols appear more, rare ones less
#     weights = [30, 25, 20, 15, 5, 3, 2]
#     return random.choices(SLOT_SYMBOLS, weights=weights, k=3)


# def calculate_payout(reels: list[str], bet: int) -> tuple[int, str]:
#     a, b, c = reels
#     if a == b == c:
#         if a== "7️⃣":
#             return 0,"won_jackpot"
#         else:
#             mult = PAYOUTS[a]
#         return bet * mult, f"**MINOR JACKPOT! 3×{a}** — {mult}× payout!"
#     if a == b or b == c or a == c:
#         winnings = int(bet * TWO_MATCH_MULTIPLIER)
#         pair = a if a == b or a == c else b
#         return winnings, f"Two of a kind {pair} — small win!"
#     return 0, "No match. Better luck next time!"


# # ── /balance ───────────────────────────────────────────────────────────────────

# @bot.tree.command(
#     name="balance",
#     description="Check your coin balance"
    
# )
# async def cmd_balance(interaction: discord.Interaction):
#     await interaction.response.defer()
#     channel_id = bot.bot_settings.get(interaction.guild_id, {}).get("gambling_channel")
#     if not channel_id:
#         await interaction.followup.send("No gambling channel set.", ephemeral=True)
#         return
#     output_channel = bot.get_channel(int(channel_id))
#     if not output_channel:
#         await interaction.followup.send("Gambling channel not found.", ephemeral=True)
#         return

#     await ensure_balance_table()
#     coins = await get_balance(interaction.user.id, interaction.guild_id)
#     embed = discord.Embed(
#         title="💰 Balance",
#         description=f"{interaction.user.display_name} has **{coins:,}** coins.",
#         color=discord.Color.gold()
#     )

#     await interaction.followup.send("Done!", ephemeral=True)
#     await output_channel.send(embed=embed)
    
# #    await interaction.response.send_message(embed=embed)


# # ── /daily ─────────────────────────────────────────────────────────────────────

# @bot.tree.command(
#     name="daily",
#     description="Claim your daily coin bonus"
    
# )
# async def cmd_daily(interaction: discord.Interaction):
#     await interaction.response.defer()
#     channel_id = bot.bot_settings.get(interaction.guild_id, {}).get("gambling_channel")
#     if not channel_id:
#         await interaction.followup.send("No gambling channel set.", ephemeral=True)
#         return
#     output_channel = bot.get_channel(int(channel_id))
#     if not output_channel:
#         await interaction.followup.send("Gambling channel not found.", ephemeral=True)
#         return

#     await ensure_balance_table()
#     async with bot.pool.acquire() as conn:
#         row = await conn.fetchrow(
#             "SELECT last_daily FROM user_balance WHERE user_id=$1 AND server_id=$2",
#             interaction.user.id, interaction.guild_id
#         )
#         now = datetime.now(timezone.utc)
#         if row and row["last_daily"]:
#             next_claim = row["last_daily"] + timedelta(hours=24)
#             if now < next_claim:
#                 remaining = next_claim - now
#                 hours, rem = divmod(int(remaining.total_seconds()), 3600)
#                 minutes = rem // 60
#                 embed = discord.Embed(
#                     title="⏳ Daily already claimed",
#                     description=f"Come back in **{hours}h {minutes}m**.",
#                     color=discord.Color.red()
#                 )
#                 await interaction.followup.send(embed=embed, ephemeral=True)
#                 return

#         await conn.execute("""
#             INSERT INTO user_balance (user_id, server_id, coins, last_daily)
#             VALUES ($1, $2, $3, $4)
#             ON CONFLICT (user_id, server_id)
#             DO UPDATE SET coins = user_balance.coins + $3, last_daily = $4
#         """, interaction.user.id, interaction.guild_id, DAILY_AMOUNT, now)
#         await update_balance(interaction.user.id, interaction.guild.id, DAILY_AMOUNT)

#     coins = await get_balance(interaction.user.id, interaction.guild_id)
#     embed = discord.Embed(
#         title="🎁 Daily Bonus",
#         description=f"You claimed **{DAILY_AMOUNT:,}** coins!\nNew balance: **{coins:,}** coins.",
#         color=discord.Color.green()
#     )
#     await interaction.followup.send("Done!", ephemeral=True)

#     await output_channel.send(embed=embed)


# ── /slots ─────────────────────────────────────────────────────────────────────

# @bot.tree.command(
#     name="slots",
#     description="Spin the slot machine and gamble your coins"
    
# )
# @app_commands.describe(amount="Number of coins to bet (minimum 20) and number of spins (default 1)")
# async def cmd_slots(interaction: discord.Interaction, amount: int, spins: int = 1):
#     await interaction.response.defer()
#     await ensure_balance_table()
#     channel_id = bot.bot_settings.get(interaction.guild_id, {}).get("gambling_channel")
#     if not channel_id:
#         await interaction.followup.send("No gambling channel set.", ephemeral=True)
#         return
#     output_channel = bot.get_channel(int(channel_id))
#     if not output_channel:
#         await interaction.followup.send("Gambling channel not found.", ephemeral=True)
#         return

#     if amount < 20:
#         await interaction.followup.send(
#             "Minimum bet is **20** coins.", ephemeral=True
#         )
#         return
#     message_out = []
    
#     for spin in range(spins):
#         coins = await get_balance(interaction.user.id, interaction.guild_id)
#         if amount > coins:
#             embed = discord.Embed(
#                 title="❌ Insufficient funds",
#                 description=f"You only have **{coins:,}** coins.",
#                 color=discord.Color.red()
#             )
            
#             await interaction.followup.send(embed=embed, ephemeral=True)
#             return

#         reels = spin_reels()
        
#         winnings, result_text = calculate_payout(reels, amount)
        
#         if result_text == "won_jackpot":
#             winnings = await get_jackpot(interaction.guild_id)
#         net = winnings - amount  # negative if loss
        
        
        
#         await update_balance(interaction.user.id, interaction.guild_id, net)
#         new_balance = await get_balance(interaction.user.id, interaction.guild_id)

#         reel_display = " | ".join(reels)
        
#         if winnings == 0:
#             color = discord.Color.red()
#             outcome = f"-{amount:,} coins"
#             finout = -amount
#             losses =1
#             wins = 0
#         elif winnings < amount:
#             color = discord.Color.orange()
#             outcome = f"-{amount - winnings:,} coins"
#             finout = amount - winnings
#             wins = 1
#             losses = 0
#         else:
#             color = discord.Color.green()
#             outcome = f"+{net:,} coins"
#             finout = net
#             wins = 1
#             losses = 0

#         if result_text == "won_jackpot":
#             result_text = "You won the whole pot!!!"
        
#         message_out.append({"spins":spin, "winnings":finout, "results":result_text, "reeldisplay":reel_display,"wins":wins, "losses":losses})

#     total_bet = spins * amount
#     wins = sum(entry["wins"] for entry in message_out)
#     losses = sum(entry["losses"] for entry in message_out)
#     winnings = sum(entry["winnings"] for entry in message_out)
#     two_oak = sum(1 for entry in message_out if "small win!" in entry["results"])
#     minor_jp = sum(1 for entry in message_out if "**" in entry["results"])
#     jackpot = sum(1 for entry in message_out if "whole" in entry["results"])

#     embed = discord.Embed(
#             title="🎰 Slot Machine",
#         )
    
#     embed.add_field(name="Spins", value=str(spins), inline=False)
#     embed.add_field(name="Wins", value=str(wins), inline=False)
#     embed.add_field(name="Losses", value=str(losses), inline=True)
#     embed.add_field(name="Two of a kind", value=str(two_oak), inline=False)
#     embed.add_field(name="Minor Jackpot", value=str(minor_jp), inline=False)
#     embed.add_field(name="Jackpot", value=str(jackpot), inline=False)
#     embed.add_field(name="Total Bet", value=total_bet, inline=True)
#     embed.add_field(name="Bet Outcome", value=winnings, inline=True)
#     embed.add_field(name="Account Balance", value=f"{new_balance:,} coins", inline=False)
    
    
#     await output_channel.send(embed=embed)
#     await interaction.followup.send("Done!", ephemeral=True)


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

# +qty item
# -qty item 
# these are members commands

# ~qty item - this sets the inventory to a definite value 
# $value item - this sets the donation value of an item, it can be decimal. 
# <qty item - this sets the "we want this many of this item"