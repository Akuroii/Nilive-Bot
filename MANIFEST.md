# Voice_XP_fix — manifest

Base: `Akuroii/Nilive-Bot` `main` @ `6c2af9a`. Extract at the repository root. Nothing committed or pushed.

## Behaviour changed

ONE new setting, `voice_farming_guard` (`leveling_config`, default **1 = ON**), decides what **alone / deafened (self or server) / AFK channel** mean for Voice XP:

- ON (default): none of the three earns Voice XP (identical to the old behaviour).
- OFF: none of the three blocks Voice XP by itself; an otherwise-eligible member earns it.

`voice_require_unmuted` is untouched (mute only). `voice_xp_enabled` still wins over everything.

Architecture (smallest split, no new consumers broken):
- `cogs/activity_engine.py` keeps computing alone / AFK / deafened with its existing definitions (alone = fewer than 2 non-bot members in the channel; a deafened member still counts as a participant; AFK = `guild.afk_channel`). For members that fail them it still skips `activity_stats.voice_minutes` and the existing `activity_voice_tick` event, exactly as before (missions + mvp unaffected; same event, same flags dict, same recipients).
- It additionally dispatches a NEW event `activity_voice_xp_tick` for every real member in a voice channel, with the old flags plus `alone` and `afk`. Only Leveling listens to it.
- `cogs/leveling.py`: the listener is renamed `on_activity_voice_tick` -> `on_activity_voice_xp_tick` (so there is no double award) and applies the guard.

## Files

| File | Type | Change |
|---|---|---|
| `cogs/activity_engine.py` | production | dispatch `activity_voice_xp_tick` (adds `alone`/`afk`); legacy gating/event/`voice_minutes` unchanged |
| `cogs/leveling.py` | production | listener moved to the new event; `voice_farming_guard` check; comments updated |
| `database.py` | production | column in `leveling_config` CREATE + additive `ALTER TABLE ... ADD COLUMN ... DEFAULT 1` in `migrate_leveling_config` (existing guilds stay ON) |
| `utils/xp_calculator.py` | production | effective default `voice_farming_guard: 1` (GET + runtime for guilds with no row); one comment |
| `dashboard/api/leveling.py` | production | field validated (0/1, default 1) and saved |
| `dashboard/templates/systems/leveling.html` | production | new checkbox, load/save wiring, hint rewritten ("Controlled by this toggle ...") |
| `scripts/test_leveling_voice_hard_blocks.py` | test | REWRITTEN in place (the old version asserted the always-blocked behaviour): real engine -> real listener matrix, ON/OFF x alone / server-deaf / self-deaf / AFK / normal, mute independence, legacy event + `voice_minutes` unchanged, master switch, default ON, additive migration, Dashboard API -> DB -> runtime, UI wiring |
| `scripts/test_leveling_runtime.py` | test | listener renamed; the engine-gate test now filters `activity_voice_tick` (the legacy contract) and feeds Leveling the new event |
| `scripts/test_slice1_leveling_gate.py` | test | listener rename only |
| `scripts/test_xp_safety_audit.py` | test | source-slice anchor renamed to the new listener |

## Tests run (repo root) — all PASS

    python scripts/test_leveling_voice_hard_blocks.py     (73 checks)
    python scripts/test_leveling_runtime.py
    python scripts/test_slice1_leveling_gate.py
    python scripts/test_xp_safety_audit.py
    python scripts/test_leveling_config_migration.py
    python scripts/test_leveling_reset_config.py
    python scripts/test_level_reward_role_progression.py
    python scripts/test_leveling_reward_e2e.py
    python scripts/test_leveling_caller_integrations.py
    python scripts/test_leveling_final_fixes.py
    python scripts/test_leveling_claim_all_d2.py
    python scripts/test_slice2_level_claims.py
    python scripts/test_slice3_shop_claims.py
    python scripts/test_slice4_boost_claims.py
    python scripts/test_rejoin_reconciliation.py
    for t in scripts/test_*.py; do python "$t"; done      50 pass, 2 fail

Mutation checks (each makes `test_leveling_voice_hard_blocks.py` fail; sources restored afterwards): guard ignored/always-block, guard ignored/never-block, guard wired to `voice_require_unmuted`, engine stops reporting `alone`, engine stops reporting `afk`, legacy `activity_voice_tick` leaking for ineligible members, API dropping the column from the upsert, default flipped to OFF.

## Caveats

- `scripts/test_afk.py` and `scripts/test_shop_publications.py` still fail exactly as on pristine `main` (not touched, as instructed).
- Missions and MVP still listen to `activity_voice_tick` and are unchanged; only Leveling moved to the new event. Any third-party/unlisted code that called `Leveling.on_activity_voice_tick` directly would need the new name (none in the repo).
- Root-level duplicate files (`leveling.py`, `activity_engine.py`, ...) are not loaded by `main.py` and were not touched.
- Test runs rewrite tracked `__pycache__/*.pyc` files; they are not in this ZIP. Run `git checkout -- .` on those before committing.
