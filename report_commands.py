import discord
from discord import app_commands
from discord.ext import commands
from io import BytesIO
from datetime import datetime, timezone, timedelta


class ReportCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool = bot.pool

    def parse_date(self, date_str: str):
        return datetime.strptime(date_str, "%Y-%m-%d")
    
    @app_commands.command(name="show_required", description="Show required items vs current stock")
    async def show_required(self, interaction: discord.Interaction):

        server_id = interaction.guild.id

        query = """
            WITH current_inventory AS (
                SELECT
                    server_id,
                    item_name,
                    quantity AS stock_qty
                FROM inventory
            )
            SELECT
                r.item_name,
                r.required_quantity,
                COALESCE(i.stock_qty, 0) AS stock_qty,
                r.required_quantity - COALESCE(i.stock_qty, 0) AS remaining
            FROM required_items r
            LEFT JOIN current_inventory i
                ON i.server_id = r.server_id
            AND i.item_name = r.item_name
            WHERE r.server_id = $1
            ORDER BY r.item_name
            LIMIT 20;
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, server_id)

        if not rows:
            await interaction.response.send_message("No required items set.", ephemeral=True)
            return

        # Table formatting
        lines = ["\n**Required Items Status:**", "```"]

        max_item_len = max(
        max(len(r["item_name"]) for r in rows),
        len("Item")
        )

        max_req_len = max(
            max(len(str(r["required_quantity"])) for r in rows),
            len("Required")
        )

        max_stock_len = max(
            max(len(str(r["stock_qty"])) for r in rows),
            len("Stock")
        )

        max_rem_len = max(
            max(len(str(r["remaining"])) for r in rows),
            len("To collect")
        )

        header = (
            f"{'Item'.ljust(max_item_len)} | "
            f"{'Required'.rjust(max_req_len)} | "
            f"{'Stock'.rjust(max_stock_len)} | "
            f"{'To collect'.rjust(max_rem_len)}"
        )

        separator = (
            f"{'-' * max_item_len}-+-"
            f"{'-' * max_req_len}-+-"
            f"{'-' * max_stock_len}-+-"
            f"{'-' * max_rem_len}"
        )

        lines.append(header)
        lines.append(separator)

        for r in rows:
            lines.append(
                f"{r['item_name'].ljust(max_item_len)} | "
                f"{str(r['required_quantity']).rjust(max_req_len)} | "
                f"{str(r['stock_qty']).rjust(max_stock_len)} | "
                f"{str(r['remaining']).rjust(max_rem_len)}"
            )

        lines.append("```")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="show_donation_values", description="View donation report")
    async def report_user(self, interaction: discord.Interaction):
        server_id = interaction.guild.id
        await interaction.response.defer()
        async with self.pool.acquire() as conn:
           rows = await conn.fetch("""
                SELECT 
                item_name, donation_value
                FROM donation_values
                WHERE server_id = $1
                AND donation_value IS NOT NULL
                ORDER BY donation_value desc
            """, server_id)
        settings = self.bot_settings.get(server_id)
        if not settings:
            return
        output_channel = self.bot.get_channel(int(settings.get("output")))
        if not output_channel:
            return
        
        if not rows:
            message=f"No donation values configured" 
        else: 
            message = (f"Item donation values set as follows:\n" 
                       + "\n".join(f"{r['item_name']} - {r['donation_value']}" for r in rows))
        
        await output_channel.send(message)
        

    @app_commands.command(name="report_user", description="View donation report")
    async def report_user(self, interaction: discord.Interaction, start_date: str = None, end_date: str = None):
        server_id = interaction.guild.id

        query = """
            SELECT user_id, item, SUM(quantity) AS total_quantity
            FROM donations
            WHERE server_id = $1
        """

        params = [server_id]
        idx = 2

        if start_date:
            start_date = self.parse_date(start_date)
            query += f" AND donation_date >= ${idx}"
            params.append(start_date)
            idx += 1

        if end_date:
            end_date = self.parse_date(end_date)
            query += f" AND donation_date <= ${idx}"
            params.append(end_date)
            idx += 1

        query += " GROUP BY user_id, item ORDER BY user_id, item"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        if not rows:
            await interaction.response.send_message("No donations in that range.", ephemeral=True)
            return

        lines = ["**Donation Report:**"]
        for r in rows:
            user_id = int(r["user_id"])
            member = interaction.guild.get_member(user_id)
            if member:
                username = member.display_name
            else:
                username = str(user_id)
            lines.append(f"{username} donated: {r['total_quantity']} of {r['item']}")

        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="report_user_file", description="View donation report")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_user_file(self, interaction: discord.Interaction, start_date: str = None, end_date: str = None):
        server_id = interaction.guild.id

        query = """
            SELECT user_id, item, SUM(quantity) AS total_quantity
            FROM donations
            WHERE server_id = $1 AND is_adjustment = FALSE
        """

        params = [server_id]
        idx = 2

        if start_date:
            start_date = self.parse_date(start_date)
            query += f" AND donation_date >= ${idx}"
            params.append(start_date)
            idx += 1

        if end_date:
            end_date = self.parse_date(end_date)
            query += f" AND donation_date <= ${idx}"
            params.append(end_date)
            idx += 1

        query += " GROUP BY user_id, item ORDER BY user_id, item"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        if not rows:
            await interaction.response.send_message("No donations in that range.", ephemeral=True)
            return

        # Build CSV safely
        content = "User,Item,Quantity\n"

        for r in rows:
            user_id = int(r["user_id"])

            member = interaction.guild.get_member(user_id)
            if member:
                username = member.display_name
            else:
                username = str(user_id)

            content += f"{username},{r['item']},{r['total_quantity'] or 0}\n"

        file = discord.File(
            BytesIO(content.encode("utf-8")),
            filename="user_report.csv"
        )

        await interaction.response.send_message(
            "Here is your donation report export:",
            file=file
        )

    @app_commands.command(name="inventory", description="View inventory report")
    async def inventory_report(self, interaction: discord.Interaction):
        server_id = interaction.guild.id
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
            SELECT item_name, quantity
            FROM inventory
            WHERE server_id = $1
            ORDER BY item_name
        """,server_id)
        
        if not rows:
            await interaction.response.send_message("No inventory in that range.", ephemeral=True)
            return

        lines = ["\n**Inventory Report:**", "```"]

        max_item_len = max(len(r['item_name']) for r in rows)
        max_qty_len = max(len(str(r['quantity'] or 0)) for r in rows)

        header = f"{'Item'.ljust(max_item_len)} | {'Qty'.rjust(max_qty_len)}"
        separator = f"{'-' * max_item_len}-+-{'-' * max_qty_len}"

        lines.append(header)
        lines.append(separator)

        for r in rows:
            item_name = r['item_name']
            qty = r['quantity'] or 0
            lines.append(f"{item_name.ljust(max_item_len)} | {str(qty).rjust(max_qty_len)}")

        lines.append("```")

        await interaction.response.send_message("\n".join(lines))
    
    
    @app_commands.command(name="inventory_file")
    @app_commands.checks.has_permissions(administrator=True)
    async def inventory_file(self, interaction: discord.Interaction):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT item_name, quantity
                FROM inventory
                WHERE server_id = $1
                ORDER BY item_name asc
            """, interaction.guild.id)

        if not rows:
            await interaction.response.send_message("No inventory.", ephemeral=True)
            return

        # Build CSV in memory
        content = "Item,Quantity\n"
        for r in rows:
            content += f"{r['item_name']},{r['quantity'] or 0}\n"

        file = discord.File(
            BytesIO(content.encode("utf-8")),
            filename="inventory.csv"
        )

        await interaction.response.send_message(
            "Here is your inventory export:",
            file=file
        )


async def setup(bot):
    await bot.add_cog(ReportCommands(bot))