# Workspace Loss and Recovery Status — 2026-10-31 (recorded after environment reset)

## What happened

The environment was reset at **15:54**, after the last work at **15:06**. This wiped:

- `/home/user/nilive-integration-20261031/` — the entire 593 MB preservation workspace
- `/tmp` in full — `/tmp/fx` (clone), `/tmp/p1-cand`, `/tmp/p1-dc`, `/tmp/p1-main`,
  `/tmp/p1venv`, all matrix logs, `DC.patch`, `run_matrix.sh`, the R1 reproducers,
  `verify_*.py`, `semantic_review_r2.py`

**Lesson recorded:** the snapshot preserves `/home/user/Nilive-Bot/` (the repository
checkout). It did **not** preserve a sibling directory under `/home/user/`. Any
artifact that must survive has to live **inside the repository directory** (untracked
is fine — it does not touch the git tree) or be exported by the user.

---

## Recovery inventory

| Artifact | Status | Evidence |
|---|---|---|
| 53-commit chain `a6b812b..f9e413f` | ✅ **Recoverable** | `refs/heads/arena/c014328f-nilive-bot` = `f9e413f…` on GitHub |
| **Design C patch** | ✅ **Recovered, hash-identical** | `review/g-purchase-grant-ledger:review-materials/0001-G-purchase-grant-ledger.patch` — 118,342 B, sha256 `6d94d9a11d2b3c679068fd39473e22a3c363f07fffc9a7d0f1ffe98ec4d0d4fd` — **exact match** to the `DC.patch` used in Phases 0–5 |
| Design C report | ✅ Recoverable | `review-materials/G-report.md` |
| R1 / R2 reproducer **outputs** | ✅ Recoverable | `reproduce_inventory_ambiguity.output.txt`, `reproduce_booster_obligation_gap.output.txt` |
| Reports (Phase 0 / 1 / 1.5 / audit) | ✅ **Survived** | untracked in `/home/user/Nilive-Bot/`, hashes below |
| **`phase1.5.patch`** | ❌ **LOST** | was 58,432 B, sha256 `6a6fe2fe…` — no copy anywhere |
| R2 fix code (`utils/timed_ownership.py`, `utils/level_claims.py`) | ❌ **LOST** | — |
| Drop restoration (`wallet.py`, `cogs/wallet.py`) | ❌ **LOST** | — |
| Startup change (`main.py`, `start.sh`) | ❌ **LOST** | — |
| 8 R2 tests (`test_role_retention_obligations.py`) | ❌ **LOST** | — |
| `run_matrix_v2.sh`, `semantic_review_r2.py`, `verify_*.py` | ❌ **LOST** | — |
| Matrix TSVs (`main-v2`, `cand-v2`) | ❌ **LOST** | results recorded in the audit report only |

Surviving report hashes:

```
8c336ead4066…  PHASE0_PREFLIGHT_REPORT.md          38,716 B
0a4f0bf96620…  PHASE1_VALIDATION_REPORT.md         35,632 B
4384beca327a…  PHASE1_5_CANDIDATE_HARDENING_REPORT.md  16,016 B
74bf08b161f3…  PRESERVATION_AUDIT_REPORT.md        14,803 B
```

---

## Independent verification of the external review (done on the recovered tree)

Three code-level claims from the review were re-checked against
`f9e413f` + the hash-verified Design C patch. **All three are confirmed.**

### 1. The startup change converts transient errors into permanent death

`main.py:337`:

```python
async def main():
    try:
        await init_db()
        await load_cogs()
        await bot.start(DISCORD_TOKEN)      # ← same try as init_db()
    except discord.LoginFailure:
        ...
        sys.exit(1)
    except Exception as e:                   # ← line 355
        ...
```

`bot.start()` shares the handler that Phase 1.5 changed to `sys.exit(1)`, and
`start.sh` halts on code 1. A transient gateway/network error therefore kills the
bot permanently while `dashboard/app.py` keeps running — the container looks
healthy. The earlier "transient crash still restarts" test exercised a *stub*
exit code, never this path.

### 2. Decision E has a startup race

`database.py:1555-1563`: check-then-`ALTER` with a bare `except Exception` that
raises `RuntimeError`. The bot and the dashboard both run `init_db()`, so on the
first deploy the loser of a concurrent `ALTER` gets
`duplicate column name: entry_key` and aborts startup — precisely when the
migration is needed.

### 3. `role_retention` is not excluded by every reader

`utils/level_test_reset.py:489`:

```sql
SELECT id, role_id, item_name, expires_at FROM timed_holdings
WHERE guild_id=? AND user_id=?
```

No `item_type` filter, and **0 occurrences of `role_retention` in the file**. A
retention obligation carries a `role_id` and `expires_at = 9999-12-31…`, so it is
counted as an "other grant" for that role. The Phase 1.5 audit claimed "all readers
exclude retention" and was wrong about this one.

---

## Consequence

Phase 1.5 is lost **and** independently shown to contain three real defects. There
is nothing in it to "recover" — only to redo, smaller and corrected. The chain plus
Design C is the defensible merge unit.
