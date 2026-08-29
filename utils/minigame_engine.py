import asyncio
import functools
import random
import re
import traceback

import discord
from utils import minigame_store as store
from utils.formatters import snapshot_user
from utils.reward_engine import give_reward

# ═══════════════════════════════════════════════════════════════════════
# MINIGAMES v2 — GAME ENGINES (Phase 2 of the approved v2 plan)
#
# One shared architecture, six engines (the multiple-choice engine is
# shared by math / colors / emoji — plan §6).
#
# ARCHITECTURE (plan §1, round-3 requirement):
#   * Each engine OWNS the definition of its interactive components as
#     PLAIN Discord-API components JSON (module-level pure functions,
#     zero discord imports in that code path):
#         - the bot renders the JSON into a discord.ui.View (adapter)
#         - the Dashboard live preview (Phase 4/5) renders the SAME JSON
#           — it never recreates the game's look independently.
#   * The engine is a state machine; the View is a thin transport.
#     All resolution logic lives in the engine, reachable without a live
#     discord client (tests drive `engine._dispatch(...)` directly).
#
# SINGLE AUTHORITATIVE RESOLUTION:
#   `MinigameEngine._claim()` (check-and-set of `finished` under one
#   asyncio.Lock, no awaits between) is the ONLY gate. Every ending path
#   (click, timeout, draw, second resolution attempt) funnels through
#   `resolve()`, which claims first — a second caller gets False and
#   does nothing. Timers are cancelled after the claim.
#
# NO-FORFEIT PRINCIPLE (D3): every timeout ends "no winner, no reward"
# unless a valid winning state exists. A seated RPS player who never
# chooses loses nothing — the game simply ends.
#
# FINALIZATION ORDER (plan §18, verified in tests):
#   resolve winners → independent reward rolls → grant rewards
#   → edit/reveal the embed (BEST EFFORT — a failed edit never loses
#     rewards) → close the run log row.
#
# Reward delivery is exclusively utils.reward_engine.give_reward — the
# single shared grant path (plan §7/§9).
# ═══════════════════════════════════════════════════════════════════════

# ── PURE COMPONENT DESCRIPTORS (Discord-API JSON — preview-compatible) ──
# Style constants are Discord's API values: 1 primary, 2 secondary,
# 3 success (green), 4 danger (red).

_STYLE = {"primary": 1, "secondary": 2, "success": 3, "danger": 4}
_DISCORD_STYLE = {
    1: discord.ButtonStyle.primary,
    2: discord.ButtonStyle.secondary,
    3: discord.ButtonStyle.success,
    4: discord.ButtonStyle.danger,
}


def _btn(label, style="secondary", disabled=False, custom_id="",
         emoji=None, route="main") -> dict:
    lab = str(label)[:80]
    if not lab and not emoji:
        lab = "?"  # discord rejects empty labels; a bare "?" is the safest
    d = {"type": 2, "label": lab, "style": _STYLE[style],
         "disabled": bool(disabled), "custom_id": str(custom_id)}
    if emoji:
        d["emoji"] = emoji
    if route != "main":
        d["_route"] = route  # internal routing hint, stripped for Discord
    return d


def qc_rows(button_count: int, revealed: bool, green_index: int | None = None
            ) -> list[list[dict]]:
    """Quick Click rows. Pre-reveal: all disabled (visually invalid — D2).
    Revealed: the green button enabled+success, the rest stay disabled."""
    row = []
    for i in range(button_count):
        if revealed and i == green_index:
            row.append(_btn(i + 1, "success", False, f"qc_{i}"))
        else:
            row.append(_btn(i + 1, "secondary", True, f"qc_{i}"))
    return [row]


def mc_rows(answers: list, ended: bool, correct_index: int | None = None
            ) -> list[list[dict]]:
    """Multiple-choice rows. Labels are EXACTLY what the admin typed (D6).
    Ended: correct button green, wrong buttons red, all disabled."""
    row = []
    for i, a in enumerate(answers):
        if ended:
            if i == correct_index:
                row.append(_btn(a, "success", True, f"mc_{i}"))
            else:
                row.append(_btn(a, "danger", True, f"mc_{i}"))
        else:
            row.append(_btn(a, "secondary", False, f"mc_{i}"))
    return [row]


def join_rows(emoji: str, label: str = "Join", disabled: bool = False
              ) -> list[list[dict]]:
    return [[_btn(label, "primary", disabled, "join", emoji=emoji)]]


def initial_component_rows(game_type: str, config: dict) -> list[list[dict]]:
    """
    The component rows the game message is POSTED with — pure data, no
    discord dependency. Consumed by:
      * the bot: MinigameEngine._make_view() renders these into Buttons
      * the Dashboard live preview (Phase 4/5): rendered as HTML
    This is the single source of truth for what the game looks like.
    """
    if game_type == "quick_click":
        return qc_rows(_clamp_buttons(config), False)
    if game_type in ("math", "colors", "emoji"):
        return mc_rows(config.get("answers") or [], False)
    if game_type == "wheel":
        return join_rows("🎡")
    if game_type == "rps":
        return join_rows("✊")
    raise ValueError(f"unknown game_type: {game_type!r}")


def _clamp_buttons(config: dict) -> int:
    try:
        n = int((config or {}).get("buttons") or 4)
    except (TypeError, ValueError):
        n = 4
    return max(2, min(6, n))


def _fnum(config: dict, key: str, default: float) -> float:
    try:
        return float((config or {}).get(key) if (config or {}).get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def _parse_color(value):
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return discord.Color(value)
    s = str(value).strip()
    try:
        if s.startswith("#"):
            return discord.Color(int(s[1:], 16))
        return discord.Color(int(s))
    except (ValueError, TypeError):
        return None


def _grant_kwargs(rolled: dict) -> dict:
    """Map a rolled reward row onto give_reward()'s kwargs (plan §7)."""
    t, v = rolled["reward_type"], rolled["reward_value"]
    if t in ("coins", "diamonds", "xp"):
        try:
            return {"amount": int(v)}
        except (TypeError, ValueError):
            return {"amount": 0}
    if t in ("role", "temp_role"):
        return {"role_id": v, "duration_hours": rolled.get("duration_hours")}
    if t == "item":
        return {"item_name": v}
    return {}


async def _safe_ephemeral(interaction, text: str, view=None):
    """Best-effort ephemeral ack — a failed ack must never break the game."""
    try:
        await interaction.response.send_message(text, ephemeral=True, view=view)
        return getattr(interaction.response, "message", None)
    except Exception:
        try:
            await interaction.followup.send(text, ephemeral=True, view=view)
            return None
        except Exception:
            return None


# ── BASE ENGINE ─────────────────────────────────────────────────────────

class MinigameEngine:
    """
    State machine + lifecycle for one live game.

    snapshot: dict built by the spawn path (Phase 3):
      {guild_id, template_id, name, game_type, category_id,
       category_name, embed (dict), config (dict), rewards (list)}
    mode: "auto" | "manual" | "test"
    log_id: the run row opened by the spawn path BEFORE start().
    """

    TYPE = None

    def __init__(self, snapshot: dict, mode: str, log_id: int, bot=None):
        self.snapshot = snapshot or {}
        self.mode = mode
        self.log_id = log_id
        self.bot = bot
        self.guild_id = self.snapshot.get("guild_id")
        self.cfg = self.snapshot.get("config") or {}
        self.rewards = self.snapshot.get("rewards") or []
        self.participants: dict[int, dict] = {}
        self.finished = False
        self._lock = asyncio.Lock()
        self._timers: list[asyncio.Task] = []
        self._message = None
        self._view = None

    # ── lifecycle (discord glue) ───────────────────────────────────────

    def build_embed(self) -> discord.Embed:
        """Render the admin-designed embed (from stored JSON). Test mode
        gets a [Test] title/description prefix (D4)."""
        d = self.snapshot.get("embed") or {}
        e = discord.Embed()
        title = (d.get("title") or "")
        desc = (d.get("description") or "")
        if self.mode == "test":
            if title:
                title = f"[Test] {title}"
            else:
                desc = ("[Test] " + desc).strip()
        if title:
            e.title = title[:256]
        if desc:
            e.description = desc[:4096]
        color = _parse_color(d.get("color"))
        if color is not None:
            e.color = color
        else:
            e.color = discord.Color(0x5865F2)
        author = d.get("author")
        if author:
            try:
                e.set_author(name=(author or "")[:256])
            except discord.InvalidArgument:
                pass
        if d.get("image"):
            try:
                e.set_image(url=str(d["image"]))
            except discord.InvalidArgument:
                pass
        if d.get("thumbnail"):
            try:
                e.set_thumbnail(url=str(d["thumbnail"]))
            except discord.InvalidArgument:
                pass
        if d.get("footer"):
            e.set_footer(text=str(d["footer"])[:2048])
        for f in (d.get("fields") or [])[:25]:
            try:
                e.add_field(name=(f.get("name") or "Field")[:256],
                            value=(f.get("value") or "")[:1024],
                            inline=bool(f.get("inline")))
            except discord.InvalidArgument:
                continue
        return e

    def component_rows(self) -> list[list[dict]]:
        raise NotImplementedError

    def final_component_rows(self) -> list[list[dict]]:
        """Rows used on the final edit (all buttons disabled)."""
        return [[_btn(self.snapshot.get("name") or "Game", "secondary",
                      True, "done")]]

    def _arm_timers(self):
        """Subclass arms its own timers here."""

    def _arm(self, factory, delay: float) -> None:
        task = asyncio.create_task(self._delayed(factory, delay))
        self._timers.append(task)

    async def _delayed(self, factory, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            if not self.finished:
                await factory()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[MINIGAMES] run {self.log_id} timer error: {e}")
            traceback.print_exc()

    async def _cancel_timers(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers.clear()

    def _make_view(self, rows: list[list[dict]]) -> discord.ui.View:
        """Adapter: plain component rows → discord.ui.View. The engine
        stays the brain; this is the only discord.ui construction."""
        view = discord.ui.View(timeout=None)  # engine owns the lifecycle
        view.engine = self
        for row in rows:
            for c in row:
                b = discord.ui.Button(
                    label=c["label"],
                    style=_DISCORD_STYLE[c["style"]],
                    disabled=c.get("disabled", False),
                    custom_id=c["custom_id"],
                )
                b.callback = functools.partial(
                    self._on_button, route=c.get("_route", "main"))
                view.add_item(b)
        return view

    async def _on_button(self, interaction: discord.Interaction,
                         route: str = "main") -> None:
        try:
            info = {"id": interaction.user.id,
                    "name": snapshot_user(interaction.user)["display_name"],
                    "mention": interaction.user.mention}
            await self._dispatch(interaction, route,
                                 interaction.data.get("custom_id", ""), info)
        except Exception as e:
            print(f"[MINIGAMES] run {self.log_id} dispatch error: {e}")
            traceback.print_exc()

    async def _redraw(self, rows: list[list[dict]] | None = None) -> None:
        """Re-render the live message (best effort during play)."""
        if not self._message:
            return
        self._view = self._make_view(rows if rows is not None
                                     else self.component_rows())
        try:
            await self._message.edit(embed=self.build_embed(), view=self._view)
        except Exception as e:
            print(f"[MINIGAMES] run {self.log_id} mid-game edit failed: {e}")

    async def start(self, channel) -> "discord.Message":
        """Post the game message and arm timers. The spawn path (Phase 3)
        opened the run row BEFORE calling this, so a send failure still
        leaves an auditable row (finalized as failed by the caller).
        Raises ValueError on a template that cannot be played (preflight)
        — the caller marks the spawn failed and moves on."""
        self.preflight()
        self._view = self._make_view(self.component_rows())
        self._message = await channel.send(embed=self.build_embed(),
                                           view=self._view)
        self._arm_timers()
        return self._message

    def preflight(self) -> None:
        """Override to refuse unplayable templates (raises ValueError)."""

    def _participant(self, info: dict) -> None:
        self.participants[info["id"]] = {
            "id": info["id"], "display_name": info["name"],
            "mention": info.get("mention") or ("<@" + str(info["id"]) + ">"),
        }

    # ── THE single authoritative resolution ────────────────────────────

    async def _claim(self) -> bool:
        """Atomic check-and-set of `finished` (no awaits between the check
        and the set) — exactly one caller can ever claim the game."""
        async with self._lock:
            if self.finished:
                return False
            self.finished = True
            return True

    async def resolve(self, winners: list[dict] | None = None,
                      status: str = "completed",
                      result_text: str = "") -> bool:
        """
        The ONLY path to finalization. Returns False (and does nothing)
        if the game was already resolved — concurrent clicks, a timer
        racing a click, or a double invocation can never resolve twice.

        Order (plan §18): rolls+grants → best-effort embed edit → log.
        """
        winners = winners or []
        if not await self._claim():
            return False
        await self._cancel_timers()
        await self._disable_ephemeral_views()

        # 1) independent rolls + grants — BEFORE any embed work
        granted = []
        for w in winners:
            entry = {"id": w["id"], "name": w.get("name") or w.get("display_name") or "?"}
            if not self.rewards:
                # D11: empty pool is valid — winner announced, nothing granted
                entry.update(reward_type=None, reward_value=None,
                             status="no_reward")
                granted.append(entry)
                continue
            rolled = store.roll_reward(self.rewards)
            entry.update(reward_type=rolled["reward_type"],
                         reward_value=rolled["reward_value"], status="won")
            try:
                res = await give_reward(
                    self.bot, self.guild_id, w["id"], rolled["reward_type"],
                    **_grant_kwargs(rolled),
                    reason="Minigame reward (%s)"
                           % (self.snapshot.get("name") or "?"),
                    source="minigame")
                if not res or not res.get("success"):
                    entry["status"] = "failed"
                    entry["error"] = str((res or {}).get("error")
                                         or "grant failed")[:300]
            except Exception as e:
                entry["status"] = "failed"
                entry["error"] = str(e)[:300]
            granted.append(entry)

        # 2) best-effort final embed + disabled components
        if self._message:
            embed = self.build_embed()
            if result_text:
                base = embed.description or ""
                embed.description = (base + "\n\n" + result_text)[:4096]
            self._view = self._make_view(self.final_component_rows())
            try:
                await self._message.edit(embed=embed, view=self._view)
            except Exception as e:
                # A failed edit NEVER loses rewards — they are already safe.
                print(f"[MINIGAMES] run {self.log_id}: final embed edit "
                      f"failed (rewards already delivered): {e}")

        # 3) close the run row
        try:
            await store.finish_run(
                self.log_id, status,
                participants=list(self.participants.values()),
                winners=granted)
        except Exception as e:
            print(f"[MINIGAMES] run {self.log_id}: run log finalize failed: {e}")
        return True

    async def _disable_ephemeral_views(self) -> None:
        """Override in engines that hand out private interactive views
        (RPS). Best effort."""

    async def _dispatch(self, interaction, route: str,
                        custom_id: str, info: dict) -> None:
        raise NotImplementedError


# ── QUICK CLICK / REFLEX (plan §6.1) ────────────────────────────────────

class QuickClickEngine(MinigameEngine):
    TYPE = "quick_click"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.buttons = _clamp_buttons(self.cfg)
        self.reveal_min = _fnum(self.cfg, "reveal_min", 3.0)
        self.reveal_max = _fnum(self.cfg, "reveal_max", 8.0)
        if self.reveal_max < self.reveal_min:
            self.reveal_min, self.reveal_max = self.reveal_max, self.reveal_min
        self.wait_after = _fnum(self.cfg, "wait_after", 10.0)
        self.revealed = False
        self.green_index: int | None = None

    def component_rows(self):
        return qc_rows(self.buttons, self.revealed, self.green_index)

    def final_component_rows(self):
        return qc_rows(self.buttons, False)  # all disabled after the end

    def _arm_timers(self):
        self._arm(self._do_reveal,
                  random.uniform(self.reveal_min, self.reveal_max))

    async def _do_reveal(self):
        if self.finished:
            return
        self.green_index = random.randrange(self.buttons)  # D2: random position
        self.revealed = True
        await self._redraw()
        self._arm(self._on_wait_timeout, self.wait_after)

    async def _on_wait_timeout(self):
        if self.finished:
            return
        await self.resolve(status="no_winner",
                           result_text="😴 **No one made it in time.**")

    async def _dispatch(self, interaction, route, custom_id, info):
        # Disabled-before-reveal is structural (the buttons are disabled);
        # the flags below are the engine-side double guard.
        if self.finished or not self.revealed:
            return
        m = re.fullmatch(r"qc_(\d+)", custom_id or "")
        if not m or int(m.group(1)) != self.green_index:
            return
        self._participant(info)
        await self.resolve([info], status="completed",
                           result_text="🏆 **%s** hit the green button "
                                        "first!" % info["name"])


# ── WHEEL (plan §6.2) ───────────────────────────────────────────────────

class WheelEngine(MinigameEngine):
    TYPE = "wheel"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.join_seconds = _fnum(self.cfg, "join_seconds", 30.0)

    def component_rows(self):
        return join_rows("🎡", disabled=self.finished)

    def final_component_rows(self):
        return join_rows("🎡", label="Wheel finished", disabled=True)

    def _arm_timers(self):
        self._arm(self._on_time, self.join_seconds)

    async def _on_time(self):
        if self.finished:
            return
        async with self._lock:
            if self.finished:
                return
            joiners = list(self.participants.values())
        if not joiners:
            await self.resolve(status="no_winner",
                               result_text="😴 **No one joined the wheel.**")
            return
        winner = random.choice(joiners)  # exactly one, uniform (plan §6.2)
        n = len(joiners)
        plural = "s" if n != 1 else ""
        await self.resolve([winner], status="completed",
                           result_text="🎡 **%s** wins the wheel! "
                                        "(%d participant%s)"
                                        % (winner["display_name"], n, plural))

    async def _dispatch(self, interaction, route, custom_id, info):
        if self.finished or custom_id != "join":
            return
        async with self._lock:
            if self.finished:
                return
            already = info["id"] in self.participants
            if not already:
                self._participant(info)
        if already:
            await _safe_ephemeral(interaction, "You're already in!")
        else:
            await _safe_ephemeral(interaction, "You're in!")


# ── MULTIPLE CHOICE — math / colors / emoji (plan §6.3) ─────────────────

class MultipleChoiceEngine(MinigameEngine):
    """
    Shared engine for the three content types. The admin supplies the
    answer labels (exact text on the buttons — D6), the correct index,
    and the timer. Players may change their answer; the LAST answer
    committed before the game ends is the one that counts. During the
    game NOTHING about counts/identities/choices is revealed.
    """

    TYPE = "math"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.answers = [str(a) for a in (self.cfg.get("answers") or [])]
        self.answers = self.answers[:6]
        try:
            self.correct = int(self.cfg.get("correct", -1))
        except (TypeError, ValueError):
            self.correct = -1
        if not (0 <= self.correct < len(self.answers)):
            self.correct = -1
        self.seconds = _fnum(self.cfg, "seconds", 30.0)
        self.selections: dict[int, int] = {}  # user_id → answer index

    def preflight(self):
        if not self.answers:
            raise ValueError("template has no answers — cannot spawn")

    def component_rows(self):
        return mc_rows(self.answers, self.finished, self.correct)

    def final_component_rows(self):
        return mc_rows(self.answers, True, self.correct)

    def _arm_timers(self):
        if not self.answers:
            return  # broken template — the spawn path should have refused
        self._arm(self._on_time, self.seconds)

    async def _on_time(self):
        if self.finished or not self.answers:
            return
        async with self._lock:
            if self.finished:
                return
            winners = [self.participants[i] for i, a in self.selections.items()
                       if a == self.correct] if self.correct >= 0 else []
        if winners:
            names = " & ".join("**%s**" % w["display_name"]
                               for w in winners)
            plural = "s" if len(winners) != 1 else ""
            await self.resolve(winners, status="completed",
                               result_text=(f"✅ **Correct answer: "
                                            f"{self.answers[self.correct]}**\n"
                                            f"🏆 Winner{plural}: "
                                            f"{names}"))
        else:
            reveal = (f"✅ **Correct answer: {self.answers[self.correct]}**\n"
                      f"❌ **No one got it right.**") if self.correct >= 0 else \
                     "❌ **Game ended — no valid correct answer configured.**"
            await self.resolve([], status="no_winner", result_text=reveal)

    async def _dispatch(self, interaction, route, custom_id, info):
        m = re.fullmatch(r"mc_(\d+)", custom_id or "")
        if not m:
            return
        idx = int(m.group(1))
        if not (0 <= idx < len(self.answers)):
            return
        async with self._lock:
            if self.finished:
                return  # late click — the game already resolved
            self.selections[info["id"]] = idx  # last answer wins
            self._participant(info)
        await _safe_ephemeral(
            interaction, "✅ Answer set — you can change it until time "
                         "runs out.")


# ── ROCK PAPER SCISSORS (plan §6.4) ─────────────────────────────────────

class RpsEngine(MinigameEngine):
    """
    First two joiners are seated. Each seated player receives a PRIVATE
    (ephemeral) row of Rock/Paper/Scissors buttons the moment they seat
    (their own join interaction is the only way to reach them — Discord
    cannot target shared-message buttons to a user). First choice locks;
    the game resolves immediately once both have chosen.
    NO FORFEIT (D3): seating timeout or choice timeout → no winner,
    no reward. Draw → no reward.
    """

    TYPE = "rps"
    CHOICES = ("rock", "paper", "scissors")
    BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    EMOJI = {"rock": "✊", "paper": "✋", "scissors": "✌️"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seating_seconds = _fnum(self.cfg, "seating_seconds", 60.0)
        self.choice_seconds = _fnum(self.cfg, "choice_seconds", 60.0)
        self.seats: list[dict] = []
        self.choices: dict[int, str] = {}
        self._choice_views: dict[int, tuple] = {}  # uid → (view, msg|None)

    def component_rows(self):
        if len(self.seats) < 2:
            return join_rows("✊")
        return [[_btn("⏳ Awaiting choices…", "secondary", True, "rps_wait")]]

    def final_component_rows(self):
        return [[_btn("✊ RPS", "secondary", True, "rps_done")]]

    def _arm_timers(self):
        self._arm(self._on_seating_timeout, self.seating_seconds)

    async def _on_seating_timeout(self):
        if self.finished:
            return
        async with self._lock:
            if self.finished:
                return
            full = len(self.seats) >= 2
        if full:
            return  # both seated — the choice timer governs from here
        await self.resolve(status="no_winner",
                           result_text="⏰ **Game ended — no second player "
                                       "joined in time.**")

    async def _on_choice_timeout(self):
        if self.finished:
            return
        async with self._lock:
            if self.finished:
                return
            missing = [s for s in self.seats if s["id"] not in self.choices]
        if not missing:
            return  # both chose — resolution already ran / is running
        names = " and ".join("**%s**" % m["display_name"] for m in missing)
        await self.resolve(status="no_winner",
                           result_text=(f"⏰ **{names}** didn't pick in "
                                        f"time — game over, no reward."))

    def _make_choice_view(self, uid: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.engine = self
        for choice in self.CHOICES:
            b = discord.ui.Button(
                label=choice.title(), style=discord.ButtonStyle.secondary,
                custom_id=f"rps_{uid}_{choice}")
            b.emoji = self.EMOJI[choice]
            b.callback = functools.partial(self._on_button, route="choice")
            view.add_item(b)
        return view

    async def _hand_out_choices(self, interaction, only_uid: int | None):
        """Give each not-yet-handed-out seated player their private row.
        A player can only be reached via their OWN join interaction, so
        we hand out to whoever is the clicker (and, on the second
        seating, only them — the first player was served at their own
        join)."""
        for seat in self.seats:
            if only_uid is not None and seat["id"] != only_uid:
                continue
            if seat["id"] in self._choice_views:
                continue
            view = self._make_choice_view(seat["id"])
            if seat["id"] == interaction.user.id:
                waiting = len(self.seats) < 2
                text = ("You're seated! Pick your throw — it locks when "
                        "you press. "
                        + ("Waiting for a second player…" if waiting
                           else "Both players are seated — pick before time "
                              "runs out!"))
                msg = await _safe_ephemeral(interaction, text, view=view)
                self._choice_views[seat["id"]] = (view, msg)

    async def _disable_ephemeral_views(self):
        for uid, (view, msg) in self._choice_views.items():
            for item in view.children:
                item.disabled = True
            if msg is not None:
                try:
                    await msg.edit(view=view)
                except Exception:
                    pass

    async def _dispatch(self, interaction, route, custom_id, info):
        if route == "choice":
            await self._on_choice(interaction, custom_id, info)
            return
        if self.finished or custom_id != "join":
            return
        async with self._lock:
            if self.finished:
                return
            seated_ids = [s["id"] for s in self.seats]
            if len(self.seats) >= 2 or info["id"] in seated_ids:
                seated_now = None
            else:
                self._participant(info)
                self.seats.append({"id": info["id"], "name": info["name"],
                                   "display_name": info["name"],
                                   "mention": info.get("mention")
                                   or ("<@" + str(info["id"]) + ">")})
                seated_now = self.seats[-1]
        if seated_now is None:
            await _safe_ephemeral(
                interaction, "The table is full — two players are already "
                             "seated.")
            return
        if len(self.seats) == 2:
            await self._redraw()
            await self._hand_out_choices(interaction, only_uid=seated_now["id"])
            self._arm(self._on_choice_timeout, self.choice_seconds)
        else:
            # First seat: private row now, so they can lock a throw while
            # waiting for player 2.
            await self._hand_out_choices(interaction, only_uid=seated_now["id"])

    async def _on_choice(self, interaction, custom_id, info):
        m = re.fullmatch(r"rps_(\d+)_(rock|paper|scissors)", custom_id or "")
        if not m:
            return
        uid, choice = int(m.group(1)), m.group(2)
        if uid != info["id"]:
            await _safe_ephemeral(interaction,
                                  "That choice button isn't yours.")
            return
        async with self._lock:
            if self.finished:
                return
            if uid not in [s["id"] for s in self.seats]:
                return
            if uid in self.choices:
                return  # first choice locks
            self.choices[uid] = choice
            both = len(self.choices) >= 2
        if both:
            await self._resolve_rps()
            return
        await _safe_ephemeral(interaction,
                              f"{self.EMOJI[choice]} Choice locked!")

    async def _resolve_rps(self):
        p1, p2 = self.seats
        c1 = self.choices.get(p1["id"])
        c2 = self.choices.get(p2["id"])
        if not c1 or not c2:
            return
        e1, e2 = self.EMOJI[c1], self.EMOJI[c2]
        n1, n2 = p1["display_name"], p2["display_name"]
        detail = (f"{e1} **{n1}** played {c1.title()} · "
                  f"{e2} **{n2}** played {c2.title()}")
        if c1 == c2:
            await self.resolve([], status="no_winner",
                               result_text=detail + "\n🤝 **Draw — no reward.**")
            return
        winner = p1 if self.BEATS[c1] == c2 else p2
        await self.resolve([winner], status="completed",
                           result_text=detail + "\n🏆 **"
                                              + winner["display_name"]
                                              + "** wins!")


# ── REGISTRY ────────────────────────────────────────────────────────────

ENGINE_TYPES = {
    "quick_click": QuickClickEngine,
    "wheel": WheelEngine,
    "math": MultipleChoiceEngine,
    "colors": MultipleChoiceEngine,
    "emoji": MultipleChoiceEngine,
    "rps": RpsEngine,
}


def make_engine(snapshot: dict, mode: str, log_id: int, bot=None) -> MinigameEngine:
    gtype = (snapshot or {}).get("game_type")
    if gtype not in ENGINE_TYPES:
        raise ValueError(f"unknown game_type: {gtype!r}")
    engine = ENGINE_TYPES[gtype](snapshot, mode, log_id, bot=bot)
    engine.TYPE = gtype  # keep the effective type (colors/emoji share a class)
    return engine
