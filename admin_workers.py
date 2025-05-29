# --- START OF FILE admin_workers.py ---

import sqlite3
import logging
import math
from datetime import datetime, timezone, timedelta
import os

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import telegram.error as telegram_error

# --- Local Imports ---
from utils import (
    ADMIN_ID, LANGUAGES, get_db_connection, send_message_with_retry,
    log_admin_action, _get_lang_data,
    ACTION_WORKER_ROLE_ADD, ACTION_WORKER_ROLE_REMOVE,
    ACTION_WORKER_STATUS_ACTIVATE, ACTION_WORKER_STATUS_DEACTIVATE
)

logger = logging.getLogger(__name__)

WORKERS_PER_PAGE = 10

# --- Main Worker Management Menu ---
async def handle_manage_workers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    lang, lang_data = _get_lang_data(context)

    # Get quick stats
    conn = None
    total_workers = 0
    active_workers = 0
    today_drops = 0
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Total workers
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_worker = 1")
        result = c.fetchone()
        total_workers = result['count'] if result else 0
        
        # Active workers
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_worker = 1 AND worker_status = 'active'")
        result = c.fetchone()
        active_workers = result['count'] if result else 0
        
        # Today's drops by all workers
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute("""
            SELECT COUNT(*) as count 
            FROM products p 
            JOIN users u ON p.added_by = u.user_id 
            WHERE u.is_worker = 1 AND DATE(p.added_date) = ?
        """, (today,))
        result = c.fetchone()
        today_drops = result['count'] if result else 0
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker overview stats: {e}")
    finally:
        if conn: conn.close()

    msg = f"👷 Manage Workers\n\n"
    msg += f"📊 **Quick Overview:**\n"
    msg += f"• Total Workers: {total_workers}\n"
    msg += f"• Active Workers: {active_workers}\n"
    msg += f"• Today's Drops: {today_drops}\n\n"
    msg += "Select an action:"
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Worker", callback_data="adm_add_worker_prompt_id")],
        [InlineKeyboardButton("👥 View Workers", callback_data="adm_view_workers_list|0")],
        [InlineKeyboardButton("📊 Worker Analytics", callback_data="adm_worker_analytics")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="adm_worker_leaderboard")],
        [InlineKeyboardButton("⚙️ Worker Settings", callback_data="adm_worker_settings")],
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Add Worker Flow ---
async def handle_adm_add_worker_prompt_id(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    lang, lang_data = _get_lang_data(context)

    context.user_data['state'] = 'awaiting_worker_id_add'
    prompt_msg = "➕ Add Worker\n\nPlease reply with the Telegram User ID of the person you want to add as a worker."
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="manage_workers_menu")]]
    await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter Worker User ID.")

async def handle_adm_add_worker_id_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes the entered Telegram ID for adding a worker."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if admin_id != ADMIN_ID or context.user_data.get('state') != 'awaiting_worker_id_add':
        return

    if not update.message or not update.message.text:
        await send_message_with_retry(context.bot, chat_id, "Please send the User ID as text.", parse_mode=None)
        return

    entered_id_text = update.message.text.strip()
    try:
        worker_user_id = int(entered_id_text)
    except ValueError:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid User ID. Please enter a number.", parse_mode=None)
        return # Keep state

    context.user_data.pop('state', None) # Clear state as we are processing the ID

    if worker_user_id == admin_id:
        await send_message_with_retry(context.bot, chat_id, "❌ You cannot add yourself as a worker.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
        return

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username, is_worker, worker_status FROM users WHERE user_id = ?", (worker_user_id,))
        user_info = c.fetchone()

        if not user_info:
            await send_message_with_retry(context.bot, chat_id, f"❌ User ID {worker_user_id} not found in the bot's database. They need to /start the bot first.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
            return

        username_display = user_info['username'] or f"ID_{worker_user_id}"
        if user_info['is_worker'] == 1:
            status_msg = f"User @{username_display} (ID: {worker_user_id}) is already a worker. Status: {user_info['worker_status'] or 'N/A'}."
            await send_message_with_retry(context.bot, chat_id, status_msg,
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
            return

        # User exists and is not a worker, prompt admin to confirm
        # TODO: Add invitation flow later if desired
        confirm_msg = f"User @{username_display} (ID: {worker_user_id}) found.\nDo you want to make this user a worker?"
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Make Worker", callback_data=f"adm_confirm_make_worker|{worker_user_id}")],
            [InlineKeyboardButton("❌ No, Cancel", callback_data="manage_workers_menu")]
        ]
        await send_message_with_retry(context.bot, chat_id, confirm_msg, reply_markup=InlineKeyboardMarkup(keyboard))

    except sqlite3.Error as e:
        logger.error(f"DB error checking user {worker_user_id} for worker add: {e}")
        await send_message_with_retry(context.bot, chat_id, "❌ Database error. Please try again.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
    finally:
        if conn: conn.close()

async def handle_adm_confirm_make_worker(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid User ID.", show_alert=True); return

    worker_user_id = int(params[0])
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET is_worker = 1, worker_status = 'active' WHERE user_id = ?", (worker_user_id,))
        if c.rowcount > 0:
            conn.commit()
            log_admin_action(admin_id=admin_id, action=ACTION_WORKER_ROLE_ADD, target_user_id=worker_user_id, new_value='active')
            await query.edit_message_text(f"✅ User ID {worker_user_id} is now a worker and set to 'active'.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Manage Workers", callback_data="manage_workers_menu")]]))
        else:
            conn.rollback()
            await query.edit_message_text(f"❌ Error: Could not update user {worker_user_id}. They might not exist.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
    except sqlite3.Error as e:
        logger.error(f"DB error making user {worker_user_id} a worker: {e}")
        if conn and conn.in_transaction: conn.rollback()
        await query.edit_message_text("❌ Database error. Could not make user a worker.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="manage_workers_menu")]]))
    finally:
        if conn: conn.close()

# --- View & Manage Workers List ---
async def handle_adm_view_workers_list(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])
    
    await _display_worker_list_page(update, context, offset)

async def _display_worker_list_page(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int):
    query = update.callback_query
    workers_data = []
    total_workers = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_worker = 1")
        count_res = c.fetchone(); total_workers = count_res['count'] if count_res else 0
        c.execute("""
            SELECT u.user_id, u.username, u.worker_status, u.worker_alias, COUNT(p.id) as drops_added
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by
            WHERE u.is_worker = 1
            GROUP BY u.user_id, u.username, u.worker_status, u.worker_alias
            ORDER BY u.user_id DESC LIMIT ? OFFSET ?
        """, (WORKERS_PER_PAGE, offset))
        workers_data = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching worker list: {e}")
        await query.edit_message_text("❌ DB Error fetching workers.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    msg = "👥 Worker List\n\n"
    keyboard = []
    item_buttons = []

    if not workers_data and offset == 0: msg += "No workers found."
    elif not workers_data: msg += "No more workers."
    else:
        for worker in workers_data:
            user_id = worker['user_id']
            username = worker['username'] or f"ID_{user_id}"
            alias = f" ({worker['worker_alias']})" if worker['worker_alias'] else ""
            status = worker['worker_status'] or "N/A"
            drops = worker['drops_added']
            item_buttons.append([InlineKeyboardButton(f"@{username}{alias} ({status}) - Drops: {drops}",
                                                   callback_data=f"adm_view_specific_worker|{user_id}|{offset}")])
        keyboard.extend(item_buttons)
        
        total_pages = math.ceil(total_workers / WORKERS_PER_PAGE)
        current_page = (offset // WORKERS_PER_PAGE) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_view_workers_list|{max(0, offset - WORKERS_PER_PAGE)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_view_workers_list|{offset + WORKERS_PER_PAGE}"))
        if nav_buttons: keyboard.append(nav_buttons)
        if total_pages > 0 : msg += f"\nPage {current_page}/{total_pages}"


    keyboard.append([InlineKeyboardButton("⬅️ Back to Manage Workers", callback_data="manage_workers_menu")])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing worker list: {e}")
        else: await query.answer()

async def handle_adm_view_specific_worker(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    worker_user_id = int(params[0])
    offset = int(params[1]) # To go back to the correct page of the worker list
    conn = None
    msg_parts = []
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT u.user_id, u.username, u.worker_status, u.worker_alias,
                   (SELECT COUNT(*) FROM products p WHERE p.added_by = u.user_id) as total_drops,
                   (SELECT MAX(p.added_date) FROM products p WHERE p.added_by = u.user_id) as last_drop_date
            FROM users u
            WHERE u.user_id = ? AND u.is_worker = 1
        """, (worker_user_id,))
        worker_info = c.fetchone()

        if not worker_info:
            await query.edit_message_text("Worker not found or no longer a worker.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Worker List", callback_data=f"adm_view_workers_list|{offset}")]]))
            return

        username = worker_info['username'] or f"ID_{worker_user_id}"
        alias = f" ({worker_info['worker_alias']})" if worker_info['worker_alias'] else ""
        status = worker_info['worker_status'] or "N/A"
        total_drops = worker_info['total_drops']
        last_drop_date_str = "Never"
        if worker_info['last_drop_date']:
            try: last_drop_date_str = datetime.fromisoformat(worker_info['last_drop_date']).strftime("%Y-%m-%d %H:%M")
            except ValueError: pass

        msg_parts.append(f"👷 Worker Profile: @{username}{alias} (ID: {worker_user_id})\n")
        msg_parts.append(f"Status: {status.capitalize()}\n")
        msg_parts.append(f"Total Drops Added: {total_drops}\n")
        msg_parts.append(f"Last Drop Added: {last_drop_date_str}\n")

        # TODO: Add detailed drop log here (paginated) in a future phase

        keyboard = [
            [InlineKeyboardButton(f"{'🟢 Activate' if status == 'inactive' else '🔴 Deactivate'} Worker", callback_data=f"adm_worker_toggle_status|{worker_user_id}|{offset}")],
            # [InlineKeyboardButton("📋 Manage Product Assignments", callback_data=f"adm_worker_assignments|{worker_user_id}|{offset}")], # For Phase 3
            [InlineKeyboardButton("🗑️ Remove Worker Role", callback_data=f"adm_worker_remove_confirm|{worker_user_id}|{offset}")],
            [InlineKeyboardButton("⬅️ Back to Worker List", callback_data=f"adm_view_workers_list|{offset}")]
        ]
        await query.edit_message_text("".join(msg_parts), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except sqlite3.Error as e:
        logger.error(f"DB error fetching specific worker {worker_user_id}: {e}")
        await query.edit_message_text("❌ DB Error fetching worker details.", parse_mode=None)
    finally:
        if conn: conn.close()

async def handle_adm_worker_toggle_status(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    worker_user_id = int(params[0])
    offset = int(params[1]) # To return to the correct page of specific worker view
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT worker_status FROM users WHERE user_id = ? AND is_worker = 1", (worker_user_id,))
        current_data = c.fetchone()
        if not current_data:
            await query.answer("Worker not found.", show_alert=True)
            return await _display_worker_list_page(update, context, offset) # Back to list

        current_status = current_data['worker_status']
        new_status = 'inactive' if current_status == 'active' else 'active'
        c.execute("UPDATE users SET worker_status = ? WHERE user_id = ?", (new_status, worker_user_id))
        conn.commit()

        action_log = ACTION_WORKER_STATUS_ACTIVATE if new_status == 'active' else ACTION_WORKER_STATUS_DEACTIVATE
        log_admin_action(admin_id, action_log, target_user_id=worker_user_id, old_value=current_status, new_value=new_status)
        await query.answer(f"Worker status set to '{new_status}'.")
        # Refresh specific worker view
        await handle_adm_view_specific_worker(update, context, params=[str(worker_user_id), str(offset)])

    except sqlite3.Error as e:
        logger.error(f"DB error toggling worker status for {worker_user_id}: {e}")
        if conn and conn.in_transaction: conn.rollback()
        await query.answer("DB Error toggling status.", show_alert=True)
    finally:
        if conn: conn.close()

async def handle_adm_worker_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    worker_user_id = int(params[0])
    offset = int(params[1]) # For back button
    username = f"ID_{worker_user_id}" # Placeholder
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (worker_user_id,))
        res = c.fetchone(); username = res['username'] if res and res['username'] else username
    except sqlite3.Error: pass
    finally:
        if conn: conn.close()

    context.user_data["confirm_action"] = f"confirm_remove_worker_role|{worker_user_id}|{offset}"
    msg = (f"⚠️ Confirm Worker Role Removal\n\n"
           f"Are you sure you want to remove worker role from @{username} (ID: {worker_user_id})?\n"
           f"They will no longer be able to add products. Their past additions will remain attributed to them.")
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Remove Role", callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_view_specific_worker|{worker_user_id}|{offset}")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# --- NEW: Worker Analytics Dashboard ---
async def handle_adm_worker_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show comprehensive worker analytics dashboard"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    
    analytics = await _get_worker_analytics()
    
    msg = "📊 Worker Analytics Dashboard\n\n"
    
    # Overview stats
    msg += f"📈 **Performance Overview:**\n"
    msg += f"• Total Workers: {analytics['overview']['total_workers']}\n"
    msg += f"• Active Workers: {analytics['overview']['active_workers']}\n"
    msg += f"• Today's Total Drops: {analytics['overview']['today_drops']}\n"
    msg += f"• This Month's Drops: {analytics['overview']['month_drops']}\n\n"
    
    # Top performers
    msg += f"🏆 **Top Performers (This Month):**\n"
    for i, worker in enumerate(analytics['top_performers'][:5], 1):
        username = worker['username'] or f"ID_{worker['user_id']}"
        alias = f" ({worker['alias']})" if worker['alias'] else ""
        msg += f"{i}. @{username}{alias}: {worker['drops']} drops\n"
    
    msg += f"\n📊 **Quota Achievement:**\n"
    msg += f"• Average Quota Achievement: {analytics['quota']['avg_achievement']:.1f}%\n"
    msg += f"• Workers Meeting Quota: {analytics['quota']['meeting_quota']}/{analytics['overview']['active_workers']}\n"
    msg += f"• Best Performer: {analytics['quota']['best_performer']}\n\n"
    
    msg += f"📅 **Activity Trends:**\n"
    msg += f"• Most Active Day: {analytics['trends']['most_active_day']}\n"
    msg += f"• Average Daily Drops: {analytics['trends']['avg_daily_drops']:.1f}\n"
    msg += f"• Growth vs Last Month: {analytics['trends']['growth_percentage']:+.1f}%"
    
    keyboard = [
        [InlineKeyboardButton("📈 Detailed Report", callback_data="adm_worker_detailed_report")],
        [InlineKeyboardButton("📊 Export Data", callback_data="adm_worker_export_data")],
        [InlineKeyboardButton("⬅️ Back to Worker Menu", callback_data="manage_workers_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- NEW: Admin Worker Leaderboard ---
async def handle_adm_worker_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show detailed worker leaderboard for admins"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    
    leaderboard = await _get_detailed_worker_leaderboard()
    
    msg = "🏆 Worker Leaderboard (This Month)\n\n"
    
    for i, worker in enumerate(leaderboard[:15], 1):
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        username = worker['username'] or f"ID_{worker['user_id']}"
        alias = f" ({worker['alias']})" if worker['alias'] else ""
        status_emoji = "🟢" if worker['status'] == 'active' else "🔴"
        
        msg += f"{emoji} {status_emoji} @{username}{alias}\n"
        msg += f"   • Drops: {worker['drops_this_month']}\n"
        msg += f"   • Daily Avg: {worker['daily_avg']:.1f}\n"
        msg += f"   • Quota: {worker['quota_achievement']:.1f}%\n"
        msg += f"   • Last Active: {worker['last_active']}\n\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 Analytics", callback_data="adm_worker_analytics")],
        [InlineKeyboardButton("⚙️ Manage Quotas", callback_data="adm_worker_quota_management")],
        [InlineKeyboardButton("⬅️ Back to Worker Menu", callback_data="manage_workers_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- NEW: Worker Settings ---
async def handle_adm_worker_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Manage global worker settings"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    
    settings = await _get_worker_settings()
    
    msg = "⚙️ Worker Settings\n\n"
    msg += f"📊 **Current Settings:**\n"
    msg += f"• Default Daily Quota: {settings['default_quota']} drops\n"
    msg += f"• Auto-notifications: {'✅ Enabled' if settings['notifications'] else '❌ Disabled'}\n"
    msg += f"• Performance Reports: {'✅ Weekly' if settings['reports'] else '❌ Disabled'}\n\n"
    msg += "Select an action:"
    
    keyboard = [
        [InlineKeyboardButton("📊 Set Default Quota", callback_data="adm_set_default_quota")],
        [InlineKeyboardButton("🔔 Toggle Notifications", callback_data="adm_toggle_worker_notifications")],
        [InlineKeyboardButton("👥 Bulk Worker Actions", callback_data="adm_bulk_worker_actions")],
        [InlineKeyboardButton("📋 Worker Templates", callback_data="adm_worker_templates")],
        [InlineKeyboardButton("⬅️ Back to Worker Menu", callback_data="manage_workers_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Enhanced Worker Profile with More Details ---
async def handle_adm_view_specific_worker_enhanced(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Enhanced worker profile view with detailed statistics"""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    worker_user_id = int(params[0])
    offset = int(params[1])
    
    # Get comprehensive worker data
    worker_data = await _get_comprehensive_worker_data(worker_user_id)
    if not worker_data:
        await query.edit_message_text("Worker not found or no longer a worker.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Worker List", callback_data=f"adm_view_workers_list|{offset}")]]))
        return

    username = worker_data['username'] or f"ID_{worker_user_id}"
    alias = f" ({worker_data['alias']})" if worker_data['alias'] else ""
    status_emoji = "🟢" if worker_data['status'] == 'active' else "🔴"
    
    msg = f"👷 {status_emoji} Worker Profile: @{username}{alias}\n\n"
    
    # Basic info
    msg += f"📋 **Basic Information:**\n"
    msg += f"• Status: {worker_data['status'].capitalize()}\n"
    msg += f"• Daily Quota: {worker_data['daily_quota']} drops\n"
    msg += f"• Worker Since: {worker_data['worker_since']}\n\n"
    
    # Today's performance
    msg += f"📅 **Today's Performance:**\n"
    msg += f"• Drops Added: {worker_data['today']['drops']}\n"
    msg += f"• Quota Progress: {worker_data['today']['quota_progress']:.1f}%\n"
    msg += f"• Avg per Hour: {worker_data['today']['avg_per_hour']:.1f}\n\n"
    
    # This month's stats
    msg += f"📊 **This Month:**\n"
    msg += f"• Total Drops: {worker_data['month']['drops']}\n"
    msg += f"• Daily Average: {worker_data['month']['daily_avg']:.1f}\n"
    msg += f"• Ranking: #{worker_data['month']['rank']} of {worker_data['month']['total_workers']}\n\n"
    
    # All-time stats
    msg += f"🏆 **All-Time Stats:**\n"
    msg += f"• Total Drops: {worker_data['alltime']['drops']}\n"
    msg += f"• Days Active: {worker_data['alltime']['days_active']}\n"
    msg += f"• Best Product: {worker_data['alltime']['top_product']}\n"
    
    keyboard = [
        [InlineKeyboardButton(f"{'🟢 Activate' if worker_data['status'] == 'inactive' else '🔴 Deactivate'} Worker", 
                             callback_data=f"adm_worker_toggle_status|{worker_user_id}|{offset}")],
        [InlineKeyboardButton("⚙️ Edit Quota", callback_data=f"adm_worker_edit_quota|{worker_user_id}|{offset}"),
         InlineKeyboardButton("🏷️ Edit Alias", callback_data=f"adm_worker_edit_alias|{worker_user_id}|{offset}")],
        [InlineKeyboardButton("📊 Detailed Report", callback_data=f"adm_worker_detailed_stats|{worker_user_id}|{offset}"),
         InlineKeyboardButton("📈 Activity Log", callback_data=f"adm_worker_activity_log|{worker_user_id}|{offset}")],
        [InlineKeyboardButton("🗑️ Remove Worker Role", callback_data=f"adm_worker_remove_confirm|{worker_user_id}|{offset}")],
        [InlineKeyboardButton("⬅️ Back to Worker List", callback_data=f"adm_view_workers_list|{offset}")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Message Handler for Worker Management ---
async def handle_admin_worker_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages for worker management flows"""
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""
    
    # Only handle admin worker messages
    if user_id != ADMIN_ID:
        return
    
    state = context.user_data.get('state')
    
    if state == 'awaiting_worker_id_add':
        await handle_adm_add_worker_id_message(update, context)
    elif state == 'awaiting_worker_quota_edit':
        await handle_adm_worker_quota_edit_message(update, context)
    elif state == 'awaiting_worker_alias_edit':
        await handle_adm_worker_alias_edit_message(update, context)
    elif state == 'awaiting_default_quota_set':
        await handle_adm_default_quota_set_message(update, context)

# --- Helper Functions for Enhanced Features ---
async def _get_worker_analytics() -> dict:
    """Get comprehensive worker analytics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        last_month_start = (datetime.now().replace(day=1) - timedelta(days=1)).replace(day=1).strftime('%Y-%m-%d')
        last_month_end = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m-%d')
        
        analytics = {}
        
        # Overview stats
        c.execute("SELECT COUNT(*) as total FROM users WHERE is_worker = 1")
        total_workers = c.fetchone()['total']
        
        c.execute("SELECT COUNT(*) as active FROM users WHERE is_worker = 1 AND worker_status = 'active'")
        active_workers = c.fetchone()['active']
        
        c.execute("""
            SELECT COUNT(*) as today_drops 
            FROM products p JOIN users u ON p.added_by = u.user_id 
            WHERE u.is_worker = 1 AND DATE(p.added_date) = ?
        """, (today,))
        today_drops = c.fetchone()['today_drops']
        
        c.execute("""
            SELECT COUNT(*) as month_drops 
            FROM products p JOIN users u ON p.added_by = u.user_id 
            WHERE u.is_worker = 1 AND DATE(p.added_date) >= ?
        """, (month_start,))
        month_drops = c.fetchone()['month_drops']
        
        analytics['overview'] = {
            'total_workers': total_workers,
            'active_workers': active_workers,
            'today_drops': today_drops,
            'month_drops': month_drops
        }
        
        # Top performers
        c.execute("""
            SELECT u.user_id, u.username, u.worker_alias, COUNT(p.id) as drops
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by AND DATE(p.added_date) >= ?
            WHERE u.is_worker = 1 AND u.worker_status = 'active'
            GROUP BY u.user_id, u.username, u.worker_alias
            ORDER BY drops DESC
            LIMIT 10
        """, (month_start,))
        
        top_performers = []
        for row in c.fetchall():
            top_performers.append({
                'user_id': row['user_id'],
                'username': row['username'],
                'alias': row['worker_alias'],
                'drops': row['drops']
            })
        analytics['top_performers'] = top_performers
        
        # Quota achievement stats
        c.execute("""
            SELECT u.worker_daily_quota, COUNT(p.id) as month_drops
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by AND DATE(p.added_date) >= ?
            WHERE u.is_worker = 1 AND u.worker_status = 'active'
            GROUP BY u.user_id, u.worker_daily_quota
        """, (month_start,))
        
        quota_achievements = []
        days_in_month = datetime.now().day
        for row in c.fetchall():
            daily_avg = row['month_drops'] / max(1, days_in_month)
            achievement = (daily_avg / max(1, row['worker_daily_quota'])) * 100
            quota_achievements.append(achievement)
        
        avg_achievement = sum(quota_achievements) / max(1, len(quota_achievements))
        meeting_quota = len([a for a in quota_achievements if a >= 100])
        
        # Find best performer
        best_performer = top_performers[0]['username'] if top_performers else "None"
        
        analytics['quota'] = {
            'avg_achievement': avg_achievement,
            'meeting_quota': meeting_quota,
            'best_performer': best_performer
        }
        
        # Activity trends
        c.execute("""
            SELECT DATE(p.added_date) as date, COUNT(*) as drops
            FROM products p JOIN users u ON p.added_by = u.user_id
            WHERE u.is_worker = 1 AND DATE(p.added_date) >= ?
            GROUP BY DATE(p.added_date)
            ORDER BY drops DESC
            LIMIT 1
        """, (month_start,))
        
        most_active_result = c.fetchone()
        most_active_day = f"{most_active_result['date']} ({most_active_result['drops']} drops)" if most_active_result else "N/A"
        
        avg_daily_drops = month_drops / max(1, days_in_month)
        
        # Growth calculation
        c.execute("""
            SELECT COUNT(*) as last_month_drops 
            FROM products p JOIN users u ON p.added_by = u.user_id 
            WHERE u.is_worker = 1 AND DATE(p.added_date) BETWEEN ? AND ?
        """, (last_month_start, last_month_end))
        
        last_month_drops = c.fetchone()['last_month_drops']
        growth_percentage = ((month_drops - last_month_drops) / max(1, last_month_drops)) * 100
        
        analytics['trends'] = {
            'most_active_day': most_active_day,
            'avg_daily_drops': avg_daily_drops,
            'growth_percentage': growth_percentage
        }
        
        return analytics
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker analytics: {e}")
        return {
            'overview': {'total_workers': 0, 'active_workers': 0, 'today_drops': 0, 'month_drops': 0},
            'top_performers': [],
            'quota': {'avg_achievement': 0, 'meeting_quota': 0, 'best_performer': 'None'},
            'trends': {'most_active_day': 'N/A', 'avg_daily_drops': 0, 'growth_percentage': 0}
        }
    finally:
        if conn: conn.close()

async def _get_detailed_worker_leaderboard() -> list:
    """Get detailed worker leaderboard with extended information"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        days_in_month = datetime.now().day
        
        c.execute("""
            SELECT 
                u.user_id, u.username, u.worker_alias, u.worker_status, u.worker_daily_quota,
                COUNT(p.id) as drops_this_month,
                MAX(p.added_date) as last_active
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by AND DATE(p.added_date) >= ?
            WHERE u.is_worker = 1
            GROUP BY u.user_id, u.username, u.worker_alias, u.worker_status, u.worker_daily_quota
            ORDER BY drops_this_month DESC
        """, (month_start,))
        
        results = c.fetchall()
        leaderboard = []
        
        for result in results:
            daily_avg = result['drops_this_month'] / max(1, days_in_month)
            quota_achievement = (daily_avg / max(1, result['worker_daily_quota'])) * 100
            
            last_active = "Never"
            if result['last_active']:
                try:
                    last_active = datetime.fromisoformat(result['last_active']).strftime("%m-%d")
                except:
                    pass
            
            leaderboard.append({
                'user_id': result['user_id'],
                'username': result['username'],
                'alias': result['worker_alias'],
                'status': result['worker_status'],
                'drops_this_month': result['drops_this_month'],
                'daily_avg': daily_avg,
                'quota_achievement': quota_achievement,
                'last_active': last_active
            })
        
        return leaderboard
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching detailed worker leaderboard: {e}")
        return []
    finally:
        if conn: conn.close()

async def _get_worker_settings() -> dict:
    """Get current worker settings"""
    # For now, return default settings - could be stored in a settings table
    return {
        'default_quota': 10,
        'notifications': True,
        'reports': True
    }

async def _get_comprehensive_worker_data(worker_user_id: int) -> dict:
    """Get all data for a specific worker"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Basic worker info
        c.execute("""
            SELECT username, worker_status, worker_alias, worker_daily_quota,
                   (SELECT MIN(added_date) FROM products WHERE added_by = ?) as worker_since
            FROM users
            WHERE user_id = ? AND is_worker = 1
        """, (worker_user_id, worker_user_id))
        
        worker_info = c.fetchone()
        if not worker_info:
            return None
        
        worker_since = "N/A"
        if worker_info['worker_since']:
            try:
                worker_since = datetime.fromisoformat(worker_info['worker_since']).strftime("%Y-%m-%d")
            except:
                pass
        
        today = datetime.now().strftime('%Y-%m-%d')
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        days_in_month = datetime.now().day
        
        # Today's stats
        c.execute("""
            SELECT COUNT(*) as drops, MIN(added_date) as first_drop
            FROM products
            WHERE added_by = ? AND DATE(added_date) = ?
        """, (worker_user_id, today))
        
        today_result = c.fetchone()
        drops_today = today_result['drops'] if today_result else 0
        quota_progress = (drops_today / max(1, worker_info['worker_daily_quota'])) * 100
        
        # Calculate average per hour for today
        avg_per_hour = 0
        if today_result['first_drop'] and drops_today > 0:
            try:
                first_time = datetime.fromisoformat(today_result['first_drop'])
                hours_working = max(1, (datetime.now() - first_time).total_seconds() / 3600)
                avg_per_hour = drops_today / hours_working
            except:
                pass
        
        # Month stats
        c.execute("""
            SELECT COUNT(*) as month_drops
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
        """, (worker_user_id, month_start))
        
        month_drops = c.fetchone()['month_drops']
        month_daily_avg = month_drops / max(1, days_in_month)
        
        # Get ranking
        c.execute("""
            SELECT user_id, COUNT(*) as drops
            FROM products p JOIN users u ON p.added_by = u.user_id
            WHERE u.is_worker = 1 AND u.worker_status = 'active' AND DATE(p.added_date) >= ?
            GROUP BY user_id
            ORDER BY drops DESC
        """, (month_start,))
        
        ranking_results = c.fetchall()
        rank = 0
        for i, result in enumerate(ranking_results, 1):
            if result['user_id'] == worker_user_id:
                rank = i
                break
        
        # All-time stats
        c.execute("""
            SELECT COUNT(*) as total_drops, 
                   COUNT(DISTINCT DATE(added_date)) as days_active,
                   product_type
            FROM products
            WHERE added_by = ?
            GROUP BY product_type
            ORDER BY COUNT(*) DESC
        """, (worker_user_id,))
        
        alltime_results = c.fetchall()
        total_drops = sum(result['total_drops'] for result in alltime_results)
        days_active = alltime_results[0]['days_active'] if alltime_results else 0
        top_product = alltime_results[0]['product_type'] if alltime_results else "None"
        
        return {
            'username': worker_info['username'],
            'alias': worker_info['worker_alias'],
            'status': worker_info['worker_status'],
            'daily_quota': worker_info['worker_daily_quota'],
            'worker_since': worker_since,
            'today': {
                'drops': drops_today,
                'quota_progress': quota_progress,
                'avg_per_hour': avg_per_hour
            },
            'month': {
                'drops': month_drops,
                'daily_avg': month_daily_avg,
                'rank': rank,
                'total_workers': len(ranking_results)
            },
            'alltime': {
                'drops': total_drops,
                'days_active': days_active,
                'top_product': top_product
            }
        }
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching comprehensive worker data for {worker_user_id}: {e}")
        return None
    finally:
        if conn: conn.close()

# --- Message Handlers for New Features ---
async def handle_adm_worker_quota_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quota edit message"""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if admin_id != ADMIN_ID or context.user_data.get('state') != 'awaiting_worker_quota_edit':
        return
    
    try:
        new_quota = int(update.message.text.strip())
        if new_quota < 0 or new_quota > 100:
            await send_message_with_retry(context.bot, chat_id, "❌ Invalid quota. Please enter a number between 0-100.", parse_mode=None)
            return
        
        worker_id = context.user_data.get('editing_worker_id')
        offset = context.user_data.get('editing_worker_offset', 0)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET worker_daily_quota = ? WHERE user_id = ?", (new_quota, worker_id))
        conn.commit()
        conn.close()
        
        context.user_data.pop('state', None)
        context.user_data.pop('editing_worker_id', None)
        context.user_data.pop('editing_worker_offset', None)
        
        await send_message_with_retry(context.bot, chat_id, f"✅ Daily quota updated to {new_quota} drops.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Worker Profile", callback_data=f"adm_view_specific_worker|{worker_id}|{offset}")]]))
        
    except ValueError:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid number. Please enter a valid quota (0-100).", parse_mode=None)

async def handle_adm_worker_alias_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle alias edit message"""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if admin_id != ADMIN_ID or context.user_data.get('state') != 'awaiting_worker_alias_edit':
        return
    
    new_alias = update.message.text.strip()
    if len(new_alias) > 20:
        await send_message_with_retry(context.bot, chat_id, "❌ Alias too long. Maximum 20 characters.", parse_mode=None)
        return
    
    worker_id = context.user_data.get('editing_worker_id')
    offset = context.user_data.get('editing_worker_offset', 0)
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET worker_alias = ? WHERE user_id = ?", (new_alias if new_alias else None, worker_id))
    conn.commit()
    conn.close()
    
    context.user_data.pop('state', None)
    context.user_data.pop('editing_worker_id', None)
    context.user_data.pop('editing_worker_offset', None)
    
    display_alias = new_alias if new_alias else "None"
    await send_message_with_retry(context.bot, chat_id, f"✅ Worker alias updated to: {display_alias}",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Worker Profile", callback_data=f"adm_view_specific_worker|{worker_id}|{offset}")]]))

# --- Enhanced Worker Profile Management ---
async def handle_adm_worker_edit_alias(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle alias editing for workers"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: Invalid data.", show_alert=True)

    worker_id = int(params[0])
    offset = int(params[1])
    
    # Get current alias
    conn = None
    current_alias = ""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT worker_alias, username FROM users WHERE user_id = ? AND is_worker = 1", (worker_id,))
        result = c.fetchone()
        if result:
            current_alias = result['worker_alias'] or ""
            username = result['username'] or f"ID_{worker_id}"
        else:
            return await query.answer("Worker not found.", show_alert=True)
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker alias: {e}")
        return await query.answer("Database error.", show_alert=True)
    finally:
        if conn: conn.close()
    
    context.user_data['state'] = 'awaiting_worker_alias_edit'
    context.user_data['editing_worker_id'] = worker_id
    context.user_data['editing_worker_offset'] = offset
    
    msg = f"✏️ **Edit Worker Alias**\n\n"
    msg += f"Worker: @{username} (ID: {worker_id})\n"
    msg += f"Current Alias: {current_alias or 'None'}\n\n"
    msg += "Please reply with the new alias (max 20 characters) or send '-' to remove alias:"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_view_specific_worker|{worker_id}|{offset}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer("Enter new alias in chat.")

async def handle_adm_worker_edit_quota(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle quota editing for workers"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: Invalid data.", show_alert=True)

    worker_id = int(params[0])
    offset = int(params[1])
    
    # Get current quota
    conn = None
    current_quota = 10
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT worker_daily_quota, username FROM users WHERE user_id = ? AND is_worker = 1", (worker_id,))
        result = c.fetchone()
        if result:
            current_quota = result['worker_daily_quota'] or 10
            username = result['username'] or f"ID_{worker_id}"
        else:
            return await query.answer("Worker not found.", show_alert=True)
    except sqlite3.Error as e:
        logger.error(f"Error fetching worker quota: {e}")
        return await query.answer("Database error.", show_alert=True)
    finally:
        if conn: conn.close()
    
    context.user_data['state'] = 'awaiting_worker_quota_edit'
    context.user_data['editing_worker_id'] = worker_id
    context.user_data['editing_worker_offset'] = offset
    
    msg = f"📊 **Edit Daily Quota**\n\n"
    msg += f"Worker: @{username} (ID: {worker_id})\n"
    msg += f"Current Daily Quota: {current_quota} drops\n\n"
    msg += "Please reply with the new daily quota (0-100):"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_view_specific_worker|{worker_id}|{offset}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer("Enter new quota in chat.")

# --- Export Functionality ---
async def handle_adm_export_performance_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Export comprehensive performance summary to CSV"""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    
    await query.answer("🔄 Generating performance export...")
    
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Get comprehensive worker data
        c.execute("""
            SELECT 
                u.user_id,
                u.username,
                COALESCE(u.worker_alias, '') as alias,
                u.worker_status,
                COALESCE(u.daily_quota, 10) as daily_quota,
                COALESCE(u.worker_performance_score, 0) as performance_score,
                u.created_at as joined_date,
                COUNT(p.id) as total_drops,
                COALESCE(SUM(p.price), 0) as total_revenue,
                COUNT(CASE WHEN DATE(p.created_at, 'localtime') = DATE('now', 'localtime') THEN 1 END) as today_drops,
                COUNT(CASE WHEN p.created_at >= datetime('now', '-7 days') THEN 1 END) as week_drops,
                COUNT(CASE WHEN p.created_at >= datetime('now', '-30 days') THEN 1 END) as month_drops
            FROM users u
            LEFT JOIN products p ON u.user_id = p.added_by
            WHERE u.is_worker = 1
            GROUP BY u.user_id
            ORDER BY total_drops DESC
        """)
        
        workers = c.fetchall()
        
        # Generate CSV content
        csv_content = "Worker ID,Username,Alias,Status,Daily Quota,Performance Score,Joined Date,Total Drops,Total Revenue,Today Drops,Week Drops,Month Drops,Efficiency (EUR/Drop),Daily Progress %\n"
        
        for worker in workers:
            efficiency = float(worker['total_revenue']) / worker['total_drops'] if worker['total_drops'] > 0 else 0
            daily_progress = (worker['today_drops'] / worker['daily_quota']) * 100
            
            display_name = worker['alias'] or worker['username'] or f"Worker_{worker['user_id']}"
            
            csv_content += f"{worker['user_id']},{worker['username']},{worker['alias']},{worker['worker_status']},{worker['daily_quota']},{worker['performance_score']},{worker['joined_date']},{worker['total_drops']},{worker['total_revenue']:.2f},{worker['today_drops']},{worker['week_drops']},{worker['month_drops']},{efficiency:.2f},{daily_progress:.1f}\n"
        
        # Create temporary file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"worker_performance_summary_{timestamp}.csv"
        filepath = os.path.join(os.getcwd(), filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(csv_content)
        
        # Send file to admin
        with open(filepath, 'rb') as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=f,
                filename=filename,
                caption=f"📊 Worker Performance Summary Export\n📅 Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n👥 Workers: {len(workers)}"
            )
        
        # Clean up file
        os.remove(filepath)
        
        msg = "✅ Performance summary exported successfully! Check your documents."
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back to Analytics", callback_data="adm_worker_analytics")
        ]]))
        
    except Exception as e:
        logger.error(f"Error checking daily quota for worker {user_id}: {e}")

async def _check_performance_milestones(context, user_id, username, alias, performance_score):
    """Check and notify performance score milestones"""
    try:
        display_name = alias or username or f"Worker_{user_id}"
        milestones = [100, 250, 500, 1000, 2500, 5000]
        
        conn = get_db_connection()
        c = conn.cursor()
        
        for milestone in milestones:
            if performance_score >= milestone:
                # Check if already notified for this milestone
                c.execute("""
                    SELECT id FROM worker_notifications 
                    WHERE worker_id = ? AND notification_type = ?
                """, (user_id, f"performance_{milestone}"))
                
                if not c.fetchone():
                    # Send notification
                    message = f"🏆 **{display_name}** reached {milestone} performance points!\n"
                    message += f"Current score: {performance_score}\n"
                    message += "Keep up the excellent work! 🎉"
                    
                    # Send to admin
                    await send_message_with_retry(context.bot, ADMIN_ID, message, parse_mode=None)
                    
                    # Send to worker
                    await send_message_with_retry(context.bot, user_id, 
                        f"🏆 Milestone achieved! You've reached {milestone} performance points!\n"
                        f"Current score: {performance_score}\n"
                        f"🎉 Keep up the excellent work!", parse_mode=None)
                    
                    # Log notification
                    c.execute("""
                        INSERT INTO worker_notifications 
                        (worker_id, notification_type, message, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (user_id, f"performance_{milestone}", message, datetime.now(timezone.utc)))
                    
                    conn.commit()
                    logger.info(f"Performance milestone notification sent to worker {user_id} for {milestone} points")
                    
        conn.close()
        
    except Exception as e:
        logger.error(f"Error checking performance milestones for worker {user_id}: {e}")

async def _check_weekly_achievements(context, user_id, username, alias):
    """Check and notify weekly achievements"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Get this week's drops
        today = datetime.now(timezone.utc)
        week_start = today - timedelta(days=today.weekday())
        
        c.execute("""
            SELECT COUNT(*) as count FROM products 
            WHERE added_by = ? AND created_at >= ?
        """, (user_id, week_start))
        
        weekly_drops = c.fetchone()['count']
        display_name = alias or username or f"Worker_{user_id}"
        
        # Weekly milestones
        weekly_milestones = [50, 100, 200, 500]
        
        for milestone in weekly_milestones:
            if weekly_drops >= milestone:
                # Check if already notified this week for this milestone
                c.execute("""
                    SELECT id FROM worker_notifications 
                    WHERE worker_id = ? AND notification_type = ? 
                    AND created_at >= ?
                """, (user_id, f"weekly_{milestone}", week_start))
                
                if not c.fetchone():
                    # Send notification
                    message = f"📅 **{display_name}** weekly achievement!\n"
                    message += f"🎯 {milestone} drops this week!\n"
                    message += f"Total this week: {weekly_drops}\n"
                    message += "🔥 Outstanding weekly performance!"
                    
                    # Send to admin
                    await send_message_with_retry(context.bot, ADMIN_ID, message, parse_mode=None)
                    
                    # Send to worker
                    await send_message_with_retry(context.bot, user_id, 
                        f"📅 Weekly achievement unlocked!\n"
                        f"🎯 {milestone} drops this week!\n"
                        f"Total: {weekly_drops}\n"
                        f"🔥 Outstanding performance!", parse_mode=None)
                    
                    # Log notification
                    c.execute("""
                        INSERT INTO worker_notifications 
                        (worker_id, notification_type, message, created_at)
                        VALUES (?, ?, ?, ?)
                    """, (user_id, f"weekly_{milestone}", message, datetime.now(timezone.utc)))
                    
                    conn.commit()
                    logger.info(f"Weekly achievement notification sent to worker {user_id} for {milestone} drops")
                    
        conn.close()
        
    except Exception as e:
        logger.error(f"Error checking weekly achievements for worker {user_id}: {e}")

# --- END OF FILE admin_workers.py ---