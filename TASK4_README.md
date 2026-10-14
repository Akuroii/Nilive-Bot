# TASK 4 — Ticket Dashboard Config (delta ZIP)

Drop these files into your repo at the SAME paths — each is a full,
ready-to-use file (not a patch/diff), so just overwrite what's there.

## Files in this ZIP

- `database.py`
  - Added one migration: `ticket_categories.required_role_id`
    (nullable INTEGER). NULL = category open to everyone (unchanged
    behavior). Set = only members holding that role can open it.
  - Everything else in the file is untouched from your existing ZIP.

- `cogs/tickets.py`
  - `TicketOpenButton.open_ticket()` now filters the category select
    menu down to categories the clicking member is actually allowed
    to open (based on `required_role_id`).
  - `create_ticket()` re-checks the same permission server-side
    (defense in depth), and now reads/applies the full embed —
    footer, thumbnail, image, plus `{user}`/`{name}`/`{server}`
    placeholders in title/description/footer. Previously only
    title/description/color were read; the rest silently did nothing.
  - Everything else (claim/close/delete/reopen permission checks,
    transcript saving, etc.) is unchanged.

- `dashboard/api/tickets.py`
  - `/api/tickets/categories` GET now returns `required_role_id`.
  - `/api/tickets/categories` POST (create/update) now accepts and
    saves `required_role_id`.
  - Every other route in this file (settings, panels, claim,
    transfer, tag, ratings) is unchanged.

- `dashboard/templates/manage/tickets.html`
  - New tabbed layout: **Tickets** (existing list, untouched) /
    **General** (new — ticket_settings CRUD: enabled, max per user,
    auto-close hours, name format, default staff role, transcript
    channel, save-transcripts toggle) / **Permissions & Embeds**
    (new — per-category CRUD: who can open it, viewer roles, admin
    "closer" roles, auto-assign roles, and a full embed builder for
    the message posted when a ticket opens: title, color, description
    with placeholders, footer, thumbnail, image).
  - This replaces the old single-tab ticket list page.

## What to double check after deploying

1. Restart the bot process at least once so `database.py`'s
   `init_db()` runs the new migration
   (`ticket_categories.required_role_id`).
2. Open the dashboard **Tickets** page → **Permissions & Embeds** tab
   → add or edit a category → confirm "Who can open this category"
   picker loads roles (uses the existing `nero-role-picker` Select2
   widget already wired elsewhere in the dashboard).
3. In Discord, click **Open Ticket** on your ticket panel:
   - If you gated a category behind a role and you DON'T have that
     role, it should not appear in the select menu.
   - Open a ticket in a category with a custom embed configured and
     confirm title/description/footer/thumbnail/image all show up
     correctly, with `{user}`, `{name}`, `{server}` replaced.
4. Confirm the **General** tab's Save button round-trips correctly
   (reload the page after saving, values should persist).

## Compile check

All three Python files were run through `python3 -m py_compile` with
zero errors before this ZIP was built.

## Not touched / no regressions expected

- `ticket_settings`, `ticket_panels`, `ticket_ratings` schemas/logic:
  unchanged.
- Claim/Close/Delete/Reopen permission logic in `cogs/tickets.py`:
  unchanged (already had hierarchy fixes from earlier passes).
- Every other file in your project (leveling, economy, moderation,
  etc.): not included in this ZIP because not touched by Task 4.

## Ideas flagged for later (not built, no action needed now)

1. `ticket_panels` table has full dashboard CRUD but nothing in
   `cogs/tickets.py` reads it to actually post a live Discord panel —
   `/ticket_setup` posts its own hardcoded panel instead. Either wire
   it in for real or remove the unused API surface.
2. Per-category transcript log channel override (currently one global
   channel in `ticket_settings`).
3. `required_role_id` is single-role only — multi-role "any of these"
   gating would need a JSON list instead.
