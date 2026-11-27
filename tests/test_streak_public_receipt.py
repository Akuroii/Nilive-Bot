"""Streak public-receipt: configuration validation + delivery outcome reporting.

Covers the reported "public receipt channel does not work":
  * Public/Both must not be savable without a channel.
  * Preferences saved BEFORE that rule existed must not silently deliver
    nothing -- every failure path must be logged and reported as not delivered.
  * A failed receipt must never look like a failed claim.

Read-only. Scratch DB outside the repo; Discord objects are local doubles.

    python tests/test_streak_public_receipt.py     # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SCRATCH_DIR = tempfile.mkdtemp(prefix="nilive-receipt-")
os.environ.update({
    "DATABASE_PATH": os.path.join(SCRATCH_DIR, "scratch.db"),
    "NERO_ENVIRONMENT": "production",
    "OWNER_ID": "704453350384730237",
    "SECRET_KEY": "r" * 48,
    "DISCORD_TOKEN": "M" * 24 + "." + "A" * 6 + "." + "B" * 27,
})

GUILD, OTHER_GUILD, USER = 1413528046785101865, 1413528046785101999, 555555555555555555
CHANNEL = 980000000000000001

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return ok


# ── Discord doubles (only what send_public_claim_receipt touches) ──────────
class Perms:
    def __init__(self, send_messages=True): self.send_messages = send_messages


class FakeMe:
    def __init__(self, send_messages=True): self.guild_permissions = Perms(send_messages)


class FakeGuild:
    def __init__(self, gid, send_messages=True):
        self.id = gid
        self.me = FakeMe(send_messages)


class FakeChannel:
    def __init__(self, gid, send_messages=True, raise_on_send=None):
        self.id = CHANNEL
        self.guild = FakeGuild(gid, send_messages)
        self.raise_on_send = raise_on_send
        self.sent = []

    async def send(self, **kwargs):
        if self.raise_on_send:
            raise self.raise_on_send
        self.sent.append(kwargs)


class FakeBot:
    def __init__(self, channel=None, fetch_raises=None):
        self._channel = channel
        self._fetch_raises = fetch_raises

    def get_channel(self, cid): return self._channel

    async def fetch_channel(self, cid):
        if self._fetch_raises:
            raise self._fetch_raises
        return self._channel


def claim_result(**over) -> dict:
    base = {
        "guild_id": GUILD, "user_id": USER, "streak": 3, "stage": 3,
        "base": 30, "streak_bonus": 0, "pre_prestige_total": 30,
        "multiplier": 1.0, "amount": 30, "credited_total": 30, "new_balance": 130,
        "bonus_capped": False, "v2_enabled": True, "receipt_visibility": "public",
        "receipt_channel_id": str(CHANNEL), "receipt_private": False,
        "receipt_public": True, "member_name": "Tester", "next_reset_seconds": 3600,
    }
    base.update(over)
    return base


CURRENCY = {"coins": {"emoji": "🪙", "name": "Coins"},
            "diamonds": {"emoji": "💎", "name": "Diamonds"}}


async def main():
    import database
    from utils import streak_settings as ss

    await database.init_db()
    from cogs import wallet as wallet_cog

    # ── 1. validation: public/both now REQUIRE a channel ───────────────────
    for mode in ("public", "both"):
        for blank in ("", None, "   "):
            rejected = False
            try:
                ss.validate_streak_settings([None] * 5, None, None, mode, blank)
            except ValueError as exc:
                rejected = "channel" in str(exc).lower()
            check(f"{mode} + blank channel ({blank!r}) is rejected", rejected,
                  "ValueError raised" if rejected else "ACCEPTED — the unpreventable-state bug")

    ok = ss.validate_streak_settings([None] * 5, None, None, "public", str(CHANNEL))
    check("public + valid channel still saves", ok["receipt_channel_id"] == str(CHANNEL))

    ok = ss.validate_streak_settings([None] * 5, None, None, "both", str(CHANNEL))
    check("both + valid channel still saves", ok["receipt_channel_id"] == str(CHANNEL))

    ok = ss.validate_streak_settings([None] * 5, None, None, "private", "")
    check("private with no channel is still allowed (no regression)",
          ok["receipt_channel_id"] is None)

    # non-numeric channel still rejected
    bad = False
    try:
        ss.validate_streak_settings([None] * 5, None, None, "public", "abc")
    except ValueError:
        bad = True
    check("non-numeric channel is rejected", bad)

    # ── 2. persistence round-trip of a valid public config ────────────────
    saved = await ss.save_streak_settings(GUILD, {
        "stages": ["10", "20", "30", "40", "50"], "stage6_min": "60",
        "stage6_max": "90", "receipt_visibility": "public",
        "receipt_channel_id": str(CHANNEL)})
    check("valid public config persists and reads back",
          saved["receipt_visibility"] == "public"
          and saved["receipt_channel_id"] == str(CHANNEL),
          f"{saved['receipt_visibility']}/{saved['receipt_channel_id']}")

    # ── 3. delivery outcomes are reported distinctly ──────────────────────
    # 3a. success
    ch = FakeChannel(GUILD)
    sent = await wallet_cog.send_public_claim_receipt(FakeBot(ch), claim_result(), CURRENCY)
    check("delivered receipt returns True", sent is True and len(ch.sent) == 1,
          f"messages={len(ch.sent)}")

    # 3b. legacy config: public but NO stored channel -> reported, not silent
    sent = await wallet_cog.send_public_claim_receipt(
        FakeBot(None), claim_result(receipt_channel_id=None), CURRENCY)
    check("public with no stored channel returns False (claim still credited)", sent is False)

    # 3c. channel cannot be resolved
    sent = await wallet_cog.send_public_claim_receipt(
        FakeBot(None, fetch_raises=RuntimeError("404 Not Found")), claim_result(), CURRENCY)
    check("unresolvable channel returns False", sent is False)

    # 3d. channel belongs to a different guild -> must NOT post
    foreign = FakeChannel(OTHER_GUILD)
    sent = await wallet_cog.send_public_claim_receipt(FakeBot(foreign), claim_result(), CURRENCY)
    check("cross-guild channel is refused (no message sent)",
          sent is False and not foreign.sent, f"messages={len(foreign.sent)}")

    # 3e. missing Send Messages permission -> refuse before the API call
    noperm = FakeChannel(GUILD, send_messages=False)
    sent = await wallet_cog.send_public_claim_receipt(FakeBot(noperm), claim_result(), CURRENCY)
    check("missing Send Messages is detected and refused",
          sent is False and not noperm.sent, f"messages={len(noperm.sent)}")

    # 3f. Discord rejects the send -> False, not an exception
    failing = FakeChannel(GUILD, raise_on_send=RuntimeError("500 Internal Server Error"))
    sent = await wallet_cog.send_public_claim_receipt(FakeBot(failing), claim_result(), CURRENCY)
    check("Discord send failure returns False and does not raise", sent is False)

    # 3g. private mode never touches the receipt path
    priv = FakeChannel(GUILD)
    sent = await wallet_cog.send_public_claim_receipt(
        FakeBot(priv), claim_result(receipt_public=False, receipt_private=True), CURRENCY)
    check("private mode never sends a public receipt",
          sent is False and not priv.sent)

    # ── 4. a failed receipt must not look like a failed claim ─────────────
    # The member-facing acknowledgement distinguishes the two states.
    ack_ok = wallet_cog.build_public_only_ack_embed(claim_result(), True, CURRENCY)
    ack_bad = wallet_cog.build_public_only_ack_embed(claim_result(), False, CURRENCY)
    ok_txt = " ".join(str(f.value) for f in ack_ok.fields) + " " + str(ack_ok.description)
    bad_txt = " ".join(str(f.value) for f in ack_bad.fields) + " " + str(ack_bad.description)
    check("success ack says the receipt was posted", "configured channel" in ok_txt)
    check("failure ack says the reward WAS credited but the channel was unreachable",
          "credited" in bad_txt and "could not be reached" in bad_txt, bad_txt[:90])

    # ── 5. claim result wiring (no schema/economy change) ────────────────
    cfg = await ss.get_streak_settings(GUILD)
    priv_flag, pub_flag = ss.receipt_destinations(cfg["receipt_visibility"])
    check("public mode maps to public-only receipts",
          (priv_flag, pub_flag) == (False, True), f"{priv_flag},{pub_flag}")
    check("stored channel is what the claim would use",
          cfg["receipt_channel_id"] == str(CHANNEL))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} checks passed =====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} :: {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
