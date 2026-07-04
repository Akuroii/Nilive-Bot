import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
import random
from database import DB_PATH

try:
    from thefuzz import fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False
    print("[triggers] thefuzz not installed — fuzzy matching disabled")


class Triggers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS triggers (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id         INTEGER,
                    trigger_words    TEXT NOT NULL,
                    response_text    TEXT,
                    response_embed   TEXT,
                    response_type    TEXT DEFAULT 'text',
                    match_type       TEXT DEFAULT 'contains',
                    fuzzy_match      INTEGER DEFAULT 0,
                    case_sensitive   INTEGER DEFAULT 0,
                    response_chance  INTEGER DEFAULT 100,
                    allowed_channels TEXT,
                    enabled          INTEGER DEFAULT 1,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    def _matches(self, content: str, trigger_words: str,
                 match_type: str, fuzzy: bool,
                 case_sensitive: bool) -> bool:
        """
        Checks if a message content matches the trigger.

        match_type options:
            contains   — trigger word appears anywhere in message
            startswith — message starts with trigger word
            exact      — message is exactly the trigger word
            endswith   — message ends with trigger word

        fuzzy: uses thefuzz ratio (>= 80 match threshold)
        case_sensitive: default OFF (Arabic + English both work)

        Arabic support: since we do not modify the Unicode content,
        Arabic text is matched correctly by all match types.
        """
        words = [w.strip() for w in trigger_words.split(",") if w.strip()]
        if not case_sensitive:
            content_check = content.lower()
            words = [w.lower() for w in words]
        else:
            content_check = content

        for word in words:
            if fuzzy and FUZZY_AVAILABLE:
                ratio = fuzz.partial_ratio(word, content_check)
                if ratio >= 80:
                    return True
                continue
            if match_type == "contains":
                if word in content_check:
                    return True
            elif match_type == "startswith":
                if content_check.startswith(word):
                    return True
            elif match_type == "exact":
                if content_check.strip() == word:
                    return True
            elif match_type == "endswith":
                if content_check.endswith(word):
                    return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        await self.ensure_table()

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, trigger_words, response_text,
                       response_embed, response_type, match_type,
                       fuzzy_match, case_sensitive, response_chance,
                       allowed_channels, enabled
                FROM triggers
                WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
            """, (message.guild.id,))
            all_triggers = await cursor.fetchall()

        for t in all_triggers:
            (tid, guild_id, trigger_words, response_text,
             response_embed, response_type, match_type,
             fuzzy_match, case_sensitive, response_chance,
             allowed_channels, enabled) = t

            # Channel filter
            if allowed_channels:
                try:
                    allowed = json.loads(allowed_channels)
                    if allowed and message.channel.id not in [int(c) for c in allowed]:
                        continue
                except Exception:
                    pass

            # Match check
            if not self._matches(
                message.content, trigger_words,
                match_type or "contains",
                bool(fuzzy_match),
                bool(case_sensitive)
            ):
                continue

            # % chance check
            chance = int(response_chance) if response_chance else 100
            if chance < 100 and random.randint(1, 100) > chance:
                continue

            # Send response
            try:
                if response_type == "embed" and response_embed:
                    try:
                        embed_data = json.loads(response_embed)
                    except Exception:
                        embed_data = {}
                    color_str = embed_data.get("color", "#5865F2")
                    try:
                        color_int = int(color_str.strip("#"), 16)
                    except Exception:
                        color_int = 0x5865F2
                    embed = discord.Embed(color=color_int)
                    if embed_data.get("title"):
                        embed.title = embed_data["title"]
                    if embed_data.get("description"):
                        embed.description = embed_data["description"]
                    if embed_data.get("footer"):
                        embed.set_footer(text=embed_data["footer"])
                    if embed_data.get("image"):
                        embed.set_image(url=embed_data["image"])
                    await message.channel.send(embed=embed)

                elif response_type == "reply" and response_text:
                    await message.reply(response_text, mention_author=False)

                elif response_type == "react" and response_text:
                    await message.add_reaction(response_text.strip())

                elif response_text:
                    await message.channel.send(response_text)

            except Exception as e:
                print(f"[triggers] Error responding to trigger {tid}: {e}")

            break  # Only fire first matching trigger per message

    @app_commands.command(
        name="trigger_add",
        description="Add a trigger (use dashboard for full options)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def trigger_add(self, interaction: discord.Interaction,
                          trigger: str, response: str):
        """Quick-add a simple contains trigger from Discord."""
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO triggers
                    (guild_id, trigger_words, response_text,
                     response_type, match_type, enabled)
                VALUES (?, ?, ?, 'text', 'contains', 1)
            """, (interaction.guild.id, trigger, response))
            await db.commit()
        await interaction.response.send_message(
            f"Trigger added! When someone says **{trigger}**, "
            f"I'll respond with: {response[:80]}",
            ephemeral=True)

    @app_commands.command(
        name="trigger_remove",
        description="Remove a trigger by ID")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def trigger_remove(self, interaction: discord.Interaction,
                             trigger_id: int):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM triggers WHERE id = ? AND guild_id = ?",
                (trigger_id, interaction.guild.id))
            await db.commit()
        await interaction.response.send_message(
            f"Trigger #{trigger_id} removed.", ephemeral=True)

    @app_commands.command(
        name="trigger_list",
        description="List all triggers")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def trigger_list(self, interaction: discord.Interaction):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, trigger_words, response_type,
                       match_type, response_chance, enabled
                FROM triggers
                WHERE guild_id = ? OR guild_id = 0
                ORDER BY id ASC LIMIT 25
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No triggers set. Use the dashboard to add them.",
                ephemeral=True)
            return
        embed = discord.Embed(title="Active Triggers", color=0x7c5cbf)
        for r in rows:
            status = "✅" if r[5] else "❌"
            embed.add_field(
                name=f"#{r[0]} {status} — {r[2]} ({r[3]})",
                value=f"`{r[1][:60]}` — {r[4]}% chance",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="trigger_toggle",
        description="Enable or disable a trigger")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def trigger_toggle(self, interaction: discord.Interaction,
                             trigger_id: int):
        await self.ensure_table()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT enabled FROM triggers WHERE id = ? AND (guild_id = ? OR guild_id = 0)",
                (trigger_id, interaction.guild.id))
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message(
                    "Trigger not found.", ephemeral=True)
                return
            new_state = 0 if row[0] else 1
            await db.execute(
                "UPDATE triggers SET enabled = ? WHERE id = ?",
                (new_state, trigger_id))
            await db.commit()
        state_str = "enabled" if new_state else "disabled"
        await interaction.response.send_message(
            f"Trigger #{trigger_id} {state_str}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Triggers(bot))
