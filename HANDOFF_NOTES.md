# HANDOFF NOTES — this shift

Scope was strictly the 3 tasks requested. Nothing else touched.

1. **dashboard/api.py** — added `GET /api/activity/<user_id>`.
   - Gate: `@require_api_permission(LEVEL_ADMIN)`
   - Reads `activity_stats` WHERE guild_id=session guild AND user_id=<param>
   - guild_id always comes from `get_session_guild_id()`, never the client —
     matches every other route in the file.
   - Returns:
     ```json
     {
       "guild_id": ...,
       "user_id": ...,
       "messages_count": <lifetime sum>,
       "words_count": <lifetime sum>,
       "voice_minutes": <lifetime sum>,
       "forum_posts_count": <lifetime sum>,
       "daily": [ {date, messages_count, words_count, voice_minutes, forum_posts_count}, ... ]
     }
     ```
   - Added a `?days=N` query param (default 30, capped 365) for the daily
     breakdown array, since activity_stats is stored per-day and a raw sum
     alone throws away that granularity for anyone who wants a chart later.
     Not asked for explicitly but it's the same table, same query cost, and
     avoids a second endpoint later — flag if you'd rather it be totals-only.
   - Placed it in a new "Activity" section, right after the Members section,
     since it's per-member data in the same spirit as members_search.

2. **Deleted** `dashboard/templates/config/commands.html`. Grepped every
   route in `dashboard/app.py` first — nothing renders `config/commands.html`.
   `manage/commands.html` is the live one at `/commands`, left untouched.

3. **STATUS.md** updated — E1 marked verified (not rebuilt), activity
   endpoint marked complete, next-up note points at E2 re-verification.

## Not done / explicitly out of scope this shift
- No changes to cogs/activity_engine.py.
- No new database tables or migrations — activity_stats already existed.
- No dashboard template/UI added to *display* this endpoint's data — you
  only asked for the API route. Say the word if you want a page/tab wired
  to it next.
