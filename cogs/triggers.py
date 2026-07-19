import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import json
import random
import time
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
        # PHASE 2 FIX: anti-repeat cooldown tracker. Keyed by
        # (guild_id, trigger_id) -> last-fired unix timestamp, so a
        # trigger can be rate-limited independently of any other
        # trigger even if several match the same message. In-memory
        # only (like Leveling's _xp_cooldowns) — resets on restart,
        # which is fine for a spam-prevention cooldown.
        self._last_fired: dict[tuple, float] = {}

        # MEMORY LEAK FIX (dark-fixes pass #11): this dict only ever
        # grew — a key is written every time a cooldown-gated trigger
        # fires and nothing ever removed old entries, so it slowly
        # accumulated one permanent (guild_id, trigger_id) entry per
        # trigger that ever fired, for the lifetime of the process.
        # Flagged as low-priority across several passes and left
        # alone each time; cleared now since nothing higher-priority
        # is currently blocking on Dark's input.
        #
        # Unlike economy.py's cooldown dicts, the stored value here
        # is a last-FIRED timestamp, not an expiry — per-trigger
        # cooldown_seconds lives in the DB, not in this dict, so
        # there's no single expiry to compare against cheaply. Using
        # a generous fixed age cutoff instead: any trigger that
        # hasn't fired in the last 24h is safe to drop regardless of
        # its configured cooldown (cooldowns are configured in
        # seconds/minutes in the dashboard UI, never days), and only
        # runs the sweep once the dict has actually grown large.
        self._COOLDOWN_PRUNE_THRESHOLD = 2000
        self._COOLDOWN_MAX_AGE_SECONDS = 24 * 60 * 60

    def _prune_last_fired(self, now: float) -> None:
        if len(self._last_fired) < self._COOLDOWN_PRUNE_THRESHOLD:
            return
        cutoff = now - self._COOLDOWN_MAX_AGE_SECONDS
        stale = [k for k, ts in self._last_fired.items() if ts < cutoff]
        for k in stale:
            del self._last_fired[k]

    async def cog_load(self):
        # PERFORMANCE FIX (dark-fixes pass #2): ensure_table() used to
        # run inside on_message, meaning every single guild message
        # opened a fresh connection and issued a full CREATE TABLE IF
        # NOT EXISTS + commit before the trigger lookup could even
        # start — on any active server that's thousands of redundant
        # schema statements a day for a table that database.py's
        # central init_db() (see database.py, "triggers" table) already
        # guarantees exists before the bot ever comes online. Schema
        # setup now happens once here, at cog load, matching the
        # lifecycle discord.py already provides for this exact purpose.
        await self.ensure_table()

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
                    fuzzy_threshold  INTEGER DEFAULT 80,
                    case_sensitive   INTEGER DEFAULT 0,
                    response_chance  INTEGER DEFAULT 100,
                    cooldown_seconds INTEGER DEFAULT 0,
                    allowed_channels TEXT,
                    enabled          INTEGER DEFAULT 1,
                    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    def _matches(self, content: str, trigger_words: str,
                 match_type: str, fuzzy: bool,
                 case_sensitive: bool, fuzzy_threshold: int = 80) -> bool:
        """
        Checks if a message content matches the trigger.

        match_type options:
            contains   — trigger word appears anywhere in message
            startswith — message starts with trigger word
            exact      — message is exactly the trigger word
            endswith   — message ends with trigger word

        fuzzy: uses thefuzz ratio (>= fuzzy_threshold match, default 80)
        case_sensitive: default OFF (Arabic + English both work)

        PHASE 2 FIX: fuzzy_threshold was previously hardcoded to 80
        for every trigger. A loose trigger (short/common word) with
        an 80% partial-ratio threshold can false-positive constantly;
        a strict one might want 90+. It's now per-trigger, read from
        the triggers table and defaulting to 80 for older rows that
        predate the column.

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
                if ratio >= fuzzy_threshold:
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

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT id, guild_id, trigger_words, response_text,
                       response_embed, response_type, match_type,
                       fuzzy_match, fuzzy_threshold, case_sensitive,
                       response_chance, cooldown_seconds,
                       allowed_channels, enabled
                FROM triggers
                WHERE (guild_id = ? OR guild_id = 0) AND enabled = 1
            """, (message.guild.id,))
            all_triggers = await cursor.fetchall()

        now = time.time()

        for t in all_triggers:
            (tid, guild_id, trigger_words, response_text,
             response_embed, response_type, match_type,
             fuzzy_match, fuzzy_threshold, case_sensitive,
             response_chance, cooldown_seconds,
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
                bool(case_sensitive),
                int(fuzzy_threshold) if fuzzy_threshold else 80,
            ):
                continue

            # PHASE 2 FIX: anti-repeat cooldown. Previously a trigger
            # fired on every single matching message with no rate
            # limit at all — a common word set to "contains" could
            # spam a response into a busy channel dozens of times a
            # minute. cooldown_seconds=0 (the default) preserves the
            # old fire-every-time behavior for triggers that want it.
            cooldown_key = (message.guild.id, tid)
            cooldown     = int(cooldown_seconds) if cooldown_seconds else 0
            if cooldown > 0:
                last = self._last_fired.get(cooldown_key, 0)
                if now - last < cooldown:
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

                # Only stamp the cooldown once the response actually
                # sent successfully — a send failure shouldn't burn
                # the cooldown window for a trigger that never fired.
                if cooldown > 0:
                    self._last_fired[cooldown_key] = now
                    self._prune_last_fired(now)

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
