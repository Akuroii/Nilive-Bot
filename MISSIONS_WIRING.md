# Missions — remaining manual wiring (3 small edits)

`dashboard/app.py` and `dashboard/templates/base.html` are too large to
re-ship whole for one route + one nav link (same reasoning
`NAV_LINK_SNIPPET.html` already used for minigames). Apply these three
snippets by hand — 5 minutes total.

## 1. dashboard/app.py — add the page route

Paste anywhere near the `/minigames` route (e.g. right after it):

```python
@app.route("/missions")
@require_page("missions")
def missions_page():
    guild_id = get_session_guild_id()
    ctx = get_current_user_context()
    return render("systems/missions.html", **ctx)
```

Page data loads client-side via `/api/missions/list` and
`/api/missions/completions` (same pattern the minigames page already
uses), so no server-side query is needed in the route itself.

## 2. dashboard/app.py — add to COMMAND_CATEGORIES

Inside the `COMMAND_CATEGORIES` dict, add a new entry (matches the
`"Minigames"` entry already there):

```python
    "Missions": [
        "missions", "mission_create", "mission_list", "mission_remove",
    ],
```

## 3. dashboard/templates/base.html — add the nav link

Inside the `<div class="nav-section">` block for "Systems", right
after the `/minigames` nav-link and before `/ledger`'s:

```html
    <a href="/missions"
       hx-get="/missions"
       hx-target="#content-area"
       hx-swap="outerHTML"
       hx-select="#content-area"
       hx-push-url="true"
       class="nav-link">
        <span class="nav-icon">🗺️</span>
        <span class="nav-text">Missions</span>
    </a>
```

That's all three. Everything else (schema, engine, cog, dashboard API,
dashboard page template, cog list, page permission) is already in this
ZIP and wired.
