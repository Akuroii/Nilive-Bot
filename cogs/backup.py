import os
import time
import uuid
import shutil
import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite
from database import DB_PATH

# FEATURE (dark-fixes pass #2): backup_log had a schema (database.py)
# with zero implementation anywhere — no backup mechanism existed at
# all, just an empty table waiting for rows nothing ever wrote. This
# cog is that missing mechanism: a daily automated DB backup, a manual
# trigger, and pruning so backups don't grow unbounded.
#
# Uses aiosqlite's Connection.backup() (wraps sqlite3's online backup
# API) rather than a raw file copy — a plain `cp` of a WAL-mode SQLite
# file while it's in use can capture the main file and WAL out of sync
# and produce a corrupt backup. The backup API is safe to run against
# a live, concurrently-written database.
#
# CAVEAT (Railway specifically): DB_PATH already lives on Railway's
# persistent volume (/app/data), so backups written next to it survive
# process restarts and redeploys. They do NOT protect against the
# volume itself being deleted — that would need off-volume storage
# (S3, etc.). See SECONDARY_BACKUP_DIR below for an optional mitigation.

BACKUP_DIR   = os.path.join(os.path.dirname(DB_PATH), "backups")
KEEP_BACKUPS = 7  # prune anything past the most recent N

# RELIABILITY FIX: backups previously lived exclusively under
# BACKUP_DIR, which is on the SAME persistent volume as the live
# database (DB_PATH). If that volume is ever lost (disk failure,
# accidental deletion, a redeploy that drops the mount), the last 7
# daily backups are destroyed right alongside the live DB — a backup
# strategy that shares a single point of failure with the thing it's
# backing up isn't a real safety net.
#
# SECONDARY_BACKUP_DIR is an optional escape hatch: if set to a path
# on a genuinely different volume/mount (a second disk, a mounted
# network share, anything not sharing physical storage with DB_PATH's
# volume), every backup is also copied there. Left unset, behavior is
# identical to before — this is additive, not a replacement for
# actually mounting a second location.
SECONDARY_BACKUP_DIR = os.getenv("SECONDARY_BACKUP_DIR", "").strip() or None


async def _do_backup() -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # BUGFIX (caught by testing, not review): strftime at 1-second
    # resolution collided when two backups landed in the same second
    # (e.g. /backup_now fired twice quickly) — the second backup
    # silently overwrote the first's file on disk while backup_log
    # still recorded two separate rows pointing at the same filename.
    # That collision then fed a worse bug: pruning an old duplicate-
    # named row deleted the file a newer surviving row also pointed
    # to, wiping every backup on disk instead of just the oldest ones.
    # A short uuid suffix makes collisions practically impossible.
    filename = f"nero_backup_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.db"
    dest_path = os.path.join(BACKUP_DIR, filename)

    async with aiosqlite.connect(DB_PATH) as src:
        async with aiosqlite.connect(dest_path) as dst:
            await src.backup(dst)

    size_bytes = os.path.getsize(dest_path)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO backup_log (filename, size_bytes)
            VALUES (?, ?)
        """, (filename, size_bytes))
        await db.commit()

    # RELIABILITY FIX: best-effort copy to a second, operator-
    # configured location. Failure here must never break the primary
    # backup (which already succeeded above) or the calling command —
    # logged and swallowed, same defensive pattern as the rest of this
    # cog's error handling.
    secondary_ok = None
    if SECONDARY_BACKUP_DIR:
        try:
            os.makedirs(SECONDARY_BACKUP_DIR, exist_ok=True)
            shutil.copy2(dest_path, os.path.join(SECONDARY_BACKUP_DIR, filename))
            secondary_ok = True
        except Exception as e:
            print(f"[BACKUP] Secondary copy to {SECONDARY_BACKUP_DIR} failed: {e}")
            secondary_ok = False

    await _prune_old_backups()
    return {"filename": filename, "size_bytes": size_bytes,
            "secondary_ok": secondary_ok}


async def _prune_old_backups():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT id, filename FROM backup_log
            ORDER BY created_at DESC
        """)
        rows = await cursor.fetchall()

        for row_id, filename in rows[KEEP_BACKUPS:]:
            path = os.path.join(BACKUP_DIR, filename)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                print(f"[BACKUP] Failed to remove old backup {filename}: {e}")
            # Prune the secondary copy too, if configured — same
            # KEEP_BACKUPS retention window applies to both locations
            # so the secondary doesn't grow unbounded.
            if SECONDARY_BACKUP_DIR:
                sec_path = os.path.join(SECONDARY_BACKUP_DIR, filename)
                try:
                    if os.path.exists(sec_path):
                        os.remove(sec_path)
                except Exception as e:
                    print(f"[BACKUP] Failed to remove old secondary backup {filename}: {e}")
            await db.execute("DELETE FROM backup_log WHERE id = ?", (row_id,))
        await db.commit()


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_backup.start()

    def cog_unload(self):
        self.daily_backup.cancel()

    @tasks.loop(hours=24)
    async def daily_backup(self):
        try:
            result = await _do_backup()
            print(f"[BACKUP] Created {result['filename']} "
                  f"({result['size_bytes']:,} bytes)"
                  + (" [+secondary]" if result.get("secondary_ok") else ""))
        except Exception as e:
            print(f"[BACKUP] Automated backup failed: {e}")

    @daily_backup.before_loop
    async def before_daily_backup(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="backup_now",
                          description="Trigger an immediate DB backup (owner only)")
    async def backup_now(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await _do_backup()
            sec_note = ""
            if SECONDARY_BACKUP_DIR:
                sec_note = (" · secondary copy ✅" if result.get("secondary_ok")
                            else " · ⚠️ secondary copy FAILED (check logs)")
            await interaction.followup.send(
                f"✅ Backup created: `{result['filename']}` "
                f"({result['size_bytes']:,} bytes){sec_note}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Backup failed: {e}", ephemeral=True)

    @app_commands.command(name="backup_list",
                          description="List recent DB backups (owner only)")
    async def backup_list(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "Owner only.", ephemeral=True)
            return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT filename, size_bytes, created_at FROM backup_log
                ORDER BY created_at DESC LIMIT 10
            """)
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No backups yet.", ephemeral=True)
            return
        embed = discord.Embed(title="💾 Recent Backups", color=0x5865F2)
        note = (f"\nSecondary location: `{SECONDARY_BACKUP_DIR}`"
                if SECONDARY_BACKUP_DIR else
                "\n⚠️ No SECONDARY_BACKUP_DIR configured — backups share "
                "a volume with the live DB. See cogs/backup.py notes.")
        embed.description = "\n".join(
            f"`{f}` — {s:,} bytes — {c}" for f, s, c in rows) + note
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
