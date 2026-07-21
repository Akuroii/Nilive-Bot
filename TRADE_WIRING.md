# Trade dashboard page — remaining manual wiring (2 small edits)

`dashboard/app.py` and `dashboard/templates/base.html` are too large to
re-ship whole for one route + one nav link — same reasoning
`NAV_LINK_SNIPPET.html` and `MISSIONS_WIRING.md` already used. Apply
these two snippets by hand — 2 minutes total.

## 1. dashboard/app.py — add the page route

Paste anywhere near the `/ledger` or `/inventory` routes:

```python
@app.route("/trade")
@require_page("trade")
def trade_page():
    guild_id = get_session_guild_id()
    ctx = get_current_user_context()
    return render("systems/trade.html", **ctx)
```

Page data loads client-side via `/api/trade/history` (same pattern the
minigames/missions pages already use), so no server-side query is
needed in the route itself.

## 2. dashboard/templates/base.html — add the nav link

Inside the `<div class="nav-section">` block for "Systems", right
after the `/inventory` nav-link (last item in that section):

```html
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
```

That's both. Everything else (API route, dashboard page template,
permission tier) is already in this delta ZIP and wired.
