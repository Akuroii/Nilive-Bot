#!/usr/bin/env python3
"""
Standalone diagnostic — verifies whether the bot APPLICATION currently owns
CHECK_EMOJI_ID (1549831102078787744) against LIVE Discord.

Does NOT modify utils/emoji.py, the Mission resolver, or the fallback
behavior. Read-only probe. Run from the project root with the real bot's
env (DISCORD_TOKEN set) on a machine with network access to discord.com.

Usage:
    python3 check_emoji_probe.py

Exit code 0 = confirmed owned by the application (token will render).
Exit code 1 = confirmed NOT owned (Discord 404'd it -- fallback '✅' will
              show on Mission panels until a new emoji is added/ID fixed).
Exit code 2 = inconclusive (network/auth issue -- token is used regardless,
              per the existing fallback policy: unproven != missing).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main():
    import discord
    from utils.emoji import (
        CHECK_EMOJI_ID, CHECK_EMOJI, verify_check_emoji,
        check_emoji_state, check_emoji_detail,
        CHECK_STATE_CONFIRMED, CHECK_STATE_MISSING,
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not set in environment -- cannot probe live Discord.")
        sys.exit(2)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    result = {}

    @client.event
    async def on_ready():
        try:
            token_str = await verify_check_emoji(client, force=True, timeout=10)
            result["token"] = token_str
            result["state"] = check_emoji_state()
            result["detail"] = check_emoji_detail()
        finally:
            await client.close()

    await client.start(token)

    print(f"Probed CHECK_EMOJI_ID = {CHECK_EMOJI_ID}")
    print(f"Expected token        = {CHECK_EMOJI}")
    print(f"Resolver state        = {result.get('state')}")
    print(f"Detail                = {result.get('detail')}")
    print(f"What Missions render  = {result.get('token')}")

    if result.get("state") == CHECK_STATE_CONFIRMED:
        print("\n✅ CONFIRMED: the application owns this emoji ID. "
              "Mission panels render the animated Check.")
        sys.exit(0)
    elif result.get("state") == CHECK_STATE_MISSING:
        print("\n❌ MISSING: Discord says the application does NOT own this "
              "emoji ID (404). Mission panels are showing the unicode ✅ "
              "fallback until this is fixed in the Developer Portal or the "
              "constant is updated.")
        sys.exit(1)
    else:
        print("\n⚠️  INCONCLUSIVE: could not get a definitive answer from "
              "Discord (network/auth/rate-limit). Per the existing fallback "
              "policy, the custom token is still used -- inconclusive is "
              "never treated as missing.")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
