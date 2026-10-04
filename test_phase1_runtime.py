"""Script-entrypoint regression, real gates/loader, mocked Discord transport.

Execute main.py's declarations with __name__='__main__', removing ONLY the final
startup guard so no login occurs. Importing main normally would hide the bug.
No production source is rewritten, and no alternative gate is implemented here.
"""
import ast
import asyncio
import inspect
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from phase1_support import ROOT, GUILD, USER, reset_database, seed_member, execute, interaction
import discord
from cogs.command_aliases import CommandAliases, AliasGateError
from utils import command_gating


def script_declarations():
    path = ROOT / "main.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    guard = tree.body[-1]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test) in (
        "__name__ == '__main__'", '__name__ == "__main__"')
    tree.body.pop()
    module = ModuleType("__main__")
    module.__file__ = str(path)
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reset_database()
        seed_member()
        self.assertNotIn("main", sys.modules, "entrypoint test requires clean import state")
        self.runtime = script_declarations()
        self.bot = self.runtime.bot
        await self.bot._async_setup_hook()  # initialise readiness without login
        self.addAsyncCleanup(self.cleanup_bots)
        # Clear any future shared store to isolate test cases, without requiring
        # a new production symbol before Phase 2/3 implements it.
        for value in (getattr(self.runtime, "_command_cooldowns", None),
                      getattr(command_gating, "_command_cooldowns", None)):
            if value is not None:
                value.clear()
        execute("INSERT INTO command_toggles (guild_id,command_name,enabled,cooldown_seconds) "
                "VALUES (?,'rank',1,60)", (GUILD,))
        self.itx = interaction()
        self.itx.command = SimpleNamespace(qualified_name="rank")
        self.itx.channel_id = 8301
        self.ctx = SimpleNamespace(guild=self.itx.guild, author=self.itx.user,
                                   channel=SimpleNamespace(id=8301), bot=self.bot)
        cog = object.__new__(CommandAliases)
        cog.bot = self.bot
        self.alias = cog._make_gate("rank")

    async def cleanup_bots(self):
        imported = sys.modules.pop("main", None)
        if imported is not None and imported.bot is not self.bot:
            await imported.bot.close()
        await self.bot.close()
        await asyncio.sleep(0)  # let canceled loop tasks finish

    async def slash(self):
        return await self.bot.tree.interaction_check(self.itx)

    async def test_slash_then_alias_is_blocked(self):
        self.assertTrue(await self.slash())
        with self.assertRaisesRegex(AliasGateError, "[Ss]low|cooldown|try again"):
            await self.alias(self.ctx)

    async def test_alias_then_slash_is_blocked(self):
        self.assertTrue(await self.alias(self.ctx))
        self.assertFalse(await self.slash(), "alias and slash got separate cooldown windows")
        self.itx.response.send_message.assert_awaited_once()

    async def test_both_paths_pass_the_same_store_object_to_shared_evaluator(self):
        seen = []
        original = command_gating.evaluate_toggle_row
        def record(*args, **kwargs):
            seen.append(inspect.signature(original).bind(*args, **kwargs).arguments["cooldowns"])
            return original(*args, **kwargs)
        with patch.object(command_gating, "evaluate_toggle_row", side_effect=record):
            await self.slash()
            try:
                await self.alias(self.ctx)
            except AliasGateError:
                pass
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1], "same keys are not enough: store identity must match")

    async def test_alias_gate_does_not_construct_or_import_a_second_main_bot(self):
        self.assertTrue(await self.alias(self.ctx))
        imported = sys.modules.get("main")
        self.assertTrue(imported is None or imported.bot is self.bot,
                        "alias gate imported main and created a second Bot")
        self.assertIs(self.ctx.bot, self.bot)

    async def test_each_path_already_blocks_its_own_second_call(self):
        self.assertTrue(await self.slash())
        self.assertFalse(await self.slash())
        # A different member isolates this assertion from the slash cooldown.
        self.ctx.author = SimpleNamespace(id=USER + 1, roles=[])
        self.assertTrue(await self.alias(self.ctx))
        with self.assertRaises(AliasGateError):
            await self.alias(self.ctx)

    async def test_disabled_policy_is_identical_on_both_paths(self):
        execute("UPDATE command_toggles SET enabled=0")
        self.assertFalse(await self.slash())
        with self.assertRaises(AliasGateError):
            await self.alias(self.ctx)

    async def test_real_loader_has_one_canonical_rank_callback_and_one_reset_loop(self):
        # No login/sync/socket; background tasks wait on the real readiness Event.
        await self.runtime.load_cogs()
        self.assertEqual(self.bot.failed_cogs, [], "loader failure must not be hidden")
        self.assertIn("cogs.command_aliases", self.bot.loaded_cogs)
        commands = [cmd for cmd in self.bot.tree.walk_commands() if cmd.qualified_name == "rank"]
        self.assertEqual(len(commands), 1)
        rank = commands[0]
        self.assertEqual(rank.callback.__module__, "cogs.leveling")
        self.assertEqual(rank.callback.__qualname__, "Leveling.rank")
        cog = self.bot.get_cog("Leveling")
        self.assertIs(rank.binding, cog)
        loop = cog.leaderboard_reset_task
        task = loop.get_task()
        self.assertIsNotNone(task)
        self.assertTrue(loop.is_running())
        with self.assertRaises(discord.ext.commands.ExtensionAlreadyLoaded):
            await self.bot.load_extension("cogs.leveling")
        self.assertIs(loop.get_task(), task)
        self.assertEqual(sum(cmd.qualified_name == "rank" for cmd in self.bot.tree.walk_commands()), 1)
        # Registered callback -> real data -> real renderer; only remote assets
        # and Discord interaction transport are faked.
        from utils import rank_card_renderer
        from PIL import Image
        with patch.object(rank_card_renderer, "_fetch_image", new=AsyncMock(return_value=None)):
            await rank.callback(rank.binding, self.itx)
        self.itx.followup.send.assert_awaited_once()
        result = self.itx.followup.send.call_args.kwargs
        self.assertNotIn("embed", result, "fallback embed must not masquerade as render success")
        attachment = result["file"]
        self.assertEqual(attachment.filename, "rank.png")
        attachment.fp.seek(0)
        image = Image.open(attachment.fp)
        self.assertEqual(image.size, (1280, 853))
        self.assertEqual(image.format, "PNG")
        attachment.close()
        await self.bot.unload_extension("cogs.leveling")
        await asyncio.sleep(0)
        self.assertTrue(task.done(), "cog unload must cancel its existing task")
        self.assertIsNone(self.bot.tree.get_command("rank"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
