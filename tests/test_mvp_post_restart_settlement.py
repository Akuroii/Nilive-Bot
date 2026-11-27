"""Post-restart MVP settlement: the loop must exist, run, and settle exactly once.

The pre-launch failure was that mvp_cycle_task.start() ran at import time, its
before_loop awaited wait_until_ready() pre-login, and the task died before the
first tick -- so no guild cycle ever settled in production. This asserts the
loop is alive before READY, that it survives the wait, and that the real
utils.mvp_engine.settle_cycle then settles a due cycle exactly once.

Read-only: scratch DB, no Discord network, no role changes.

    python tests/test_mvp_post_restart_settlement.py     # exit 0 = all pass
"""
import asyncio, os, sys, tempfile, traceback
from datetime import datetime, timedelta, timezone

REPO="/home/user/Nilive-Bot"; sys.path.insert(0,REPO)
os.environ.update({"DATABASE_PATH": os.path.join(tempfile.mkdtemp(prefix="mvp-"),"s.db"),
    "NERO_ENVIRONMENT":"production","OWNER_ID":"704453350384730237",
    "SECRET_KEY":"m"*48,"DISCORD_TOKEN":"M"*24+"."+"A"*6+"."+"B"*27})
G=1413528046785101865
res=[]
def ck(n,ok,d=""):
    res.append(ok); print(f"[{'PASS' if ok else 'FAIL'}] {n}"+(f" :: {d}" if d else "")); return ok

class G_:
    id=G; name="S"; icon=None; owner_id=704453350384730237; member_count=2
    def get_member(self, mid): return None
    def get_role(self, rid): return None
    def get_channel(self, cid): return None
    async def fetch_member(self, mid): raise LookupError(mid)
class Bot:
    def __init__(s): s.guilds=[G_()]; s._r=False
    def is_ready(s): return s._r

async def main():
    from utils import mvp_engine as E
    import database, aiosqlite
    from cogs.mvp import MVP
    await database.init_db()
    bot=Bot(); cog=MVP(bot)
    loop=cog.mvp_cycle_task
    ck("mvp_cycle_task is started at cog construction", loop.is_running())
    ck("ensure_started does not double-start it", __import__("utils.task_lifecycle",fromlist=["x"]).ensure_started(loop) is False)

    # pre-READY: loop must be alive but NOT yet ticking
    await asyncio.sleep(0.9)
    _t = loop.get_task()
    ck("loop survives until READY (would have died pre-login before the fix)",
       loop.is_running() and not _t.done())

    # configure a cycle and give the guild a score
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        await db.execute("""INSERT INTO mvp_config (guild_id, enabled, cycle_hours,
            winner_ranks, chat_word_weight, voice_minute_weight,
            min_activity_words, rank_rewards) VALUES (?,1,6,1,1.0,2.0,0,'[10]')
            ON CONFLICT(guild_id) DO UPDATE SET cycle_hours=6, winner_ranks=1,
            rank_rewards='[10]'""", (G,))
        await db.commit()
    start=(datetime.now(timezone.utc)-timedelta(hours=7)).isoformat()
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        await db.execute("INSERT INTO mvp_cycle_state (guild_id,cycle_start,cycle_hours) VALUES (?,?,6) ON CONFLICT(guild_id) DO UPDATE SET cycle_start=excluded.cycle_start",(G,start))
        await db.execute("INSERT OR REPLACE INTO mvp_cycle_scores (guild_id,cycle_start,user_id,message_score,total_score,last_activity_at) VALUES (?,?,?,?,?,?)",(G,start,555,50,50,start))
        await db.commit()

    bot._r=True                       # READY arrives
    first=await E.settle_cycle(bot, bot.guilds[0])
    ck("a due cycle settles after READY", bool(first), str(first.get("cycle_id") if first else None))
    second=await E.settle_cycle(bot, bot.guilds[0])
    ck("a second pass does NOT pay twice (idempotent)", second is None, f"second={second}")
    print("\n===== %d/%d checks passed =====" % (sum(res),len(res)))
    return 0 if all(res) else 1

try: sys.exit(asyncio.run(main()))
except SystemExit: raise
except Exception: traceback.print_exc(); sys.exit(2)
