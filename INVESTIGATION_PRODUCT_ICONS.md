# Shop Product Icons — Production Investigation (evidence-based)

**Status: investigation only.** No code edits to application modules, no PR, no branch change,
no DB writes, no Railway changes, no emoji operations.

| Field | Value |
|---|---|
| Repository | `Akuroii/Nilive-Bot` |
| Deployed commit | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` (merge of PR #121) |
| Working checkout HEAD | `e0ca73853be97c5ebbf1e3dd66e70c76595ecd52` — **identical** |
| Commit diff vs HEAD | **none** (`git diff --stat e0ca738… HEAD` is empty) |
| Railway service | `Nilive-Bot`, env `production`, ID `e5c6b775-e2b1-4487-b036-70f159a5be7e` |
| Deployment | `1230fe25-28b1-4be7-a6f0-b7af61addf92` |
| Volume | `/app/data` → DB at `/app/data/nero.db` (`DATABASE_PATH` default) |

Because the checkout HEAD equals the deployed commit, every code statement below describes
the **exact deployed code**, not a nearby revision.

---

## 1. What I could actually access

| Capability | Available? | Evidence |
|---|---|---|
| Inspect the exact deployed commit | **Yes** | checkout HEAD == deployed SHA, zero diff |
| Run `gh` / git against the repo | **Yes** | `gh 2.23.0` authenticated |
| Access Railway logs | **No** | `railway` CLI not installed; no Railway token in the sandbox |
| Execute a command inside the production service | **No** | no shell/exec path into the Railway container |
| Inspect the mounted SQLite DB | **No** | `/app/data` is not reachable from this sandbox |
| Query Discord's application-emoji endpoint | **No** | no `DISCORD_TOKEN` / `DISCORD_CLIENT_ID` in this sandbox |
| Read Railway service variables | **No** | same as above |

`env` in the sandbox contains only `GH_TOKEN` / `GITHUB_TOKEN`. There is **no** production
credential, database, or log access here.

> **Nothing in this document is a production query result.** Every production value below is
> marked **[UNKNOWN]**. No rows, IDs, hashes, or statuses have been invented. The four
> product names come from your Railway log lines; nothing else about them is known.

The facts I *do* have from you: the four `rank_card: no usable icon … (tried 1 source(s))`
warnings, repeated in a later request, for those four product names.

---

## 2. Confirmed facts from the deployed code (read, not run)

These are properties of commit `e0ca738`, verified by reading the source.

**F1. Schema used by the pipeline** (created by `database.init_db()` →
`utils.shop_emoji.ensure_shop_emoji_schema()`):

- `shop_items(id, guild_id, name, …, icon_url, rarity, category_id, option_of_id)` — URL lives in `icon_url`.
- `item_catalog(id, guild_id, item_name, icon_url, rarity, value_currency, value_amount, updated_at)`, `UNIQUE(guild_id, item_name)`.
- `inventory_items(…, guild_id, user_id, item_name, item_type, quantity, metadata, …)`, `UNIQUE(guild_id, user_id, item_name)` — **no icon column, no product id**.
- `shop_emoji_assets` PK `(application_id, content_hash)`; columns include `emoji_id`, `emoji_name`, `animated`, `creator_guild_id`, `status CHECK IN ('preparing','ready','uncertain','missing','failed')`, `last_error`.
- `shop_emoji_url_aliases` PK `(application_id, guild_id, source_url)`; `content_hash` FK → assets (`ON DELETE RESTRICT`).

**F2. The alias key is the canonicalized URL.** `canonicalize_url()` lowercases host,
drops default port 443, strips fragment, and **preserves path and query byte-for-byte**.
Preparation stores `canonicalize_url(shop_items.icon_url)`; the read path canonicalizes the
same string. So save-time and read-time values agree *provided the stored `icon_url` is
unchanged between preparation and read*.

**F3. The read predicate is strict.** `attach_prepared_emojis()` joins
`shop_emoji_url_aliases a` → `shop_emoji_assets e` on
`a.application_id = ? AND a.guild_id = ? AND a.source_url IN (…) AND e.status = 'ready'`.
All four conditions must hold, **and** `e.emoji_id` must be digit-valid. Everything else
yields `app_emoji = ""` silently — there is no log line for a DB miss.

**F4. The application ID reaches both call sites correctly (this is what PR #121 added).**

- `cogs/leveling.py` `/rank` → `interaction.application_id or interaction.client.application_id` → `get_rank_card_data(..., application_id=…)`.
- `cogs/wallet.py` Inventory → same expression → `_decorate(..., application_id=…)` → `attach_prepared_emojis(..., application_id=…)`.

**F5. `find_listing_icon_urls()` requires an unambiguous listing.** It does
`SELECT name, MIN(icon_url), COUNT(*) FROM shop_items WHERE guild_id=? AND name IN (…) GROUP BY name`
and rejects a name when `COUNT(*) != 1` or the URL is blank / not `http(s)`. Two listings with
the same name in the same guild → **no listing URL at all**.

**F6. The Rank renderer's candidate list** (`_fetch_item_icon`), in order and deduplicated:
`app_emoji` → CDN URL; `listing_icon_url` (if http/s); `icon_url` (if http/s); a non-URL
field is parsed as a pasted `<:name:id>` token. If **nothing** resolves, the placeholder is
drawn with **no warning at all**. The warning fires only when `len(candidates) >= 1`.

**F7. `_fetch_image` logs failures but not status class.** It returns `None` for any
`resp.status != 200` **without logging**, and logs `failed to fetch <url>: <exception>` only
for real exceptions. A 403/404 is therefore **silent**.

**F8. `_ownership_supported()` requires the emoji's `user.id` to equal the bot's user ID.**
Note: Discord's application-emoji objects do not normally carry a `user` field. This matters
for the *write* path (`create_emoji` validation, `_adopt_asset`), **not** for the read path —
`attach_prepared_emojis` never calls it, so ownership metadata cannot explain a DB-level miss.

**F9. Both failure sites swallow exceptions.** `utils/rank_card_data.py` wraps the whole
resolution in `except Exception:`; `cogs/wallet.py::_attach_prepared_app_emojis` wraps it in
`except Exception: pass`. A *schema* or *connection* error is therefore indistinguishable from
"nothing was prepared" in production logs. This is a real observability defect.

**F10. There is no prep or preparation of any kind on the read path.** Inventory and Rank only
`SELECT`. The explicit workflow (`POST /shop-publisher/emojis/prepare` →
`prepare_product_emojis`) is the only writer, and it takes **product IDs only** — it reads
`shop_items.icon_url` server-side and never trusts a browser URL.

---

## 3. Decisive inference from the one piece of production evidence available

The warning count is a fingerprint. `tried 1 source(s)` means `len(candidates) == 1` (F6).

Given Inventory **also** shows the ⚪ fallback for these products (`_item_display` returns the
rarity glyph only when `app_emoji` is empty and `icon_url` is not an emoji token), and given
that the two paths share `attach_prepared_emojis`, there are exactly two ways to get a count of 1:

1. `app_emoji` empty **and** only one of `listing_icon_url` / `icon_url` is populated, or the
   two are the **same string** (deduplicated).
2. `app_emoji` non-empty but its **CDN URL string collides** with a stored URL — impossible in
   practice (`cdn.discordapp.com` vs the admin's host).

So: **`app_emoji` was empty, and exactly one raw URL existed for each of the four items.**

Two consequences follow directly, and they are the most useful result of this investigation:

- **The raw URL is not "missing".** At least one URL was present, otherwise the count would
  be 0 and the warning would not have fired. The failure is *not* "`icon_url` was never saved".
- **The four products were not mitigated by the PR #121 raw-URL fallback either.** If the
  single candidate URL were fetchable, the icon would have rendered from `listing_icon_url`
  or `icon_url` and there would be no warning. A warning means that one URL also failed to
  produce an image (non-200, non-image body, timeout) — **or** the URL is unfetchable from
  Railway (auth-walled / signed / hotlink-blocked / dead).

So for each of the four products, the chain broke at **two** consecutive points:
alias resolution (DB) **and** the raw URL fetch (network).

---

## 4. Four-product diagnostic table

Nothing below is measured; `[UNKNOWN]` is literal. These are the exact fields the
diagnostic script fills in.

| # | Product (exact name from logs) | product id | guild id | listing `icon_url` | catalog `icon_url` | canonical match? | alias for (app, guild)? | asset status | content hash | emoji id | emoji alive in app? | first failing condition |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `سهران السيرفر` | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] |
| 2 | `عنتيل السيرفر` | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] |
| 3 | `عصير المجرات` | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] |
| 4 | `شوكولاته الجلاكسي` | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] | [UNKNOWN] |

**Only two cells are constrained by evidence:** "alias for (app, guild)?" resolved nothing
(else `app_emoji` would be non-empty), and at least one raw URL existed (else the count would
be 0, not 1). Everything else needs the run in §7.

---

## 5. Hypotheses, and how each is ranked *before* data arrives

The diagnostic script distinguishes every row below mechanically.

| # | Hypothesis | Plausible? | What would prove it | Read evidence |
|---|---|---|---|---|
| H1 | **Preparation never completed** — no alias row for the canonical URL | **Highest** | alias query returns 0 rows for both URL sources; alias table contains no near-miss | Read path only accepts `status='ready'`; instruction 2 of the workflow is the only writer. Consistent with a 1-source count. |
| H2 | **Asset prepared but not `ready`** — `failed` / `missing` / `uncertain` / `preparing` / `quarantined` | **High** | alias exists, `status != 'ready'`, `last_error` populated | `_prepare_one` stores `failed` with `last_error` on any `SourceImageError`; the five-state CHECK makes this a normal, expected resting state. |
| H3 | **Application-ID mismatch** — alias rows scoped to a different `application_id` (e.g. prepared by `Nilive-Bot Copy`, or a different `DISCORD_CLIENT_ID` than the token's app) | **High** | an alias exists for the same URL under another `application_id` | PK is `(application_id, guild_id, source_url)`; `configured_application_id()` reads the **dashboard** env var while the bot read uses `interaction.application_id`. Two services exist. |
| H4 | **Guild-ID mismatch** — prepared under a different guild | Medium | alias exists for the same URL under another `guild_id` | Alias PK is guild-scoped; `prepare_product_emojis` uses the *session* guild from `get_session_guild_id()`. |
| H5 | **URL changed after preparation** — listing URL edited/re-uploaded, so the alias key no longer matches | **High** | listing URL ≠ catalog URL, and the alias matches only one of them | `_upsert_alias` keys on the canonical URL; a changed URL silently stops matching. `upsert_catalog_entry` uses `COALESCE` so the catalog can retain the *old* URL. |
| H6 | **Ambiguous listing** — >1 `shop_items` row with that name | Low-Medium | `COUNT(*) > 1` for the name | `find_listing_icon_urls` returns nothing then; does not affect the catalog pass. |
| H7 | **Emoji deleted in Discord** — row still says `ready` | Medium | `emoji_id` present but CDN fetch non-200 / missing from the app's emoji list | Read path never re-validates; a deleted emoji leaves `status='ready'`. Would still count as a *candidate*, so a 1-source count fits when listing/catalog URLs coincide. |
| H8 | **Source URL unfetchable from Railway** — dead link, auth-walled, hotlink-blocked, or signed URL expired | **High (co-primary)** | the raw candidate URL returns non-200 / non-image / times out from inside the container | This is exactly what the `tried 1 source(s)` warning implies (§3). `_fetch_image` logs non-200 silently (F7). |
| H9 | **Schema / DB-path mismatch** — emoji tables missing in the file the bot reads, or bot and dashboard on different DB files | Low | `.tables` shows missing `shop_emoji_*`, or two DB files exist | Both processes read `database.DB_PATH` (`/app/data/nero.db`); `init_db()` runs in both. Staging guard only triggers when `NERO_ENVIRONMENT=staging`. |
| H10 | **Swallowed exception masking a real error** | Medium (as a *confounder*) | re-running resolution with exceptions surfaced | F9. Cannot be the root cause on its own, but it can hide H9. |

**H1 alone cannot explain both symptoms.** A bare missing alias is fully consistent with the
Rank warning; but note that if the alias were missing *and* the raw URL were healthy, the icon
would still draw from the URL. The presence of the warning is evidence that the URL failed too.

---

## 6. Why PR #121 did not solve the problem

PR #121 fixed exactly one thing, correctly: **which application ID the read uses**
(`interaction.application_id`, with the client and `DISCORD_CLIENT_ID` as fallbacks). Its own
audit document says so and is explicit about the boundary it accepted:

> "this is a **proven failure mode in the audited code** … Production DB/env were not
> inspected, so this report does not claim to have observed the deployed Railway values.
> **If the deployed bot already has the correct `DISCORD_CLIENT_ID`, this specific defect
> would not by itself explain that deployment**."

Its verification used a scratch DB, a fake Discord application, and synthetic
`https://assets.example.com/item-N.png` URLs — i.e. it proved the *plumbing*, with every
condition that is failing in production (real alias rows, real source URL reachability, the
real application's emoji inventory) stubbed out.

Consequently:

- If the four products have **no ready alias at all** (H1/H2), a correct application ID changes nothing — an empty result is still empty. This is the single most likely explanation.
- If the source URLs are **unreachable from Railway** (H8), the raw-URL fallback that PR #121 also added cannot fire either.
- A correct application ID is a *necessary but not sufficient* condition. PR #121 supplied the necessity and was verified only for the necessity.

In short: **the PR addressed the identity dimension of the lookup; the observed failures are
consistent with the content dimension (does a ready alias for this exact URL exist?) and/or the
network dimension (is the source asset fetchable?).**

---

## 7. The smallest safe fix

**I am not proposing a code change until §8 returns data.** Ranking the candidate minimal
fixes by likelihood, so the decision is pre-made against the evidence:

| If the data shows | Smallest safe fix | Files / functions |
|---|---|---|
| H1/H2/H3/H4/H5 — no ready alias, or an alias in the wrong scope | **No code change.** Re-run the existing explicit workflow: `POST /shop-publisher/emojis/prepare` with the four product IDs (Shop Designer → prepare), then confirm each product's result is `status: ready` with a bound `emoji_id`. If a `ready` asset belongs to another guild's scope, only the owner-side recovery flow may move it. | `utils/shop_emoji.py::prepare_product_emojis` (unchanged), `dashboard/api/shop_emoji.py` (unchanged) |
| H7 — `emoji_id` no longer exists | `POST /shop-publisher/emojis/reconcile` for the four IDs; only then prepare. No schema change. | `utils/shop_emoji.py::reconcile_product_emojis` |
| H8 — source URL unfetchable | Fix the **asset**, not the code: re-upload to a stable public HTTPS host, save the new URL, re-prepare. If you want the failure visible instead of silent, the one-line honest change is to log the HTTP status/content-type in `_fetch_image` — logging only, no behaviour change. | `utils/rank_card_renderer.py::_fetch_image` (log only) |
| H9 — DB/schema mismatch | Repair by letting `init_db()` run on the correct file; **no** manual DDL. | `database.py::init_db` / `ensure_shop_emoji_schema` |
| H10 — swallowed exception is real | Replace the two bare `except Exception:` handlers with handlers that log the exception at WARNING. Diagnostic only. | `utils/rank_card_data.py` (grid resolution block), `cogs/wallet.py::_attach_prepared_app_emojis` |

The likely shape of the final change is therefore **small or empty**: a verification run, a
re-preparation of four products, and at most two logging lines. No upload automation, no
schema change, no read-path mutation.

---

## 8. Tool delivered: `tools/icon_diagnostic.py` (read-only)

Runs inside the main `Nilive-Bot` service and reproduces the failure through the **real
deployed code**. It answers §4 row by row.

**Safety properties (verified locally against a throwaway DB):**

- Every SQLite connection, including the one handed to the real
  `attach_prepared_emojis` / `find_listing_icon_urls`, uses `file:…?mode=ro`, `uri=True`.
- No DDL, no migration, no `INSERT`/`UPDATE`, no Discord API call, no network I/O.
- "Read-only" was proved empirically: the DB file was byte-identical (`cmp`) before and after a full run.
- URLs are printed as `scheme://host/path` only — **query strings are redacted**, since signed
  URLs carry credentials. Tokens are never read or printed.

**It does not re-implement the matching logic.** It imports
`utils.shop_emoji.canonicalize_url` and calls the production resolver, then reproduces
`_fetch_item_icon()`'s candidate list to tell you exactly which sources the
`tried N source(s)` counter saw.

### How to run (no writes, no restart)

```bash
# Railway → Nilive-Bot → Shell  (the shell has the production env + volume)
cd /app
OWNER_ID=0 python3 tools/icon_diagnostic.py --guild-id <YOUR_GUILD_ID> --user-id <MEMBER_ID>
```

- `--user-id` is the member whose Rank card fails; it enables the Rank/Inventory resolver trace.
- Omit `--guild-id` and the script auto-detects it from the four product names.
- `OWNER_ID=0` is required only because importing `database.py` exits when `OWNER_ID` is unset;
  it is not used by any query here.
- If the container shell cannot run `git pull`, copy the single file in:
  `railway run` / `docker cp`, or paste it via the Railway shell editor.

### The one thing it cannot check: is the emoji still alive in Discord?

Run this **separately**, right after, and compare the IDs. It prints the app's emoji
id→name map; the token is never echoed:

```bash
curl -sS -H "Authorization: Bot $DISCORD_TOKEN" \
  "https://discord.com/api/v10/applications/$DISCORD_CLIENT_ID/emojis" \
  | python3 -c 'import json,sys; print({e["id"]: e["name"] for e in json.load(sys.stdin).get("items", [])})'
```

And to prove the source URL itself is reachable **from inside Railway** (this is the H8 test,
and it must be run from the container, not from your laptop):

```bash
URL='<the exact icon_url the script printed>'
curl -sS -o /dev/null -D - --max-time 15 "$URL" | head -20
# want: HTTP/... 200  and  content-type: image/*
```

### Output you should send back

The full stdout of `icon_diagnostic.py` is enough to fill §4. Redact nothing except your
guild/member IDs if you prefer — URLs are already redacted by the script.

### Local self-test (proof it works, on fake data — not production)

I validated the script end-to-end against a **throwaway `/tmp` database** seeded with four
deliberately different failure modes. It correctly produced, per product:

- a fully `ready` alias → `app_emoji` resolved, `2 source(s)` tried (CDN + URL), no warning;
- an alias whose asset was `failed` → `app_emoji` empty, `1 source(s)`, **reproducing your exact warning**;
- listing URL ≠ catalog URL with the alias bound to the old one → *"listing URL and catalog URL canonicalize to DIFFERENT strings"*;
- an alias scoped to a different `application_id` → resolved under app A, **empty** under app B.

That last row is the H3 discriminator, and it demonstrates that the script tells H1 apart from
H3 rather than assuming one.

---

## 9. Confirmed vs hypothesis vs unknown

**Confirmed (from the deployed commit's source):** F1–F10 in §2, plus the two derived facts in
§3: `app_emoji` was empty for all four products, and exactly one raw URL candidate existed for
each (so at least one URL is saved, and that URL also failed to yield an image).

**Hypothesis (ranked, not asserted):** H1/H2/H3/H5 and H8 are the leading candidates; the
observed evidence is consistent with "no `ready` alias for the current URL" **and** "the single
raw URL is not fetchable from Railway". H9 and H10 are possible confounders.

**Unknown (requires the run in §8):** guild ID, product IDs, both stored URLs, canonical
equality, alias existence and scope, asset status/hash/emoji ID, whether the emoji still
exists in Discord, and the HTTP status/content-type of the source URL from inside Railway.

I have not claimed to inspect production data, and I have not invented any row or ID.

---

## 10. Boundaries honoured

- Application code: **unmodified** (`git status` shows only the new `tools/icon_diagnostic.py`).
- No branch change, no PR, no DB write or migration, no Railway variable change, no restart,
  no redeploy, no emoji upload/delete, no refactor.
- Awaiting your approval before implementing anything in §7.
