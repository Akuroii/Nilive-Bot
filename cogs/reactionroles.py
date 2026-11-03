import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from database import DB_PATH
from datetime import datetime, timezone, timedelta
import re
from discord.ext import tasks
from utils.permissions import check_bot_role_position


async def handle_role_toggle(
    interaction: discord.Interaction,
    message_id: int,
    role_id: int,
    booster_only: bool = False,
    required_role_id: int | None = None,
    exclusive: bool = False,
    max_roles: int = 0,
    require_confirmation: bool = False
):
    member = interaction.user
    guild = interaction.guild
    role = guild.get_role(role_id) if guild else None

    if not role:
        await interaction.response.send_message("Role not found.", ephemeral=True)
        return

    if booster_only and not getattr(member, "premium_since", None):
        await interaction.response.send_message(
            "This role is for boosters only! Boost the server to unlock it.",
            ephemeral=True)
        return

    if required_role_id:
        required = guild.get_role(required_role_id)
        if required and required not in member.roles:
            await interaction.response.send_message(
                f"You need the **{required.name}** role to get this.",
                ephemeral=True)
            return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor_btn = await db.execute("""
            SELECT role_id FROM reaction_roles WHERE message_id=?
        """, (message_id,))
        panel_roles = [row[0] for row in await cursor_btn.fetchall()]
        cursor_sel = await db.execute("""
            SELECT role_id FROM reaction_role_select_options WHERE message_id=?
        """, (message_id,))
        panel_roles.extend([row[0] for row in await cursor_sel.fetchall()])

    current_panel_roles = [
        guild.get_role(rid) for rid in set(panel_roles)
        if guild.get_role(rid) and guild.get_role(rid) in member.roles
    ]

    if role in member.roles:
        if require_confirmation:
            view = ConfirmView(role, action="remove")
            await interaction.response.send_message(
                f"Remove **{role.name}**?", view=view, ephemeral=True)
            return
        await member.remove_roles(role)
        await interaction.response.send_message(
            f"Removed **{role.name}**.", ephemeral=True)
        return

    if max_roles > 0 and len(current_panel_roles) >= max_roles:
        await interaction.response.send_message(
            f"You can only have **{max_roles}** role(s) from this panel.",
            ephemeral=True)
        return

    if require_confirmation:
        view = ConfirmView(
            role, action="add",
            exclusive=exclusive,
            current_roles=current_panel_roles if exclusive else [])
        await interaction.response.send_message(
            f"Get **{role.name}**?", view=view, ephemeral=True)
        return

    if exclusive:
        for r in current_panel_roles:
            try:
                await member.remove_roles(r)
            except Exception:
                pass

    can, warn = check_bot_role_position(guild, role)
    if not can:
        await interaction.response.send_message(warn, ephemeral=True)
        return

    try:
        await member.add_roles(role)
    except discord.Forbidden:
        await interaction.response.send_message(
            "⚠️ Nero lacks permission to assign this role. Please notify an administrator.",
            ephemeral=True)
        return
    except Exception as e:
        await interaction.response.send_message(
            "⚠️ An error occurred while assigning the role.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT expires_at FROM reaction_role_expiry
            WHERE guild_id=? AND user_id=0 AND role_id=? AND message_id=?
        """, (guild.id, role_id, message_id))
        expiry = await cursor.fetchone()
        if expiry:
            await db.execute("""
                INSERT OR REPLACE INTO reaction_role_expiry
                (guild_id, user_id, role_id, message_id, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (guild.id, member.id, role_id, message_id, expiry[0]))
            await db.commit()

    msg = f"Gave you **{role.name}**."
    if exclusive and current_panel_roles:
        removed = ", ".join(r.name for r in current_panel_roles)
        msg += f"\nRemoved: {removed}"
    await interaction.response.send_message(msg, ephemeral=True)


class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.expiry_check.start()

    def cog_unload(self):
        self.expiry_check.cancel()

    async def ensure_table(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_roles (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id         INTEGER,
                    channel_id       INTEGER,
                    message_id       INTEGER,
                    button_label     TEXT,
                    button_emoji     TEXT,
                    button_color     TEXT DEFAULT 'blurple',
                    role_id          INTEGER,
                    booster_only     INTEGER DEFAULT 0,
                    required_role_id INTEGER DEFAULT NULL,
                    row_index        INTEGER DEFAULT 0,
                    button_index     INTEGER DEFAULT 0,
                    custom_id        TEXT DEFAULT NULL
                )
            """)
            try:
                cursor = await db.execute("PRAGMA table_info(reaction_roles)")
                rr_cols = [c[1] for c in await cursor.fetchall()]
                if "row_index" not in rr_cols:
                    await db.execute("ALTER TABLE reaction_roles ADD COLUMN row_index INTEGER DEFAULT 0")
                if "button_index" not in rr_cols:
                    await db.execute("ALTER TABLE reaction_roles ADD COLUMN button_index INTEGER DEFAULT 0")
                if "custom_id" not in rr_cols:
                    await db.execute("ALTER TABLE reaction_roles ADD COLUMN custom_id TEXT DEFAULT NULL")
            except Exception:
                pass
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_panels (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    exclusive INTEGER DEFAULT 0,
                    max_roles INTEGER DEFAULT 0,
                    require_confirmation INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_expiry (
                    guild_id INTEGER,
                    user_id INTEGER,
                    role_id INTEGER,
                    message_id INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    PRIMARY KEY (guild_id, user_id, role_id, message_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_selects (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id             INTEGER NOT NULL,
                    channel_id           INTEGER NOT NULL,
                    message_id           INTEGER NOT NULL,
                    row_index            INTEGER NOT NULL DEFAULT 0,
                    custom_id            TEXT NOT NULL,
                    placeholder          TEXT,
                    min_values           INTEGER NOT NULL DEFAULT 1,
                    max_values           INTEGER NOT NULL DEFAULT 1,
                    disabled             INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_select_options (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    select_id            INTEGER NOT NULL,
                    message_id           INTEGER NOT NULL,
                    role_id              INTEGER NOT NULL,
                    label                TEXT NOT NULL,
                    description          TEXT,
                    emoji                TEXT,
                    booster_only         INTEGER NOT NULL DEFAULT 0,
                    required_role_id     INTEGER DEFAULT NULL,
                    expiry_days          INTEGER NOT NULL DEFAULT 0,
                    position             INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (select_id) REFERENCES reaction_role_selects(id) ON DELETE CASCADE
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rr_selects_msg ON reaction_role_selects(message_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rr_selects_guild ON reaction_role_selects(guild_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rr_select_opts_sel ON reaction_role_select_options(select_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_rr_select_opts_msg ON reaction_role_select_options(message_id, role_id)")
            await db.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_delete_reaction_role_select_options
                AFTER DELETE ON reaction_role_selects
                BEGIN
                    DELETE FROM reaction_role_select_options WHERE select_id = OLD.id;
                END;
            """)
            await db.commit()

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_table()
        await self.restore_views()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Fallback dynamic dispatcher for reaction role interactions created from web or before view restoration."""
        if interaction.type != discord.InteractionType.component:
            return
        if interaction.response.is_done():
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("rr_"):
            return

        # Handle Select Menu dynamic fallback
        if custom_id.startswith("rr_sel_") or custom_id.startswith("rr_select_") or interaction.data.get("component_type") == 3:
            values = interaction.data.get("values", [])
            if not values:
                return
            try:
                role_id = int(values[0])
            except (ValueError, TypeError):
                return

            message_id = interaction.message.id if interaction.message else None
            if not message_id:
                parts = custom_id.split("_")
                if len(parts) >= 3 and parts[2].isdigit():
                    message_id = int(parts[2])

            if not message_id:
                return

            if "_draft_" not in custom_id:
                view_store = getattr(getattr(self.bot, "_connection", None), "_view_store", None)
                if view_store and hasattr(view_store, "is_message_tracked") and callable(view_store.is_message_tracked):
                    if view_store.is_message_tracked(message_id) is True:
                        return

            async with aiosqlite.connect(DB_PATH) as db:
                opt_cursor = await db.execute("""
                    SELECT booster_only, required_role_id, expiry_days
                    FROM reaction_role_select_options WHERE message_id=? AND role_id=?
                """, (message_id, role_id))
                opt_row = await opt_cursor.fetchone()
                if not opt_row:
                    try:
                        await interaction.response.send_message(
                            "⚠️ This option is unconfigured or no longer active.", ephemeral=True)
                    except Exception:
                        pass
                    return

                panel_cursor = await db.execute("""
                    SELECT exclusive, max_roles, require_confirmation
                    FROM reaction_role_panels WHERE message_id=?
                """, (message_id,))
                panel_row = await panel_cursor.fetchone()

            exclusive = panel_row[0] if panel_row else 0
            max_roles = panel_row[1] if panel_row else 0
            require_confirmation = panel_row[2] if panel_row else 0
            booster_only = opt_row[0]
            required_role_id = opt_row[1]

            await handle_role_toggle(
                interaction=interaction,
                message_id=message_id,
                role_id=role_id,
                booster_only=bool(booster_only),
                required_role_id=required_role_id,
                exclusive=bool(exclusive),
                max_roles=max_roles,
                require_confirmation=bool(require_confirmation)
            )
            return

        # Handle Button dynamic fallback
        parts = custom_id.split("_")
        if len(parts) < 2:
            return
        try:
            role_id = int(parts[1])
        except (ValueError, TypeError):
            return

        message_id = interaction.message.id if interaction.message else None
        if not message_id and len(parts) >= 3 and parts[2].isdigit():
            message_id = int(parts[2])

        if not message_id:
            return

        if "_draft_" not in custom_id:
            view_store = getattr(getattr(self.bot, "_connection", None), "_view_store", None)
            if view_store and hasattr(view_store, "is_message_tracked") and callable(view_store.is_message_tracked):
                if view_store.is_message_tracked(message_id) is True:
                    return

        # Query exact component by row/button index if present in custom_id
        row_idx = None
        btn_idx = None
        if len(parts) >= 5 and parts[3].isdigit() and parts[4].isdigit():
            row_idx = int(parts[3])
            btn_idx = int(parts[4])

        async with aiosqlite.connect(DB_PATH) as db:
            role_row = None
            if row_idx is not None and btn_idx is not None:
                cursor = await db.execute("""
                    SELECT button_label, button_emoji, button_color, booster_only, required_role_id
                    FROM reaction_roles WHERE message_id=? AND row_index=? AND button_index=?
                """, (message_id, row_idx, btn_idx))
                role_row = await cursor.fetchone()

            if not role_row:
                cursor = await db.execute("""
                    SELECT button_label, button_emoji, button_color, booster_only, required_role_id
                    FROM reaction_roles WHERE message_id=? AND role_id=?
                """, (message_id, role_id))
                role_row = await cursor.fetchone()

            if not role_row:
                try:
                    await interaction.response.send_message(
                        "⚠️ This button is unconfigured or no longer active.", ephemeral=True)
                except Exception:
                    pass
                return

            panel_cursor = await db.execute("""
                SELECT exclusive, max_roles, require_confirmation
                FROM reaction_role_panels WHERE message_id=?
            """, (message_id,))
            panel_row = await panel_cursor.fetchone()

        exclusive = panel_row[0] if panel_row else 0
        max_roles = panel_row[1] if panel_row else 0
        require_confirmation = panel_row[2] if panel_row else 0
        booster_only = role_row[3]
        required_role_id = role_row[4]

        await handle_role_toggle(
            interaction=interaction,
            message_id=message_id,
            role_id=role_id,
            booster_only=bool(booster_only),
            required_role_id=required_role_id,
            exclusive=bool(exclusive),
            max_roles=max_roles,
            require_confirmation=bool(require_confirmation)
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        removed_roles = set(r.id for r in before.roles) - set(r.id for r in after.roles)
        if not removed_roles:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT required_role_id FROM reaction_roles
                WHERE required_role_id IS NOT NULL
            """)
            required_role_ids = {row[0] for row in await cursor.fetchall()}
        triggered_required = removed_roles & required_role_ids
        if not triggered_required:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            for req_role_id in triggered_required:
                cursor = await db.execute("""
                    SELECT role_id FROM reaction_roles
                    WHERE required_role_id=?
                """, (req_role_id,))
                dependent_roles = await cursor.fetchall()
                for (role_id,) in dependent_roles:
                    role = after.guild.get_role(role_id)
                    if role and role in after.roles:
                        try:
                            await after.remove_roles(role)
                            print(f"Removed {role.name} from {after} — required role lost")
                        except Exception as e:
                            print(f"Could not remove {role.name}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # Member is gone from the guild entirely — clear every expiry
        # obligation for them regardless of which panel/message it
        # came from, so message_id is intentionally NOT part of this
        # WHERE clause.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                DELETE FROM reaction_role_expiry
                WHERE guild_id=? AND user_id=?
            """, (member.guild.id, member.id))
            await db.commit()

    async def restore_views(self):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT guild_id, channel_id, message_id FROM reaction_roles
                UNION
                SELECT DISTINCT guild_id, channel_id, message_id FROM reaction_role_selects
            """)
            messages = await cursor.fetchall()
        for guild_id, channel_id, message_id in messages:
            view = await self.build_view(message_id)
            self.bot.add_view(view, message_id=message_id)

    async def build_view(self, message_id: int) -> discord.ui.View:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT role_id, button_label, button_emoji, button_color,
                       booster_only, required_role_id, row_index, button_index, custom_id
                FROM reaction_roles WHERE message_id=?
                ORDER BY row_index ASC, button_index ASC, id ASC
            """, (message_id,))
            button_rows = await cursor.fetchall()

            select_cursor = await db.execute("""
                SELECT id, row_index, custom_id, placeholder, min_values, max_values
                FROM reaction_role_selects WHERE message_id=? ORDER BY row_index ASC, id ASC
            """, (message_id,))
            select_rows = await select_cursor.fetchall()

            panel_cursor = await db.execute("""
                SELECT exclusive, max_roles, require_confirmation
                FROM reaction_role_panels WHERE message_id=?
            """, (message_id,))
            panel = await panel_cursor.fetchone()

        exclusive = panel[0] if panel else 0
        max_roles = panel[1] if panel else 0
        require_confirmation = panel[2] if panel else 0
        view = discord.ui.View(timeout=None)

        for role_id, label, emoji, color, booster_only, required_role_id, r_idx, btn_idx, b_custom_id in button_rows:
            color_map = {
                "blurple": discord.ButtonStyle.primary,
                "gray": discord.ButtonStyle.secondary,
                "green": discord.ButtonStyle.success,
                "red": discord.ButtonStyle.danger,
            }
            style = color_map.get(color, discord.ButtonStyle.primary)
            cid = b_custom_id or f"rr_{role_id}_{message_id}_{r_idx or 0}_{btn_idx or 0}"
            button = RoleButton(
                role_id=role_id,
                label=label,
                emoji=emoji or None,
                style=style,
                booster_only=bool(booster_only),
                required_role_id=required_role_id,
                exclusive=bool(exclusive),
                max_roles=max_roles,
                require_confirmation=bool(require_confirmation),
                message_id=message_id,
                custom_id=cid
            )
            view.add_item(button)

        if select_rows:
            async with aiosqlite.connect(DB_PATH) as db:
                for s_id, s_row_idx, s_custom_id, s_placeholder, s_min, s_max in select_rows:
                    opt_cursor = await db.execute("""
                        SELECT role_id, label, description, emoji, booster_only, required_role_id, expiry_days
                        FROM reaction_role_select_options WHERE select_id=? ORDER BY position ASC, id ASC
                    """, (s_id,))
                    opt_rows = await opt_cursor.fetchall()
                    if not opt_rows:
                        continue

                    discord_options = []
                    options_meta = {}
                    for r_id, o_label, o_desc, o_emoji, o_booster, o_req, o_exp in opt_rows:
                        em_obj = None
                        if o_emoji:
                            m = re.match(r"^<(a?):(\w+):(\d+)>$", str(o_emoji).strip())
                            if m:
                                em_obj = discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
                            else:
                                em_obj = discord.PartialEmoji.from_str(str(o_emoji).strip())

                        discord_options.append(discord.SelectOption(
                            label=str(o_label)[:100],
                            value=str(r_id),
                            description=str(o_desc)[:100] if o_desc else None,
                            emoji=em_obj
                        ))
                        options_meta[r_id] = {
                            "booster_only": bool(o_booster),
                            "required_role_id": o_req,
                            "expiry_days": o_exp
                        }

                    select_comp = RoleSelect(
                        message_id=message_id,
                        row_index=min(max(s_row_idx, 0), 4),
                        custom_id=s_custom_id or f"rr_sel_{message_id}_{s_row_idx}",
                        placeholder=s_placeholder or "Select a role...",
                        min_values=s_min,
                        max_values=s_max,
                        options=discord_options[:25],
                        options_metadata=options_meta,
                        exclusive=bool(exclusive),
                        max_roles=max_roles,
                        require_confirmation=bool(require_confirmation)
                    )
                    view.add_item(select_comp)

        return view

    @tasks.loop(minutes=30)
    async def expiry_check(self):
        """
        PHASE 2 FIX: this used to try/except-and-pass the role
        removal, then unconditionally DELETE the expiry row right
        after regardless of whether the removal actually worked. If
        remove_roles() failed (rate limit, missing permission,
        network blip), the row was deleted anyway and the role
        obligation silently vanished forever — the role just stayed
        on the member with no record left to retry against. The
        delete is now conditional on the removal actually succeeding
        (or there being nothing to remove), and each entry is
        isolated in its own try/except so one bad row can't stop the
        rest of the batch — or the whole 30-minute loop — from
        running. Same pattern as cogs/shop.py's temp_role_cleanup.

        PHASE 5 TAIL FIX (two changes):

        1. reaction_role_expiry rows are now keyed by (guild_id,
           user_id, role_id, message_id) instead of just (guild_id,
           user_id, role_id) — the old key let the same role added to
           two different panels with two different expiry periods
           collide, so a member claiming the role from either panel
           could silently pick up the wrong panel's expiry. message_id
           disambiguates which panel a given row belongs to, and the
           SELECT/DELETE below now carry it through.

        2. This loop used to sweep up user_id=0 sentinel rows (the
           per-panel "template" expiry written by /reactionrole_add)
           as if they were real member obligations. guild.get_member(0)
           is always None, so the role-removal branch was always
           skipped for a sentinel row — but removal_ok stayed True
           regardless, so the sentinel got DELETEd anyway once its own
           expires_at passed. That silently broke the feature: a
           role's configured expiry stopped applying to any NEW
           claimant after the first expiry_days window elapsed, since
           the template row RoleButton.callback reads from was gone.
           Sentinel rows are now explicitly excluded here — they're
           metadata for a button click to read, not an obligation for
           this loop to clean up.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT guild_id, user_id, role_id, message_id
                FROM reaction_role_expiry
                WHERE expires_at <= ? AND user_id != 0
            """, (now,))
            expired = await cursor.fetchall()

        for guild_id, user_id, role_id, message_id in expired:
            try:
                guild = self.bot.get_guild(guild_id)
                removal_ok = True
                if guild:
                    member = guild.get_member(user_id)
                    role = guild.get_role(role_id)
                    if member and role and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except Exception as e:
                            print(f"[RR] Failed to remove expired role "
                                  f"{role_id} from {user_id} in "
                                  f"{guild_id}: {e}")
                            removal_ok = False
                else:
                    # Bot isn't in this guild right now — keep the
                    # row so it's retried once it rejoins.
                    removal_ok = False

                if removal_ok:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("""
                            DELETE FROM reaction_role_expiry
                            WHERE guild_id=? AND user_id=? AND role_id=? AND message_id=?
                        """, (guild_id, user_id, role_id, message_id))
                        await db.commit()
            except Exception as e:
                print(f"[RR] expiry_check error for {user_id}/{role_id} "
                      f"in {guild_id}: {e}")

    @app_commands.command(name="reactionrole_create", description="Create a reaction role message with buttons")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_create(self, interaction: discord.Interaction,
                                   channel: discord.TextChannel,
                                   title: str,
                                   description: str,
                                   exclusive: bool = False,
                                   max_roles: int = 0,
                                   require_confirmation: bool = False):
        await self.ensure_table()
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.blurple())
        embed.set_footer(text="Click a button to get or remove a role.")
        view = discord.ui.View(timeout=None)
        msg = await channel.send(embed=embed, view=view)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO reaction_role_panels
                (message_id, guild_id, exclusive, max_roles, require_confirmation)
                VALUES (?, ?, ?, ?, ?)
            """, (msg.id, interaction.guild.id, int(exclusive),
                  max_roles, int(require_confirmation)))
            await db.commit()
        settings = []
        if exclusive:
            settings.append("exclusive")
        if max_roles:
            settings.append(f"max {max_roles} roles")
        if require_confirmation:
            settings.append("confirmation required")
        await interaction.response.send_message(
            f"Reaction role message created in {channel.mention}!\n"
            f"Message ID: `{msg.id}`\n"
            f"Settings: {', '.join(settings) if settings else 'default'}\n"
            f"Now use `/reactionrole_add` to add buttons.",
            ephemeral=True)

    @app_commands.command(name="reactionrole_add", description="Add a role button to a reaction role message")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_add(self, interaction: discord.Interaction,
                                message_id: str,
                                role: discord.Role,
                                label: str,
                                color: str = "blurple",
                                emoji: str = None,
                                booster_only: bool = False,
                                required_role: discord.Role = None,
                                expiry_days: int = 0):
        await self.ensure_table()
        color = color.lower()
        if color not in ["blurple", "gray", "green", "red"]:
            await interaction.response.send_message(
                "Color must be: blurple, gray, green, or red", ephemeral=True)
            return
        msg_id = int(message_id)
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT channel_id FROM reaction_roles WHERE message_id=?",
                (msg_id,))
            row = await cursor.fetchone()
            channel_id = row[0] if row else interaction.channel.id
            await db.execute("""
                INSERT OR REPLACE INTO reaction_roles
                (guild_id, channel_id, message_id, button_label, button_emoji,
                 button_color, role_id, booster_only, required_role_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (interaction.guild.id, channel_id, msg_id, label, emoji,
                  color, role.id, int(booster_only),
                  required_role.id if required_role else None))
            if expiry_days > 0:
                # PHASE 5 TAIL FIX: message_id is now part of both the
                # key and the row itself. This sentinel row (user_id=0)
                # is THIS panel's template expiry for this role —
                # previously keyed only on (guild_id, role_id), adding
                # the same role to a second panel with a different
                # expiry_days would silently overwrite this row instead
                # of coexisting alongside it.
                expires = (datetime.utcnow() + timedelta(days=expiry_days)).isoformat()
                await db.execute("""
                    INSERT OR REPLACE INTO reaction_role_expiry
                    (guild_id, user_id, role_id, message_id, expires_at)
                    VALUES (?, 0, ?, ?, ?)
                """, (interaction.guild.id, role.id, msg_id, expires))
            await db.commit()
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                view = await self.build_view(msg_id)
                await msg.edit(view=view)
                self.bot.add_view(view, message_id=msg_id)
                info = []
                if booster_only:
                    info.append("boosters only")
                if required_role:
                    info.append(f"requires {required_role.name}")
                if expiry_days:
                    info.append(f"expires in {expiry_days} days")
                await interaction.response.send_message(
                    f"Added button **{label}** → {role.mention}"
                    + (f" ({', '.join(info)})" if info else ""),
                    ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found.", ephemeral=True)

    @app_commands.command(name="reactionrole_remove", description="Remove a role button from a reaction role message")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_remove(self, interaction: discord.Interaction,
                                   message_id: str,
                                   role: discord.Role):
        msg_id = int(message_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM reaction_roles WHERE message_id=? AND role_id=?",
                (msg_id, role.id))
            # PHASE 5 TAIL FIX: also clean up this panel's own sentinel
            # expiry row (and any per-member rows tied to this specific
            # panel) now that message_id disambiguates them — otherwise
            # a removed button's expiry metadata would linger forever
            # since nothing else ever cleans up user_id=0 rows.
            await db.execute(
                "DELETE FROM reaction_role_expiry "
                "WHERE guild_id=? AND role_id=? AND message_id=?",
                (interaction.guild.id, role.id, msg_id))
            await db.commit()
        for ch in interaction.guild.text_channels:
            try:
                msg = await ch.fetch_message(msg_id)
                view = await self.build_view(msg_id)
                await msg.edit(view=view)
                await interaction.response.send_message(
                    f"Removed button for {role.mention}", ephemeral=True)
                return
            except:
                continue
        await interaction.response.send_message("Message not found.", ephemeral=True)

    @app_commands.command(name="reactionrole_list", description="List all reaction role messages")
    @app_commands.checks.has_permissions(administrator=True)
    async def reactionrole_list(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT r.message_id, r.channel_id, p.exclusive, p.max_roles
                FROM reaction_roles r
                LEFT JOIN reaction_role_panels p ON r.message_id = p.message_id
                WHERE r.guild_id=?
            """, (interaction.guild.id,))
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No reaction role messages found.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Reaction Role Messages",
            color=discord.Color.blurple())
        for msg_id, ch_id, exclusive, max_roles in rows:
            info = []
            if exclusive:
                info.append("exclusive")
            if max_roles:
                info.append(f"max {max_roles}")
            embed.add_field(
                name=f"Message: {msg_id}",
                value=f"<#{ch_id}>" + (f" — {', '.join(info)}" if info else ""),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class RoleButton(discord.ui.Button):
    def __init__(self, role_id, label, emoji, style, booster_only,
                 required_role_id, exclusive, max_roles,
                 require_confirmation, message_id, custom_id=None):
        btn_custom_id = custom_id or f"rr_{role_id}_{message_id}"
        super().__init__(label=label, emoji=emoji, style=style,
                        custom_id=btn_custom_id)
        self.role_id = role_id
        self.booster_only = booster_only
        self.required_role_id = required_role_id
        self.exclusive = exclusive
        self.max_roles = max_roles
        self.require_confirmation = require_confirmation
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        await handle_role_toggle(
            interaction=interaction,
            message_id=self.message_id,
            role_id=self.role_id,
            booster_only=self.booster_only,
            required_role_id=self.required_role_id,
            exclusive=self.exclusive,
            max_roles=self.max_roles,
            require_confirmation=self.require_confirmation
        )


class RoleSelect(discord.ui.Select):
    def __init__(self, message_id, row_index, custom_id, placeholder,
                 min_values, max_values, options, options_metadata,
                 exclusive, max_roles, require_confirmation):
        super().__init__(
            custom_id=custom_id,
            placeholder=placeholder or "Select a role...",
            min_values=min_values,
            max_values=max_values,
            options=options,
            row=row_index
        )
        self.message_id = message_id
        self.options_metadata = options_metadata
        self.exclusive = exclusive
        self.max_roles = max_roles
        self.require_confirmation = require_confirmation

    async def callback(self, interaction: discord.Interaction):
        if not self.values:
            return
        try:
            role_id = int(self.values[0])
        except (ValueError, TypeError):
            await interaction.response.send_message("Invalid role option selected.", ephemeral=True)
            return

        meta = self.options_metadata.get(role_id, {})
        await handle_role_toggle(
            interaction=interaction,
            message_id=self.message_id,
            role_id=role_id,
            booster_only=meta.get("booster_only", False),
            required_role_id=meta.get("required_role_id"),
            exclusive=self.exclusive,
            max_roles=self.max_roles,
            require_confirmation=self.require_confirmation
        )


class ConfirmView(discord.ui.View):
    def __init__(self, role, action, exclusive=False, current_roles=None):
        super().__init__(timeout=30)
        self.role = role
        self.action = action
        self.exclusive = exclusive
        self.current_roles = current_roles or []

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        member = interaction.user
        if self.action == "remove":
            await member.remove_roles(self.role)
            await interaction.response.edit_message(
                content=f"Removed **{self.role.name}**.", view=None)
        else:
            can, warn = check_bot_role_position(interaction.guild, self.role)
            if not can:
                await interaction.response.edit_message(content=warn, view=None)
                return
            if self.exclusive:
                for r in self.current_roles:
                    try:
                        await member.remove_roles(r)
                    except Exception:
                        pass
            try:
                await member.add_roles(self.role)
                await interaction.response.edit_message(
                    content=f"Gave you **{self.role.name}**.", view=None)
            except discord.Forbidden:
                await interaction.response.edit_message(
                    content="⚠️ Nero lacks permission to assign this role. Please notify an administrator.", view=None)
            except Exception:
                await interaction.response.edit_message(
                    content="⚠️ An error occurred while assigning the role.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled.", view=None)


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
