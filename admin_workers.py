# --- START OF FILE admin_workers.py ---

import sqlite3
import logging
import math
from datetime import datetime, timezone

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

    # TODO: Add translations for these if needed
    msg = "👷 Manage Workers\n\nSelect an action:"
    keyboard = [
        [InlineKeyboardButton("➕ Add Worker", callback_data="adm_add_worker_prompt_id")],
        [InlineKeyboardButton("👥 View Workers", callback_data="adm_view_workers_list|0")],
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
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

# --- END OF FILE admin_workers.py ---