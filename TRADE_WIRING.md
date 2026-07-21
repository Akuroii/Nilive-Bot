# Trade dashboard page — remaining manual wiring (2 small edits)

`dashboard/app.py` and `dashboard/templates/base.html` are too large to
re-ship whole for one route + one nav link — same reasoning
`NAV_LINK_SNIPPET.html` and `MISSIONS_WIRING.md` already used. Exact
insertion points below — search for the anchor text verbatim.

## 1. dashboard/app.py — add the page route

**Find this exact block** (the end of the `/inventory/<int:user_id>`
route, immediately followed by the `# ── Config: General` section
comment):

```python
@app.route("/inventory/<int:user_id>")
@require_page("inventory_view")
def inventory_user_page(user_id: int):
    guild_id = get_session_guild_id()

    async def get_user_items():
        from utils.inventory import get_inventory
        return await get_inventory(guild_id, user_id, include_empty=False)

    items = run_async(get_user_items())
    ctx   = get_current_user_context()
    return render("systems/inventory.html",
                  items=items, target_user_id=user_id, **ctx)


# ── Config: General ────────────────────────────────────────────────────────────
```

**Insert the new route between those two pieces** — right after the
`inventory_user_page` function's `return render(...)` line, and right
before the `# ── Config: General` comment line. Result should read:

```python
@app.route("/inventory/<int:user_id>")
@require_page("inventory_view")
def inventory_user_page(user_id: int):
    guild_id = get_session_guild_id()

    async def get_user_items():
        from utils.inventory import get_inventory
        return await get_inventory(guild_id, user_id, include_empty=False)

    items = run_async(get_user_items())
    ctx   = get_current_user_context()
    return render("systems/inventory.html",
                  items=items, target_user_id=user_id, **ctx)


# ── Trade (read-only history) ───────────────────────────────────────────────

@app.route("/trade")
@require_page("trade")
def trade_page():
    guild_id = get_session_guild_id()
    ctx = get_current_user_context()
    return render("systems/trade.html", **ctx)


# ── Config: General ────────────────────────────────────────────────────────────
```

Page data loads client-side via `/api/trade/history` (same pattern the
minigames/missions pages already use), so nothing else in this route
is needed.

## 2. dashboard/templates/base.html — add the nav link

**Find this exact block** (the end of the "Systems" `nav-section`,
where the Inventory link is the last item before the section's closing
`</div>` and the "Config" section starts):

```html
    <a href="/inventory"
       hx-get="/inventory"
       hx-target="#content-area"
       hx-swap="outerHTML"
       hx-select="#content-area"
       hx-push-url="true"
       class="nav-link">
        <span class="nav-icon">🎒</span>
        <span class="nav-text">Inventory</span>
    </a>
</div>
<div class="nav-section">
    <div class="nav-section-title">Config</div>
```

**Insert the new `<a>` block between the Inventory link's closing
`</a>` and the Systems section's closing `</div>`** (i.e. Trade History
becomes the new last item in "Systems", still above "Config"). Result
should read:

```html
    <a href="/inventory"
       hx-get="/inventory"
       hx-target="#content-area"
       hx-swap="outerHTML"
       hx-select="#content-area"
       hx-push-url="true"
       class="nav-link">
        <span class="nav-icon">🎒</span>
        <span class="nav-text">Inventory</span>
    </a>
    <a href="/trade"
       hx-get="/trade"
       hx-target="#content-area"
       hx-swap="outerHTML"
       hx-select="#content-area"
       hx-push-url="true"
       class="nav-link">
        <span class="nav-icon">🤝</span>
        <span class="nav-text">Trade History</span>
    </a>
</div>
<div class="nav-section">
    <div class="nav-section-title">Config</div>
```

That's both edits. API route, page template, and permission tier are
already wired via the prior `nero_trade_dashboard_delta.zip`.
