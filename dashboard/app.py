import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import aiosqlite
import asyncio
from datetime import datetime, timedelta
from flask import Flask, render_template, redirect, url_for, session, request, jsonify, flash
from functools import wraps

from database import DB_PATH
from dashboard.auth import auth_bp, login_required, current_user_id
from dashboard.permissions import require_guild_access, get_session_guild_id, log_action, get_user_role
from dashboard.api import api_bp

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'nero-dashboard-secret-2024')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

app.register_blueprint(auth_bp)
app.register_blueprint(api_bp)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route('/')
def index():
    if session.get('user'):
        return redirect(url_for('server_select'))
    return redirect(url_for('auth.login'))


@app.route('/servers')
@login_required
def server_select():
    user_id = current_user_id()
    
    async def fetch_guilds():
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                SELECT DISTINCT guild_id FROM dashboard_users
                WHERE user_id = ? AND enabled = 1
            """, (user_id,))
            rows = await cursor.fetchall()
            return [r[0] for r in rows]
    
    guild_ids = run_async(fetch_guilds())
    
    if not guild_ids:
        return render_template('errors/403.html', 
                             message="You don't have access to any servers"), 403
    
    if len(guild_ids) == 1:
        return redirect(url_for('overview_page', guild_id=guild_ids[0]))
    
    return render_template('server_select.html', guild_ids=guild_ids)


@app.route('/dashboard/<int:guild_id>/overview')
@login_required
@require_guild_access
def overview_page(guild_id):
    user_role = get_user_role(guild_id)
    
    async def fetch_stats():
        async with aiosqlite.connect(DB_PATH) as db:
            member_count = await db.execute(
                "SELECT COUNT(*) FROM levels WHERE guild_id = ?", (guild_id,))
            member_count = (await member_count.fetchone())[0]
            
            total_xp = await db.execute(
                "SELECT SUM(xp) FROM levels WHERE guild_id = ?", (guild_id,))
            total_xp = (await total_xp.fetchone())[0] or 0
            
            total_coins = await db.execute(
                "SELECT SUM(balance) FROM economy WHERE guild_id = ?", (guild_id,))
            total_coins = (await total_coins.fetchone())[0] or 0
            
            return {
                'members': member_count,
                'total_xp': total_xp,
                'total_coins': total_coins,
            }
    
    stats = run_async(fetch_stats())
    
    if request.headers.get('HX-Request'):
        return render_template('general/overview.html',
                             guild_id=guild_id,
                             page_title='Overview',
                             stats=stats,
                             user_role=user_role)
    
    return render_template('base.html',
                         page='general/overview.html',
                         guild_id=guild_id,
                         page_title='Overview',
                         stats=stats,
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/members')
@login_required
@require_guild_access
def members_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('general/members.html',
                             guild_id=guild_id,
                             page_title='Members',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='general/members.html',
                         guild_id=guild_id,
                         page_title='Members',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/member/<int:user_id>')
@login_required
@require_guild_access
def member_profile(guild_id, user_id):
    user_role = get_user_role(guild_id)
    
    async def fetch_member_data():
        async with aiosqlite.connect(DB_PATH) as db:
            level_data = await db.execute("""
                SELECT xp, level FROM levels
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            level_data = await level_data.fetchone()
            
            economy_data = await db.execute("""
                SELECT balance FROM economy
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id))
            economy_data = await economy_data.fetchone()
            
            warnings = await db.execute("""
                SELECT COUNT(*) FROM moderation_logs
                WHERE guild_id = ? AND user_id = ? AND action = 'warn' AND deleted = 0
            """, (guild_id, user_id))
            warnings = (await warnings.fetchone())[0]
            
            mod_history = await db.execute("""
                SELECT action, reason, moderator_display_name, created_at
                FROM moderation_logs
                WHERE guild_id = ? AND user_id = ? AND deleted = 0
                ORDER BY created_at DESC LIMIT 10
            """, (guild_id, user_id))
            mod_history = await mod_history.fetchall()
            
            purchases = await db.execute("""
                SELECT item_name, price_paid, purchased_at
                FROM purchase_history
                WHERE guild_id = ? AND user_id = ?
                ORDER BY purchased_at DESC LIMIT 10
            """, (guild_id, user_id))
            purchases = await purchases.fetchall()
            
            return {
                'xp': level_data[0] if level_data else 0,
                'level': level_data[1] if level_data else 0,
                'coins': economy_data[0] if economy_data else 0,
                'warnings': warnings,
                'mod_history': mod_history,
                'purchases': purchases,
            }
    
    member_data = run_async(fetch_member_data())
    
    if request.headers.get('HX-Request'):
        return render_template('general/member_profile.html',
                             guild_id=guild_id,
                             target_user_id=user_id,
                             page_title='Member Profile',
                             member_data=member_data,
                             user_role=user_role)
    
    return render_template('base.html',
                         page='general/member_profile.html',
                         guild_id=guild_id,
                         target_user_id=user_id,
                         page_title='Member Profile',
                         member_data=member_data,
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/auditlog')
@login_required
@require_guild_access
def auditlog_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('general/auditlog.html',
                             guild_id=guild_id,
                             page_title='Audit Log',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='general/auditlog.html',
                         guild_id=guild_id,
                         page_title='Audit Log',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/moderation')
@login_required
@require_guild_access
def moderation_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('manage/moderation.html',
                             guild_id=guild_id,
                             page_title='Moderation',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='manage/moderation.html',
                         guild_id=guild_id,
                         page_title='Moderation',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/tickets')
@login_required
@require_guild_access
def tickets_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('manage/tickets.html',
                             guild_id=guild_id,
                             page_title='Tickets',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='manage/tickets.html',
                         guild_id=guild_id,
                         page_title='Tickets',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/embedbuilder')
@login_required
@require_guild_access
def embedbuilder_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('manage/embedbuilder.html',
                             guild_id=guild_id,
                             page_title='Embed Builder',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='manage/embedbuilder.html',
                         guild_id=guild_id,
                         page_title='Embed Builder',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/reactionroles')
@login_required
@require_guild_access
def reactionroles_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('manage/reactionroles.html',
                             guild_id=guild_id,
                             page_title='Reaction Roles',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='manage/reactionroles.html',
                         guild_id=guild_id,
                         page_title='Reaction Roles',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/triggers')
@login_required
@require_guild_access
def triggers_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('manage/triggers.html',
                             guild_id=guild_id,
                             page_title='Triggers',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='manage/triggers.html',
                         guild_id=guild_id,
                         page_title='Triggers',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/mvp')
@login_required
@require_guild_access
def mvp_page(guild_id):
    user_role = get_user_role(guild_id)
    
    async def fetch_mvp_data():
        async with aiosqlite.connect(DB_PATH) as db:
            config_cursor = await db.execute(
                "SELECT * FROM mvp_config WHERE guild_id = ?", (guild_id,))
            config_row = await config_cursor.fetchone()
            
            if config_row:
                cols = [d[0] for d in config_cursor.description]
                config = dict(zip(cols, config_row))
            else:
                config = {}
            
            from datetime import date
            today = date.today().isoformat()
            
            scores_cursor = await db.execute("""
                SELECT user_id, message_score, voice_minutes, total_score
                FROM mvp_scores
                WHERE guild_id = ? AND date = ?
                ORDER BY total_score DESC LIMIT 20
            """, (guild_id, today))
            today_scores = await scores_cursor.fetchall()
            
            history_cursor = await db.execute("""
                SELECT user_display_name, cycle_start, cycle_end, score
                FROM mvp_history
                WHERE guild_id = ?
                ORDER BY cycle_end DESC LIMIT 10
            """, (guild_id,))
            history = await history_cursor.fetchall()
            
            return config, today_scores, history
    
    config, today_scores, history = run_async(fetch_mvp_data())
    
    if request.headers.get('HX-Request'):
        return render_template('systems/mvp.html',
                             guild_id=guild_id,
                             page_title='MVP System',
                             config=config,
                             today_scores=today_scores,
                             history=history,
                             user_role=user_role)
    
    return render_template('base.html',
                         page='systems/mvp.html',
                         guild_id=guild_id,
                         page_title='MVP System',
                         config=config,
                         today_scores=today_scores,
                         history=history,
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/leveling')
@login_required
@require_guild_access
def leveling_page(guild_id):
    user_role = get_user_role(guild_id)
    
    async def fetch_leveling_data():
        async with aiosqlite.connect(DB_PATH) as db:
            config_cursor = await db.execute(
                "SELECT * FROM leveling_config WHERE guild_id = ?", (guild_id,))
            config_row = await config_cursor.fetchone()
            
            if config_row:
                cols = [d[0] for d in config_cursor.description]
                config = dict(zip(cols, config_row))
            else:
                config = {}
            
            rewards_cursor = await db.execute("""
                SELECT id, level, role_id FROM leveling_rewards
                WHERE guild_id = ? ORDER BY level ASC
            """, (guild_id,))
            rewards = await rewards_cursor.fetchall()
            
            bonus_cursor = await db.execute("""
                SELECT id, role_id, multiplier FROM leveling_bonus_roles
                WHERE guild_id = ? ORDER BY multiplier DESC
            """, (guild_id,))
            bonus_roles = await bonus_cursor.fetchall()
            
            blacklist_cursor = await db.execute("""
                SELECT id, role_id FROM leveling_blacklist_roles
                WHERE guild_id = ?
            """, (guild_id,))
            blacklist = await blacklist_cursor.fetchall()
            
            return config, rewards, bonus_roles, blacklist
    
    config, rewards, bonus_roles, blacklist = run_async(fetch_leveling_data())
    
    if request.headers.get('HX-Request'):
        return render_template('systems/leveling.html',
                             guild_id=guild_id,
                             page_title='Leveling System',
                             config=config,
                             rewards=rewards,
                             bonus_roles=bonus_roles,
                             blacklist=blacklist,
                             user_role=user_role)
    
    return render_template('base.html',
                         page='systems/leveling.html',
                         guild_id=guild_id,
                         page_title='Leveling System',
                         config=config,
                         rewards=rewards,
                         bonus_roles=bonus_roles,
                         blacklist=blacklist,
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/economy')
@login_required
@require_guild_access
def economy_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('systems/economy.html',
                             guild_id=guild_id,
                             page_title='Economy',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='systems/economy.html',
                         guild_id=guild_id,
                         page_title='Economy',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/shop')
@login_required
@require_guild_access
def shop_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('systems/shop.html',
                             guild_id=guild_id,
                             page_title='Shop',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='systems/shop.html',
                         guild_id=guild_id,
                         page_title='Shop',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/events')
@login_required
@require_guild_access
def events_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('systems/events.html',
                             guild_id=guild_id,
                             page_title='Events',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='systems/events.html',
                         guild_id=guild_id,
                         page_title='Events',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/general')
@login_required
@require_guild_access
def general_settings_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('config/general.html',
                             guild_id=guild_id,
                             page_title='General Settings',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/general.html',
                         guild_id=guild_id,
                         page_title='General Settings',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/welcome')
@login_required
@require_guild_access
def welcome_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('config/welcome.html',
                             guild_id=guild_id,
                             page_title='Welcome & Leave',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/welcome.html',
                         guild_id=guild_id,
                         page_title='Welcome & Leave',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/boost')
@login_required
@require_guild_access
def boost_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('config/boost.html',
                             guild_id=guild_id,
                             page_title='Boost Roles',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/boost.html',
                         guild_id=guild_id,
                         page_title='Boost Roles',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/announcements')
@login_required
@require_guild_access
def announcements_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('config/announcements.html',
                             guild_id=guild_id,
                             page_title='Announcements',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/announcements.html',
                         guild_id=guild_id,
                         page_title='Announcements',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/commands')
@login_required
@require_guild_access
def commands_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if request.headers.get('HX-Request'):
        return render_template('config/commands.html',
                             guild_id=guild_id,
                             page_title='Commands',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/commands.html',
                         guild_id=guild_id,
                         page_title='Commands',
                         user_role=user_role)


@app.route('/dashboard/<int:guild_id>/access')
@login_required
@require_guild_access
def access_page(guild_id):
    user_role = get_user_role(guild_id)
    
    if user_role != 'Owner':
        return render_template('errors/403.html',
                             message="Only server owners can manage dashboard access"), 403
    
    if request.headers.get('HX-Request'):
        return render_template('config/access.html',
                             guild_id=guild_id,
                             page_title='Dashboard Access',
                             user_role=user_role)
    
    return render_template('base.html',
                         page='config/access.html',
                         guild_id=guild_id,
                         page_title='Dashboard Access',
                         user_role=user_role)


@app.errorhandler(403)
def forbidden(e):
    return render_template('errors/403.html'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('errors/404.html'), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template('errors/500.html'), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
