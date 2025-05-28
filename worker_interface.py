# --- START OF FILE worker_interface.py ---

import logging
import sqlite3
import math
from datetime import datetime, timezone, timedelta

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import telegram.error as telegram_error

# --- Local Imports ---
from utils import (
    ADMIN_ID, SECONDARY_ADMIN_IDS, LANGUAGES, CITIES, DISTRICTS, PRODUCT_TYPES,
    get_db_connection, send_message_with_retry, _get_lang_data,
    log_admin_action, get_user_roles, DEFAULT_PRODUCT_EMOJI
)

logger = logging.getLogger(__name__)

# --- Worker Main Menu ---
async def handle_worker_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main admin menu for workers - limited functionality"""
    query = update.callback_query if hasattr(update, 'callback_query') and update.callback_query else None
    user_id = update.effective_user.id
    
    # Check if user is a worker
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        msg = "❌ Access denied. Worker permissions required."
        if query:
            await query.edit_message_text(msg, parse_mode=None)
        else:
            await send_message_with_retry(context.bot, update.effective_chat.id, msg)
        return

    # Get worker info
    worker_info = await _get_worker_info(user_id)
    if not worker_info:
        msg = "❌ Worker profile not found."
        if query:
            await query.edit_message_text(msg, parse_mode=None)
        else:
            await send_message_with_retry(context.bot, update.effective_chat.id, msg)
        return

    if worker_info['worker_status'] != 'active':
        msg = f"❌ Worker account is {worker_info['worker_status']}. Contact admin."
        if query:
            await query.edit_message_text(msg, parse_mode=None)
        else:
            await send_message_with_retry(context.bot, update.effective_chat.id, msg)
        return

    # Get today's statistics
    today_stats = await _get_worker_today_stats(user_id)
    username = update.effective_user.username or f"ID_{user_id}"
    alias = f" ({worker_info['worker_alias']})" if worker_info['worker_alias'] else ""
    
    msg = f"👷 Worker Panel: @{username}{alias}\n\n"
    msg += f"📊 Today's Progress:\n"
    msg += f"• Drops Added: {today_stats['drops_today']}\n"
    msg += f"• Daily Quota: {worker_info['daily_quota']}\n"
    
    quota_progress = (today_stats['drops_today'] / worker_info['daily_quota']) * 100 if worker_info['daily_quota'] > 0 else 0
    progress_bar = _generate_progress_bar(quota_progress)
    msg += f"• Progress: {progress_bar} {quota_progress:.1f}%\n\n"
    
    msg += f"📈 All-Time Stats:\n"
    msg += f"• Total Drops: {today_stats['total_drops']}\n"
    msg += f"• Last Drop: {today_stats['last_drop']}\n"
    msg += f"• Worker Since: {today_stats['worker_since']}\n\n"
    
    if today_stats['drops_today'] >= worker_info['daily_quota']:
        msg += "🎉 Daily quota completed! Great work!\n\n"
    else:
        remaining = worker_info['daily_quota'] - today_stats['drops_today']
        msg += f"🎯 {remaining} more drops to reach your quota!\n\n"
    
    msg += "Select an action:"

    keyboard = [
        [InlineKeyboardButton("➕ Add Products", callback_data="worker_add_products")],
        [InlineKeyboardButton("📊 My Statistics", callback_data="worker_view_stats")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="worker_leaderboard")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_start")]
    ]

    if query:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer()
    else:
        await send_message_with_retry(context.bot, update.effective_chat.id, msg, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Add Products Flow for Workers ---
async def handle_worker_add_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker product addition - similar to admin but with worker tracking"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    msg = "➕ Add Products\n\nSelect a city to add products:"
    keyboard = []
    
    for city_id, city_name in CITIES.items():
        keyboard.append([InlineKeyboardButton(f"📍 {city_name}", callback_data=f"worker_city|{city_id}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Worker Panel", callback_data="worker_admin_menu")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_worker_city_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle city selection for workers"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not params or not params[0]:
        return await query.answer("Invalid city selection.", show_alert=True)
    
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.answer("City not found.", show_alert=True)
    
    # Store selected city in context
    context.user_data['worker_selected_city'] = city_id
    context.user_data['worker_selected_city_name'] = city_name
    
    msg = f"📍 Selected: {city_name}\n\nSelect a district:"
    keyboard = []
    
    districts = DISTRICTS.get(city_id, {})
    for dist_id, dist_name in districts.items():
        keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=f"worker_district|{dist_id}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data="worker_add_products")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_worker_district_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle district selection for workers"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not params or not params[0]:
        return await query.answer("Invalid district selection.", show_alert=True)
    
    district_id = params[0]
    city_id = context.user_data.get('worker_selected_city')
    if not city_id:
        return await query.answer("City selection lost. Please start again.", show_alert=True)
    
    district_name = DISTRICTS.get(city_id, {}).get(district_id)
    if not district_name:
        return await query.answer("District not found.", show_alert=True)
    
    # Store selected district
    context.user_data['worker_selected_district'] = district_id
    context.user_data['worker_selected_district_name'] = district_name
    
    city_name = context.user_data.get('worker_selected_city_name')
    msg = f"📍 Location: {city_name} / {district_name}\n\nSelect product type:"
    keyboard = []
    
    for p_type, emoji in PRODUCT_TYPES.items():
        keyboard.append([InlineKeyboardButton(f"{emoji} {p_type}", callback_data=f"worker_type|{p_type}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"worker_city|{city_id}")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_worker_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle product type selection for workers"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not params or not params[0]:
        return await query.answer("Invalid type selection.", show_alert=True)
    
    product_type = params[0]
    if product_type not in PRODUCT_TYPES:
        return await query.answer("Product type not found.", show_alert=True)
    
    # Store selected type and prompt for details
    context.user_data['worker_selected_type'] = product_type
    context.user_data['worker_state'] = 'awaiting_product_details'
    
    city_name = context.user_data.get('worker_selected_city_name')
    district_name = context.user_data.get('worker_selected_district_name')
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    msg = f"📍 Location: {city_name} / {district_name}\n"
    msg += f"📦 Type: {emoji} {product_type}\n\n"
    msg += "💬 Please send your product details message (text + media).\n\n"
    msg += "Format example:\n"
    msg += "• Size: 1g\n"
    msg += "• Price: 25.00\n"
    msg += "• Any additional info...\n\n"
    msg += "📸 You can also include photos/videos with your message."
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send product details now.")

# --- Worker Statistics ---
async def handle_worker_view_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show detailed worker statistics"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied.", show_alert=True)
    
    # Get comprehensive stats
    stats = await _get_worker_comprehensive_stats(user_id)
    username = update.effective_user.username or f"ID_{user_id}"
    
    msg = f"📊 Statistics for @{username}\n\n"
    
    # Today's stats
    msg += f"📅 **Today ({datetime.now().strftime('%Y-%m-%d')})**\n"
    msg += f"• Drops Added: {stats['today']['drops']}\n"
    msg += f"• Quota Progress: {stats['today']['quota_progress']:.1f}%\n"
    msg += f"• Average per Hour: {stats['today']['avg_per_hour']:.1f}\n\n"
    
    # This week's stats
    msg += f"📅 **This Week**\n"
    msg += f"• Total Drops: {stats['week']['drops']}\n"
    msg += f"• Daily Average: {stats['week']['daily_avg']:.1f}\n"
    msg += f"• Best Day: {stats['week']['best_day']}\n\n"
    
    # This month's stats
    msg += f"📅 **This Month**\n"
    msg += f"• Total Drops: {stats['month']['drops']}\n"
    msg += f"• Daily Average: {stats['month']['daily_avg']:.1f}\n"
    msg += f"• Quota Achievement: {stats['month']['quota_achievement']:.1f}%\n\n"
    
    # All-time stats
    msg += f"📅 **All-Time**\n"
    msg += f"• Total Drops: {stats['alltime']['drops']}\n"
    msg += f"• Days Active: {stats['alltime']['days_active']}\n"
    msg += f"• Average per Day: {stats['alltime']['daily_avg']:.1f}\n"
    msg += f"• Most Productive Product: {stats['alltime']['top_product']}\n"
    
    # Ranking
    if stats['ranking']['position'] > 0:
        msg += f"\n🏆 **Ranking**\n"
        msg += f"• Current Rank: #{stats['ranking']['position']} of {stats['ranking']['total_workers']}\n"
        msg += f"• Top Performer This Month: {stats['ranking']['top_performer']}\n"
    
    keyboard = [
        [InlineKeyboardButton("📈 Weekly Report", callback_data="worker_weekly_report")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="worker_leaderboard")],
        [InlineKeyboardButton("⬅️ Back to Panel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Worker Leaderboard ---
async def handle_worker_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show worker leaderboard"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Get leaderboard data
    leaderboard = await _get_worker_leaderboard()
    current_user_stats = next((worker for worker in leaderboard if worker['user_id'] == user_id), None)
    
    msg = "🏆 Worker Leaderboard (This Month)\n\n"
    
    for i, worker in enumerate(leaderboard[:10], 1):
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        username = worker['username'] or f"ID_{worker['user_id']}"
        alias = f" ({worker['alias']})" if worker['alias'] else ""
        
        # Highlight current user
        highlight = "**" if worker['user_id'] == user_id else ""
        
        msg += f"{emoji} {highlight}@{username}{alias}{highlight}\n"
        msg += f"   • Drops: {worker['drops_this_month']}\n"
        msg += f"   • Avg/Day: {worker['daily_avg']:.1f}\n"
        msg += f"   • Quota Rate: {worker['quota_achievement']:.1f}%\n\n"
    
    # Show current user's position if not in top 10
    if current_user_stats and leaderboard.index(current_user_stats) >= 10:
        position = leaderboard.index(current_user_stats) + 1
        msg += f"...\n"
        msg += f"#{position}. **You**: {current_user_stats['drops_this_month']} drops\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 My Stats", callback_data="worker_view_stats")],
        [InlineKeyboardButton("⬅️ Back to Panel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Helper Functions ---
async def _get_worker_info(user_id: int) -> dict:
    """Get worker information from database"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT worker_status, worker_alias, worker_daily_quota
            FROM users
            WHERE user_id = ? AND is_worker = 1
        """, (user_id,))
        result = c.fetchone()
        
        if result:
            return {
                'worker_status': result['worker_status'],
                'worker_alias': result['worker_alias'],
                'daily_quota': result['worker_daily_quota'] or 10
            }
        return None
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker info for {user_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

async def _get_worker_today_stats(user_id: int) -> dict:
    """Get worker's today statistics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # Today's drops
        c.execute("""
            SELECT COUNT(*) as drops_today
            FROM products
            WHERE added_by = ? AND DATE(added_date) = ?
        """, (user_id, today))
        today_result = c.fetchone()
        drops_today = today_result['drops_today'] if today_result else 0
        
        # Total drops
        c.execute("""
            SELECT COUNT(*) as total_drops, MAX(added_date) as last_drop
            FROM products
            WHERE added_by = ?
        """, (user_id,))
        total_result = c.fetchone()
        total_drops = total_result['total_drops'] if total_result else 0
        last_drop = total_result['last_drop'] if total_result else "Never"
        
        # Worker since (when they were made a worker)
        c.execute("""
            SELECT MIN(added_date) as first_drop
            FROM products
            WHERE added_by = ?
        """, (user_id,))
        since_result = c.fetchone()
        worker_since = since_result['first_drop'] if since_result and since_result['first_drop'] else "N/A"
        
        if last_drop != "Never":
            try:
                last_drop = datetime.fromisoformat(last_drop).strftime("%Y-%m-%d %H:%M")
            except:
                pass
        
        if worker_since != "N/A":
            try:
                worker_since = datetime.fromisoformat(worker_since).strftime("%Y-%m-%d")
            except:
                pass
        
        return {
            'drops_today': drops_today,
            'total_drops': total_drops,
            'last_drop': last_drop,
            'worker_since': worker_since
        }
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker today stats for {user_id}: {e}")
        return {'drops_today': 0, 'total_drops': 0, 'last_drop': 'Never', 'worker_since': 'N/A'}
    finally:
        if conn:
            conn.close()

async def _get_worker_comprehensive_stats(user_id: int) -> dict:
    """Get comprehensive worker statistics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
        month_start = now.replace(day=1).strftime('%Y-%m-%d')
        
        stats = {
            'today': {},
            'week': {},
            'month': {},
            'alltime': {},
            'ranking': {}
        }
        
        # Today's stats
        c.execute("""
            SELECT COUNT(*) as drops, MIN(added_date) as first_drop
            FROM products
            WHERE added_by = ? AND DATE(added_date) = ?
        """, (user_id, today))
        today_result = c.fetchone()
        
        # Get quota
        c.execute("SELECT worker_daily_quota FROM users WHERE user_id = ?", (user_id,))
        quota_result = c.fetchone()
        daily_quota = quota_result['worker_daily_quota'] if quota_result else 10
        
        drops_today = today_result['drops'] if today_result else 0
        quota_progress = (drops_today / daily_quota * 100) if daily_quota > 0 else 0
        
        # Calculate average per hour for today
        first_drop_today = today_result['first_drop'] if today_result else None
        avg_per_hour = 0
        if first_drop_today and drops_today > 0:
            try:
                first_time = datetime.fromisoformat(first_drop_today)
                hours_working = max(1, (now - first_time).total_seconds() / 3600)
                avg_per_hour = drops_today / hours_working
            except:
                pass
        
        stats['today'] = {
            'drops': drops_today,
            'quota_progress': quota_progress,
            'avg_per_hour': avg_per_hour
        }
        
        # Week's stats
        c.execute("""
            SELECT COUNT(*) as drops, DATE(added_date) as date
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
            GROUP BY DATE(added_date)
            ORDER BY drops DESC
        """, (user_id, week_start))
        week_results = c.fetchall()
        
        week_drops = sum(day['drops'] for day in week_results)
        week_days = len(week_results)
        week_daily_avg = week_drops / max(1, week_days)
        best_day = f"{week_results[0]['drops']} drops" if week_results else "0 drops"
        
        stats['week'] = {
            'drops': week_drops,
            'daily_avg': week_daily_avg,
            'best_day': best_day
        }
        
        # Month's stats
        c.execute("""
            SELECT COUNT(*) as drops
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
        """, (user_id, month_start))
        month_result = c.fetchone()
        month_drops = month_result['drops'] if month_result else 0
        
        days_in_month = now.day
        month_daily_avg = month_drops / max(1, days_in_month)
        quota_achievement = (month_daily_avg / daily_quota * 100) if daily_quota > 0 else 0
        
        stats['month'] = {
            'drops': month_drops,
            'daily_avg': month_daily_avg,
            'quota_achievement': quota_achievement
        }
        
        # All-time stats
        c.execute("""
            SELECT COUNT(*) as drops, 
                   COUNT(DISTINCT DATE(added_date)) as days_active,
                   product_type
            FROM products
            WHERE added_by = ?
            GROUP BY product_type
            ORDER BY COUNT(*) DESC
        """, (user_id,))
        alltime_results = c.fetchall()
        
        total_drops = sum(result['drops'] for result in alltime_results)
        days_active = alltime_results[0]['days_active'] if alltime_results else 0
        alltime_daily_avg = total_drops / max(1, days_active)
        top_product = alltime_results[0]['product_type'] if alltime_results else "None"
        
        stats['alltime'] = {
            'drops': total_drops,
            'days_active': days_active,
            'daily_avg': alltime_daily_avg,
            'top_product': top_product
        }
        
        # Ranking
        c.execute("""
            SELECT user_id, COUNT(*) as month_drops
            FROM products p
            JOIN users u ON p.added_by = u.user_id
            WHERE u.is_worker = 1 AND DATE(p.added_date) >= ?
            GROUP BY user_id
            ORDER BY month_drops DESC
        """, (month_start,))
        ranking_results = c.fetchall()
        
        position = 0
        total_workers = len(ranking_results)
        top_performer = "None"
        
        for i, result in enumerate(ranking_results, 1):
            if result['user_id'] == user_id:
                position = i
            if i == 1:
                c.execute("SELECT username FROM users WHERE user_id = ?", (result['user_id'],))
                top_user = c.fetchone()
                top_performer = top_user['username'] if top_user and top_user['username'] else f"ID_{result['user_id']}"
        
        stats['ranking'] = {
            'position': position,
            'total_workers': total_workers,
            'top_performer': top_performer
        }
        
        return stats
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching comprehensive stats for {user_id}: {e}")
        return {
            'today': {'drops': 0, 'quota_progress': 0, 'avg_per_hour': 0},
            'week': {'drops': 0, 'daily_avg': 0, 'best_day': '0 drops'},
            'month': {'drops': 0, 'daily_avg': 0, 'quota_achievement': 0},
            'alltime': {'drops': 0, 'days_active': 0, 'daily_avg': 0, 'top_product': 'None'},
            'ranking': {'position': 0, 'total_workers': 0, 'top_performer': 'None'}
        }
    finally:
        if conn:
            conn.close()

async def _get_worker_leaderboard() -> list:
    """Get worker leaderboard for current month"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        days_in_month = datetime.now().day
        
        c.execute("""
            SELECT 
                u.user_id, u.username, u.worker_alias, u.worker_daily_quota,
                COUNT(p.id) as drops_this_month
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by AND DATE(p.added_date) >= ?
            WHERE u.is_worker = 1 AND u.worker_status = 'active'
            GROUP BY u.user_id, u.username, u.worker_alias, u.worker_daily_quota
            ORDER BY drops_this_month DESC
        """, (month_start,))
        
        results = c.fetchall()
        leaderboard = []
        
        for result in results:
            daily_avg = result['drops_this_month'] / max(1, days_in_month)
            quota_achievement = (daily_avg / max(1, result['worker_daily_quota'])) * 100
            
            leaderboard.append({
                'user_id': result['user_id'],
                'username': result['username'],
                'alias': result['worker_alias'],
                'drops_this_month': result['drops_this_month'],
                'daily_avg': daily_avg,
                'quota_achievement': quota_achievement
            })
        
        return leaderboard
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker leaderboard: {e}")
        return []
    finally:
        if conn:
            conn.close()

def _generate_progress_bar(percentage: float, length: int = 10) -> str:
    """Generate a visual progress bar"""
    filled = int((percentage / 100) * length)
    empty = length - filled
    return "█" * filled + "░" * empty

# --- END OF FILE worker_interface.py --- 