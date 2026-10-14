from markupsafe import escape

# Python-side twin of dashboard/templates/macros/user_identity.html —
# for the dashboard/api/*.py routes that build partial HTML by hand
# (e.g. economy_shop.py's economy_leaderboard_partial, tickets.py's
# tickets_partial) rather than through Jinja. Kept visually and
# structurally identical to the macro's markup so both render paths
# produce the same DOM/CSS hooks.
#
# All inputs are escaped — display_name/username can be an
# attacker-controlled Discord nickname, and this HTML is inserted via
# innerHTML on both the htmx-swap and raw-fetch render paths.


def render_user_identity_html(user_id, display_name: str = None,
                               username: str = None,
                               avatar_url: str = None,
                               compact: bool = False) -> str:
    name = display_name or username or f"User {user_id}"
    name_esc = escape(name)
    uid_esc = escape(str(user_id))
    compact_class = " user-identity-compact" if compact else ""

    avatar_html = ""
    if not compact:
        initial = escape(name[0].upper()) if name else "?"
        if avatar_url:
            avatar_esc = escape(avatar_url)
            avatar_html = (
                f"<img src='{avatar_esc}' class='user-identity-avatar' "
                f"onerror=\"this.style.display='none';"
                f"this.nextElementSibling.style.display='flex';\">"
                f"<div class='user-identity-avatar-fallback' "
                f"style='display:none;'>{initial}</div>"
            )
        else:
            avatar_html = (
                f"<div class='user-identity-avatar-fallback'>{initial}</div>"
            )

    return (
        f"<div class='user-identity{compact_class}'>"
        f"{avatar_html}"
        f"<div class='user-identity-text'>"
        f"<div class='user-identity-name'>{name_esc}</div>"
        f"<div class='user-identity-id'>{uid_esc}</div>"
        f"</div></div>"
    )
