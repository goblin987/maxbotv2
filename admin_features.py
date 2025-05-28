# --- START OF FILE admin_features.py ---

import sqlite3
import os
import logging
import json
# import tempfile # Not used by functions in this file
import shutil # Used by handle_confirm_yes for media deletion
import time # Used by handle_confirm_yes for product name generation (though that part might be in product_management)
import secrets # For generating random codes in discount management
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import math # Add math for pagination calculation
from decimal import Decimal # Ensure Decimal is imported

# Need emoji library for validation (or implement a simpler check)
# Let's try a simpler check first to avoid adding a dependency
# import emoji # Optional, for more robust emoji validation

# --- Telegram Imports ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    # InputMediaPhoto, InputMediaVideo, InputMediaAnimation # Not directly used by functions here, but handle_confirm_yes might interact with media deletion
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes # Import JobQueue if any jobs are defined/managed here
from telegram import helpers
import telegram.error as telegram_error

# --- Local Imports ---
# Import only what's needed by the functions moved to this file
from utils import (
    CITIES, DISTRICTS, PRODUCT_TYPES, ADMIN_ID, LANGUAGES, THEMES, # PRODUCT_TYPES for sales reports, discount/welcome previews
    # BOT_MEDIA, SIZES, # Not directly used here
    fetch_reviews, format_currency, send_message_with_retry,
    get_date_range, TOKEN, load_all_data, format_discount_value,
    # SECONDARY_ADMIN_IDS, # Not directly used by functions in this file for permission checks
    get_db_connection, MEDIA_DIR, # MEDIA_DIR for handle_confirm_yes
    DEFAULT_PRODUCT_EMOJI, # For sales reports, welcome previews
    fetch_user_ids_for_broadcast,
    get_welcome_message_templates, get_welcome_message_template_count,
    add_welcome_message_template,
    update_welcome_message_template,
    delete_welcome_message_template,
    set_active_welcome_message,
    DEFAULT_WELCOME_MESSAGE,
    get_user_status, get_progress_bar, # For welcome message preview
    _get_lang_data,
    log_admin_action, ACTION_RESELLER_DISCOUNT_DELETE, ACTION_PRODUCT_TYPE_REASSIGN # For handle_confirm_yes
)

# Logging setup
logger = logging.getLogger(__name__)

# Constants for features in this file
TEMPLATES_PER_PAGE = 5 # Pagination for welcome templates

# --- Sales Analytics Handlers ---
async def handle_sales_analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the sales analytics submenu."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    msg = "📊 Sales Analytics\n\nSelect a report or view:"
    keyboard = [
        [InlineKeyboardButton("📈 View Dashboard", callback_data="sales_dashboard")],
        [InlineKeyboardButton("📅 Generate Report", callback_data="sales_select_period|main")],
        [InlineKeyboardButton("🏙️ Sales by City", callback_data="sales_select_period|by_city")],
        [InlineKeyboardButton("💎 Sales by Type", callback_data="sales_select_period|by_type")],
        [InlineKeyboardButton("🏆 Top Products", callback_data="sales_select_period|top_prod")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_sales_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays a quick sales dashboard for today, this week, this month."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    periods = {
        "today": ("☀️ Today ({})", datetime.now(timezone.utc).strftime("%Y-%m-%d")), # Use UTC
        "week": ("🗓️ This Week (Mon-Sun)", None),
        "month": ("📆 This Month", None)
    }
    msg = "📊 Sales Dashboard\n\n"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        for period_key, (label_template, date_str) in periods.items():
            start, end = get_date_range(period_key)
            if not start or not end:
                msg += f"Could not calculate range for {period_key}.\n\n"
                continue
            # Use column names
            c.execute("SELECT COALESCE(SUM(price_paid), 0.0) as total_revenue, COUNT(*) as total_units FROM purchases WHERE purchase_date BETWEEN ? AND ?", (start, end))
            result = c.fetchone()
            revenue = result['total_revenue'] if result else 0.0
            units = result['total_units'] if result else 0
            aov = revenue / units if units > 0 else 0.0
            revenue_str = format_currency(revenue)
            aov_str = format_currency(aov)
            label_formatted = label_template.format(date_str) if date_str else label_template
            msg += f"{label_formatted}\n"
            msg += f"    Revenue: {revenue_str} EUR\n"
            msg += f"    Units Sold: {units}\n"
            msg += f"    Avg Order Value: {aov_str} EUR\n\n"
    except sqlite3.Error as e:
        logger.error(f"DB error generating sales dashboard: {e}", exc_info=True)
        msg += "\n❌ Error fetching dashboard data."
    except Exception as e:
        logger.error(f"Unexpected error in sales dashboard: {e}", exc_info=True)
        msg += "\n❌ An unexpected error occurred."
    finally:
         if conn: conn.close() # Close connection if opened
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="sales_analytics_menu")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing sales dashboard: {e}")
        else: await query.answer()

async def handle_sales_select_period(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows options for selecting a reporting period."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params:
        logger.warning("handle_sales_select_period called without report_type.")
        return await query.answer("Error: Report type missing.", show_alert=True)
    report_type = params[0]
    context.user_data['sales_report_type'] = report_type
    keyboard = [
        [InlineKeyboardButton("Today", callback_data=f"sales_run|{report_type}|today"),
         InlineKeyboardButton("Yesterday", callback_data=f"sales_run|{report_type}|yesterday")],
        [InlineKeyboardButton("This Week", callback_data=f"sales_run|{report_type}|week"),
         InlineKeyboardButton("Last Week", callback_data=f"sales_run|{report_type}|last_week")],
        [InlineKeyboardButton("This Month", callback_data=f"sales_run|{report_type}|month"),
         InlineKeyboardButton("Last Month", callback_data=f"sales_run|{report_type}|last_month")],
        [InlineKeyboardButton("Year To Date", callback_data=f"sales_run|{report_type}|year")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sales_analytics_menu")]
    ]
    await query.edit_message_text("📅 Select Reporting Period", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_sales_run(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Generates and displays the selected sales report."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2:
        logger.warning("handle_sales_run called with insufficient parameters.")
        return await query.answer("Error: Report type or period missing.", show_alert=True)
    report_type, period_key = params[0], params[1]
    start_time, end_time = get_date_range(period_key)
    if not start_time or not end_time:
        return await query.edit_message_text("❌ Error: Invalid period selected.", parse_mode=None)
    period_title = period_key.replace('_', ' ').title()
    msg = ""
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        # row_factory is set in helper
        c = conn.cursor()
        base_query = "FROM purchases WHERE purchase_date BETWEEN ? AND ?"
        base_params = (start_time, end_time)
        if report_type == "main":
            c.execute(f"SELECT COALESCE(SUM(price_paid), 0.0) as total_revenue, COUNT(*) as total_units {base_query}", base_params)
            result = c.fetchone()
            revenue = result['total_revenue'] if result else 0.0
            units = result['total_units'] if result else 0
            aov = revenue / units if units > 0 else 0.0
            revenue_str = format_currency(revenue)
            aov_str = format_currency(aov)
            msg = (f"📊 Sales Report: {period_title}\n\nRevenue: {revenue_str} EUR\n"
                   f"Units Sold: {units}\nAvg Order Value: {aov_str} EUR")
        elif report_type == "by_city":
            c.execute(f"SELECT city, COALESCE(SUM(price_paid), 0.0) as city_revenue, COUNT(*) as city_units {base_query} GROUP BY city ORDER BY city_revenue DESC", base_params)
            results = c.fetchall()
            msg = f"🏙️ Sales by City: {period_title}\n\n"
            if results:
                for row in results:
                    msg += f"{row['city'] or 'N/A'}: {format_currency(row['city_revenue'])} EUR ({row['city_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        elif report_type == "by_type":
            c.execute(f"SELECT product_type, COALESCE(SUM(price_paid), 0.0) as type_revenue, COUNT(*) as type_units {base_query} GROUP by product_type ORDER BY type_revenue DESC", base_params)
            results = c.fetchall()
            msg = f"📊 Sales by Type: {period_title}\n\n"
            if results:
                for row in results:
                    type_name = row['product_type'] or 'N/A'
                    emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)
                    msg += f"{emoji} {type_name}: {format_currency(row['type_revenue'])} EUR ({row['type_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        elif report_type == "top_prod":
            c.execute(f"""
                SELECT pu.product_name, pu.product_size, pu.product_type,
                       COALESCE(SUM(pu.price_paid), 0.0) as prod_revenue,
                       COUNT(pu.id) as prod_units
                FROM purchases pu
                WHERE pu.purchase_date BETWEEN ? AND ?
                GROUP BY pu.product_name, pu.product_size, pu.product_type
                ORDER BY prod_revenue DESC LIMIT 10
            """, base_params) # Simplified query relying on purchase record details
            results = c.fetchall()
            msg = f"🏆 Top Products: {period_title}\n\n"
            if results:
                for i, row in enumerate(results):
                    type_name = row['product_type'] or 'N/A'
                    emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)
                    msg += f"{i+1}. {emoji} {row['product_name'] or 'N/A'} ({row['product_size'] or 'N/A'}): {format_currency(row['prod_revenue'])} EUR ({row['prod_units'] or 0} units)\n"
            else: msg += "No sales data for this period."
        else: msg = "❌ Unknown report type requested."
    except sqlite3.Error as e:
        logger.error(f"DB error generating sales report '{report_type}' for '{period_key}': {e}", exc_info=True)
        msg = "❌ Error generating report due to database issue."
    except Exception as e:
        logger.error(f"Unexpected error generating sales report: {e}", exc_info=True)
        msg = "❌ An unexpected error occurred."
    finally:
         if conn: conn.close()
    keyboard = [[InlineKeyboardButton("⬅️ Back to Period", callback_data=f"sales_select_period|{report_type}"),
                 InlineKeyboardButton("📊 Analytics Menu", callback_data="sales_analytics_menu")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing sales report: {e}")
        else: await query.answer()

# --- Discount Handlers ---
async def handle_adm_manage_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays existing discount codes and management options."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("""
            SELECT id, code, discount_type, value, is_active, max_uses, uses_count, expiry_date
            FROM discount_codes ORDER BY created_date DESC
        """)
        codes = c.fetchall()
        msg = "🏷️ Manage General Discount Codes\n\n" # Clarified title
        keyboard = []
        if not codes: msg += "No general discount codes found."
        else:
            for code in codes: # Access by column name
                status = "✅ Active" if code['is_active'] else "❌ Inactive"
                value_str = format_discount_value(code['discount_type'], code['value'])
                usage_limit = f"/{code['max_uses']}" if code['max_uses'] is not None else "/∞"
                usage = f"{code['uses_count']}{usage_limit}"
                expiry_info = ""
                if code['expiry_date']:
                     try:
                         # Ensure stored date is treated as UTC before comparison
                         expiry_dt = datetime.fromisoformat(code['expiry_date']).replace(tzinfo=timezone.utc)
                         expiry_info = f" | Expires: {expiry_dt.strftime('%Y-%m-%d')}"
                         # Compare with current UTC time
                         if datetime.now(timezone.utc) > expiry_dt and code['is_active']: status = "⏳ Expired"
                     except ValueError: expiry_info = " | Invalid Date"
                toggle_text = "Deactivate" if code['is_active'] else "Activate"
                delete_text = "🗑️ Delete"
                code_text = code['code']
                msg += f"`{code_text}` ({value_str} {code['discount_type']}) | {status} | Used: {usage}{expiry_info}\n" # Use markdown for code
                keyboard.append([
                    InlineKeyboardButton(f"{'❌' if code['is_active'] else '✅'} {toggle_text}", callback_data=f"adm_toggle_discount|{code['id']}"),
                    InlineKeyboardButton(f"{delete_text}", callback_data=f"adm_delete_discount|{code['id']}")
                ])
        keyboard.extend([
            [InlineKeyboardButton("➕ Add New General Discount", callback_data="adm_add_discount_start")],
            [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
        ])
        try:
             # Use MarkdownV2 for code formatting
            await query.edit_message_text(helpers.escape_markdown(msg, version=2), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing discount list (MarkdownV2): {e}. Falling back to plain.")
                 try:
                     # Fallback to plain text
                     plain_msg = msg.replace('`', '') # Simple removal
                     await query.edit_message_text(plain_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
                 except Exception as fallback_e:
                     logger.error(f"Error editing discount list (Fallback): {fallback_e}")
                     await query.answer("Error updating list.", show_alert=True)
             else: await query.answer() # Ignore not modified
    except sqlite3.Error as e:
        logger.error(f"DB error loading discount codes: {e}", exc_info=True)
        await query.edit_message_text("❌ Error loading discount codes.", parse_mode=None)
    except Exception as e:
         logger.error(f"Unexpected error managing discounts: {e}", exc_info=True)
         await query.edit_message_text("❌ An unexpected error occurred.", parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_toggle_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Activates or deactivates a specific discount code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Code ID missing.", show_alert=True)
    conn = None # Initialize conn
    try:
        code_id = int(params[0])
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("SELECT is_active FROM discount_codes WHERE id = ?", (code_id,))
        result = c.fetchone()
        if not result: return await query.answer("Code not found.", show_alert=True)
        current_status = result['is_active']
        new_status = 0 if current_status == 1 else 1
        c.execute("UPDATE discount_codes SET is_active = ? WHERE id = ?", (new_status, code_id))
        conn.commit()
        action = 'deactivated' if new_status == 0 else 'activated'
        logger.info(f"Admin {query.from_user.id} {action} discount code ID {code_id}.")
        await query.answer(f"Code {action} successfully.")
        await handle_adm_manage_discounts(update, context) # Refresh list
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Error toggling discount code {params[0]}: {e}", exc_info=True)
        await query.answer("Error updating code status.", show_alert=True)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_delete_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles delete button press for discount code, shows confirmation."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Code ID missing.", show_alert=True)
    conn = None # Initialize conn
    try:
        code_id = int(params[0])
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("SELECT code FROM discount_codes WHERE id = ?", (code_id,))
        result = c.fetchone()
        if not result: return await query.answer("Code not found.", show_alert=True)
        code_text = result['code']
        context.user_data["confirm_action"] = f"delete_discount|{code_id}"
        msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to permanently delete discount code: `{helpers.escape_markdown(code_text, version=2)}`?\n\n"
               f"🚨 This action is irreversible!")
        keyboard = [[InlineKeyboardButton("✅ Yes, Delete Code", callback_data="confirm_yes"),
                     InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_discounts")]]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Error preparing delete confirmation for discount code {params[0]}: {e}", exc_info=True)
        await query.answer("Error fetching code details.", show_alert=True)
    except telegram_error.BadRequest as e_tg:
         # Fallback if Markdown fails
         logger.warning(f"Markdown error displaying delete confirm: {e_tg}. Falling back.")
         msg_plain = msg.replace('`', '') # Simple removal
         await query.edit_message_text(msg_plain, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    finally:
        if conn: conn.close() # Close connection if opened


async def handle_adm_add_discount_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the process of adding a new discount code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    context.user_data['state'] = 'awaiting_discount_code'
    context.user_data['new_discount_info'] = {} # Initialize dict
    random_code = secrets.token_urlsafe(8).upper().replace('-', '').replace('_', '')[:8]
    keyboard = [
        [InlineKeyboardButton(f"Use Generated: {random_code}", callback_data=f"adm_use_generated_code|{random_code}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]
    ]
    await query.edit_message_text(
        "🏷️ Add New General Discount Code\n\nPlease reply with the code text you want to use (e.g., SUMMER20), or use the generated one below.\n"
        "Codes are case-sensitive.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=None
    )
    await query.answer("Enter code text or use generated.")


async def handle_adm_use_generated_code(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles using the suggested random code."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Generated code missing.", show_alert=True)
    code_text = params[0]
    await process_discount_code_input(update, context, code_text) # This function will handle message editing


async def handle_adm_set_discount_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Sets the discount type and asks for the value."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Discount type missing.", show_alert=True)
    current_state = context.user_data.get("state")
    if current_state not in ['awaiting_discount_type', 'awaiting_discount_code']: # Check if state is valid
         logger.warning(f"handle_adm_set_discount_type called in wrong state: {current_state}")
         if context.user_data and 'new_discount_info' in context.user_data and 'code' in context.user_data['new_discount_info']:
             context.user_data['state'] = 'awaiting_discount_type'
             logger.info("Forcing state back to awaiting_discount_type")
         else:
             return await handle_adm_manage_discounts(update, context)

    discount_type = params[0]
    if discount_type not in ['percentage', 'fixed']:
        return await query.answer("Invalid discount type.", show_alert=True)
    if 'new_discount_info' not in context.user_data: context.user_data['new_discount_info'] = {}
    context.user_data['new_discount_info']['type'] = discount_type
    context.user_data['state'] = 'awaiting_discount_value'
    value_prompt = ("Enter the percentage value (e.g., 10 for 10%):" if discount_type == 'percentage' else
                    "Enter the fixed discount amount in EUR (e.g., 5.50):")
    code_text = context.user_data.get('new_discount_info', {}).get('code', 'N/A')
    msg = f"Code: {code_text} | Type: {discount_type.capitalize()}\n\n{value_prompt}"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer("Enter the discount value.")
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
             logger.error(f"Error editing message in handle_adm_set_discount_type: {e}. Message: {msg}")
             await query.answer("Error updating prompt. Please try again.", show_alert=True)
        else: await query.answer()

# --- Message Handlers for Discount Creation ---
async def process_discount_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE, code_text: str):
    """Shared logic to process entered/generated discount code and ask for type."""
    chat_id = update.effective_chat.id
    query = update.callback_query
    if not code_text:
        msg = "Code cannot be empty. Please try again."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return
    if len(code_text) > 50:
        msg = "Code too long (max 50 chars)."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("SELECT 1 FROM discount_codes WHERE code = ?", (code_text,))
        if c.fetchone():
            error_msg = f"❌ Error: Discount code '{code_text}' already exists."
            if query:
                try: await query.edit_message_text(error_msg, parse_mode=None)
                except telegram_error.BadRequest: await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
            else: await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
            return
    except sqlite3.Error as e:
        logger.error(f"DB error checking discount code uniqueness: {e}")
        error_msg = "❌ Database error checking code uniqueness."
        if query: await query.answer("DB Error.", show_alert=True)
        await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
        context.user_data.pop('state', None)
        return
    finally:
        if conn: conn.close() # Close connection if opened
    if 'new_discount_info' not in context.user_data: context.user_data['new_discount_info'] = {}
    context.user_data['new_discount_info']['code'] = code_text
    context.user_data['state'] = 'awaiting_discount_type'
    keyboard = [
        [InlineKeyboardButton("％ Percentage", callback_data="adm_set_discount_type|percentage"),
         InlineKeyboardButton("€ Fixed Amount", callback_data="adm_set_discount_type|fixed")],
        [InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_discounts")]
    ]
    prompt_msg = f"Code set to: {code_text}\n\nSelect the discount type:"
    if query:
        try: await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest: await query.answer() # Ignore if not modified
    else: await send_message_with_retry(context.bot, chat_id, prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_discount_code_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount code text via message."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != "awaiting_discount_code": return
    if not update.message or not update.message.text: return
    code_text = update.message.text.strip()
    await process_discount_code_input(update, context, code_text)

async def handle_adm_discount_value_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount value and saves the code."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != "awaiting_discount_value": return
    if not update.message or not update.message.text: return
    value_text = update.message.text.strip().replace(',', '.')
    discount_info = context.user_data.get('new_discount_info', {})
    code = discount_info.get('code'); dtype = discount_info.get('type')
    if not code or not dtype:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Discount context lost.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
        return
    conn = None # Initialize conn
    try:
        value = float(value_text)
        if value <= 0: raise ValueError("Discount value must be positive.")
        if dtype == 'percentage' and (value > 100): raise ValueError("Percentage cannot exceed 100.")
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("INSERT INTO discount_codes (code, discount_type, value, created_date, is_active) VALUES (?, ?, ?, ?, 1)",
                  (code, dtype, value, datetime.now(timezone.utc).isoformat())) # Use UTC Time
        conn.commit()
        logger.info(f"Admin {user_id} added discount code: {code} ({dtype}, {value})")
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
        await send_message_with_retry(context.bot, chat_id, f"✅ Discount code '{code}' added!", parse_mode=None)
        keyboard = [[InlineKeyboardButton("🏷️ View Discount Codes", callback_data="adm_manage_discounts")]]
        await send_message_with_retry(context.bot, chat_id, "Returning to discount management.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except ValueError as e:
        await send_message_with_retry(context.bot, chat_id, f"❌ Invalid Value: {e}. Enter valid positive number.", parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error saving discount code '{code}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Database error saving code.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_discount_info", None)
    finally:
        if conn: conn.close() # Close connection if opened

# --- Review Management Handlers ---
async def handle_adm_manage_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays reviews paginated for the admin with delete options."""
    query = update.callback_query
    user_id = query.from_user.id
    # Viewer Admins are also allowed to view reviews, primary admin can delete
    # is_primary_admin = (user_id == ADMIN_ID) # Not needed for this function directly, handled by button visibility in menu
    # is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS) # Not needed for this function directly
    # If this handler is called, assume permission was granted by the menu leading here.

    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])
    reviews_per_page = 5 # Using a local constant, can be moved to global if shared
    reviews_data = fetch_reviews(offset=offset, limit=reviews_per_page + 1) # Sync function uses helper
    msg = "🚫 Manage Reviews\n\n"
    keyboard = []
    item_buttons = []
    if not reviews_data:
        if offset == 0: msg += "No reviews have been left yet."
        else: msg += "No more reviews to display."
    else:
        has_more = len(reviews_data) > reviews_per_page
        reviews_to_show = reviews_data[:reviews_per_page]
        for review in reviews_to_show:
            review_id = review.get('review_id', 'N/A')
            try:
                date_str = review.get('review_date', '')
                formatted_date = "???"
                if date_str:
                    try: formatted_date = datetime.fromisoformat(date_str.replace('Z','+00:00')).strftime("%Y-%m-%d") # Handle Z for UTC
                    except ValueError: pass
                username = review.get('username', 'anonymous')
                username_display = f"@{username}" if username and username != 'anonymous' else username
                review_text = review.get('review_text', '')
                review_text_preview = review_text[:100] + ('...' if len(review_text) > 100 else '')
                msg += f"ID {review_id} | {username_display} ({formatted_date}):\n{review_text_preview}\n\n"
                if user_id == ADMIN_ID: # Only primary admin can delete
                     item_buttons.append([InlineKeyboardButton(f"🗑️ Delete Review #{review_id}", callback_data=f"adm_delete_review_confirm|{review_id}")])
            except Exception as e:
                 logger.error(f"Error formatting review item #{review_id} for admin view: {review}, Error: {e}")
                 msg += f"ID {review_id} | (Error displaying review)\n\n"
                 if user_id == ADMIN_ID: item_buttons.append([InlineKeyboardButton(f"🗑️ Delete Review #{review_id}", callback_data=f"adm_delete_review_confirm|{review_id}")])
        keyboard.extend(item_buttons)
        nav_buttons = []
        if offset > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_manage_reviews|{max(0, offset - reviews_per_page)}"))
        if has_more: nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"adm_manage_reviews|{offset + reviews_per_page}"))
        if nav_buttons: keyboard.append(nav_buttons)

    # Determine correct back button (this function is called from both admin menus)
    # The actual permission check for deletion is in handle_confirm_yes and handle_adm_delete_review_confirm
    back_callback_data = "admin_menu" # Default to primary admin menu
    if hasattr(context, '_chat_id') and context._chat_id is not None: # Check if context has chat_id
        from viewer_admin import SECONDARY_ADMIN_IDS as VIEWER_SECONDARY_ADMIN_IDS # Local import to avoid circular
        if query.from_user.id in VIEWER_SECONDARY_ADMIN_IDS and query.from_user.id != ADMIN_ID:
            back_callback_data = "viewer_admin_menu" # If secondary admin, point to their menu

    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data=back_callback_data)])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Failed to edit message for adm_manage_reviews: {e}"); await query.answer("Error updating review list.", show_alert=True)
        else:
            await query.answer() # Acknowledge if not modified
    except Exception as e:
        logger.error(f"Unexpected error in adm_manage_reviews: {e}", exc_info=True)
        await query.edit_message_text("❌ An unexpected error occurred while loading reviews.", parse_mode=None)


async def handle_adm_delete_review_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Review' button press, shows confirmation."""
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True) # Only primary admin
    if not params: return await query.answer("Error: Review ID missing.", show_alert=True)
    try: review_id = int(params[0])
    except ValueError: return await query.answer("Error: Invalid Review ID.", show_alert=True)
    review_text_snippet = "N/A"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        # Use column name
        c.execute("SELECT review_text FROM reviews WHERE review_id = ?", (review_id,))
        result = c.fetchone()
        if result: review_text_snippet = result['review_text'][:100]
        else:
            await query.answer("Review not found.", show_alert=True)
            try: await query.edit_message_text("Error: Review not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Reviews", callback_data="adm_manage_reviews|0")]]), parse_mode=None)
            except telegram_error.BadRequest: pass
            return
    except sqlite3.Error as e: logger.warning(f"Could not fetch review text for confirmation (ID {review_id}): {e}")
    finally:
        if conn: conn.close() # Close connection if opened
    context.user_data["confirm_action"] = f"delete_review|{review_id}"
    msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to permanently delete review ID {review_id}?\n\n"
           f"Preview: {review_text_snippet}{'...' if len(review_text_snippet) >= 100 else ''}\n\n"
           f"🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Review", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_reviews|0")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Broadcast Handlers ---
async def handle_adm_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the broadcast message process by asking for the target audience."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)

    lang, lang_data = _get_lang_data(context) # Use helper

    # Clear previous broadcast data
    context.user_data.pop('broadcast_content', None)
    context.user_data.pop('broadcast_target_type', None)
    context.user_data.pop('broadcast_target_value', None)

    prompt_msg = lang_data.get("broadcast_select_target", "📢 Broadcast Message\n\nSelect the target audience:")
    keyboard = [
        [InlineKeyboardButton(lang_data.get("broadcast_target_all", "👥 All Users"), callback_data="adm_broadcast_target_type|all")],
        [InlineKeyboardButton(lang_data.get("broadcast_target_city", "🏙️ By Last Purchased City"), callback_data="adm_broadcast_target_type|city")],
        [InlineKeyboardButton(lang_data.get("broadcast_target_status", "👑 By User Status"), callback_data="adm_broadcast_target_type|status")],
        [InlineKeyboardButton(lang_data.get("broadcast_target_inactive", "⏳ By Inactivity (Days)"), callback_data="adm_broadcast_target_type|inactive")],
        [InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]
    ]
    await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()


async def handle_adm_broadcast_target_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the selection of the broadcast target type."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Target type missing.", show_alert=True)

    target_type = params[0]
    context.user_data['broadcast_target_type'] = target_type
    lang, lang_data = _get_lang_data(context) # Use helper

    if target_type == 'all':
        context.user_data['state'] = 'awaiting_broadcast_message'
        ask_msg_text = lang_data.get("broadcast_ask_message", "📝 Now send the message content (text, photo, video, or GIF with caption):")
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        await query.edit_message_text(ask_msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer("Send the message content.")

    elif target_type == 'city':
        load_all_data()
        if not CITIES:
             await query.edit_message_text("No cities configured. Cannot target by city.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_broadcast_start")]]), parse_mode=None)
             return
        sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
        keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_broadcast_target_city|{CITIES.get(c,'N/A')}")] for c in sorted_city_ids if CITIES.get(c)]
        keyboard.append([InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")])
        select_city_text = lang_data.get("broadcast_select_city_target", "🏙️ Select City to Target\n\nUsers whose last purchase was in:")
        await query.edit_message_text(select_city_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer()

    elif target_type == 'status':
        select_status_text = lang_data.get("broadcast_select_status_target", "👑 Select Status to Target:")
        vip_label = lang_data.get("broadcast_status_vip", "VIP 👑")
        regular_label = lang_data.get("broadcast_status_regular", "Regular ⭐")
        new_label = lang_data.get("broadcast_status_new", "New 🌱")
        keyboard = [
            [InlineKeyboardButton(vip_label, callback_data=f"adm_broadcast_target_status|{vip_label}")],
            [InlineKeyboardButton(regular_label, callback_data=f"adm_broadcast_target_status|{regular_label}")],
            [InlineKeyboardButton(new_label, callback_data=f"adm_broadcast_target_status|{new_label}")],
            [InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]
        ]
        await query.edit_message_text(select_status_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer()

    elif target_type == 'inactive':
        context.user_data['state'] = 'awaiting_broadcast_inactive_days'
        inactive_prompt = lang_data.get("broadcast_enter_inactive_days", "⏳ Enter Inactivity Period\n\nPlease reply with the number of days since the user's last purchase (or since registration if no purchases). Users inactive for this many days or more will receive the message.")
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        await query.edit_message_text(inactive_prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer("Enter number of days.")

    else:
        await query.answer("Unknown target type selected.", show_alert=True)
        await handle_adm_broadcast_start(update, context)


async def handle_adm_broadcast_target_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles selecting the city for targeted broadcast."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: City name missing.", show_alert=True)

    city_name = params[0]
    context.user_data['broadcast_target_value'] = city_name
    lang, lang_data = _get_lang_data(context) # Use helper

    context.user_data['state'] = 'awaiting_broadcast_message'
    ask_msg_text = lang_data.get("broadcast_ask_message", "📝 Now send the message content (text, photo, video, or GIF with caption):")
    keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
    await query.edit_message_text(f"Targeting users last purchased in: {city_name}\n\n{ask_msg_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send the message content.")

async def handle_adm_broadcast_target_status(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles selecting the status for targeted broadcast."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Status value missing.", show_alert=True)

    status_value = params[0]
    context.user_data['broadcast_target_value'] = status_value
    lang, lang_data = _get_lang_data(context) # Use helper

    context.user_data['state'] = 'awaiting_broadcast_message'
    ask_msg_text = lang_data.get("broadcast_ask_message", "📝 Now send the message content (text, photo, video, or GIF with caption):")
    keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
    await query.edit_message_text(f"Targeting users with status: {status_value}\n\n{ask_msg_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send the message content.")


async def handle_confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the 'Yes' confirmation for the broadcast."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)

    broadcast_content = context.user_data.get('broadcast_content')
    if not broadcast_content:
        logger.error("Broadcast content not found during confirmation.")
        return await query.edit_message_text("❌ Error: Broadcast content not found. Please start again.", parse_mode=None)

    text = broadcast_content.get('text')
    media_file_id = broadcast_content.get('media_file_id')
    media_type = broadcast_content.get('media_type')
    target_type = broadcast_content.get('target_type', 'all')
    target_value = broadcast_content.get('target_value')
    admin_chat_id = query.message.chat_id

    try:
        await query.edit_message_text("⏳ Broadcast initiated. Fetching users and sending messages...", parse_mode=None)
    except telegram_error.BadRequest: await query.answer()

    context.user_data.pop('broadcast_target_type', None)
    context.user_data.pop('broadcast_target_value', None)
    context.user_data.pop('broadcast_content', None)

    asyncio.create_task(send_broadcast(context, text, media_file_id, media_type, target_type, target_value, admin_chat_id))


async def handle_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Cancels the broadcast process."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)

    context.user_data.pop('state', None)
    context.user_data.pop('broadcast_content', None)
    context.user_data.pop('broadcast_target_type', None)
    context.user_data.pop('broadcast_target_value', None)

    try:
        await query.edit_message_text("❌ Broadcast cancelled.", parse_mode=None)
    except telegram_error.BadRequest: await query.answer()

    keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]
    await send_message_with_retry(context.bot, query.message.chat_id, "Returning to Admin Menu.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def send_broadcast(context: ContextTypes.DEFAULT_TYPE, text: str, media_file_id: str | None, media_type: str | None, target_type: str, target_value: str | int | None, admin_chat_id: int):
    """Sends the broadcast message to the target audience."""
    bot = context.bot
    lang_data = LANGUAGES.get('en', {}) # Use English for internal messages

    user_ids = await asyncio.to_thread(fetch_user_ids_for_broadcast, target_type, target_value)

    if not user_ids:
        logger.warning(f"No users found for broadcast target: type={target_type}, value={target_value}")
        no_users_msg = lang_data.get("broadcast_no_users_found_target", "⚠️ Broadcast Warning: No users found matching the target criteria.")
        await send_message_with_retry(bot, admin_chat_id, no_users_msg, parse_mode=None)
        return

    success_count, fail_count, block_count, total_users = 0, 0, 0, len(user_ids)
    logger.info(f"Starting broadcast to {total_users} users (Target: {target_type}={target_value})...")

    status_message = None
    status_update_interval = max(10, total_users // 20)

    try:
        status_message = await send_message_with_retry(bot, admin_chat_id, f"⏳ Broadcasting... (0/{total_users})", parse_mode=None)

        for i, user_id in enumerate(user_ids):
            try:
                send_kwargs = {'chat_id': user_id, 'caption': text, 'parse_mode': None}
                if media_file_id and media_type == "photo": await bot.send_photo(photo=media_file_id, **send_kwargs)
                elif media_file_id and media_type == "video": await bot.send_video(video=media_file_id, **send_kwargs)
                elif media_file_id and media_type == "gif": await bot.send_animation(animation=media_file_id, **send_kwargs)
                else: await bot.send_message(chat_id=user_id, text=text, parse_mode=None, disable_web_page_preview=True)
                success_count += 1
            except telegram_error.BadRequest as e:
                 error_str = str(e).lower()
                 if "chat not found" in error_str or "user is deactivated" in error_str or "bot was blocked" in error_str:
                      logger.warning(f"Broadcast fail/block for user {user_id}: {e}")
                      fail_count += 1; block_count += 1
                 else: logger.error(f"Broadcast BadRequest for {user_id}: {e}"); fail_count += 1
            except telegram_error.Unauthorized: logger.info(f"Broadcast skipped for {user_id}: Bot blocked."); fail_count += 1; block_count += 1
            except telegram_error.RetryAfter as e:
                 retry_seconds = e.retry_after + 1
                 logger.warning(f"Rate limit hit during broadcast. Sleeping {retry_seconds}s.")
                 if retry_seconds > 300: logger.error(f"RetryAfter > 5 min. Aborting for {user_id}."); fail_count += 1; continue
                 await asyncio.sleep(retry_seconds)
                 try: # Retry send after sleep
                     send_kwargs = {'chat_id': user_id, 'caption': text, 'parse_mode': None}
                     if media_file_id and media_type == "photo": await bot.send_photo(photo=media_file_id, **send_kwargs)
                     elif media_file_id and media_type == "video": await bot.send_video(video=media_file_id, **send_kwargs)
                     elif media_file_id and media_type == "gif": await bot.send_animation(animation=media_file_id, **send_kwargs)
                     else: await bot.send_message(chat_id=user_id, text=text, parse_mode=None, disable_web_page_preview=True)
                     success_count += 1
                 except Exception as retry_e: logger.error(f"Broadcast fail after retry for {user_id}: {retry_e}"); fail_count += 1;
                 if isinstance(retry_e, (telegram_error.Unauthorized, telegram_error.BadRequest)): block_count +=1 # Count as blocked if retry fails with these
            except Exception as e: logger.error(f"Broadcast fail (Unexpected) for {user_id}: {e}", exc_info=True); fail_count += 1

            await asyncio.sleep(0.05) # ~20 messages per second limit

            if status_message and (i + 1) % status_update_interval == 0:
                 try:
                     await context.bot.edit_message_text(
                         chat_id=admin_chat_id,
                         message_id=status_message.message_id,
                         text=f"⏳ Broadcasting... ({i+1}/{total_users} | ✅{success_count} | ❌{fail_count})",
                         parse_mode=None
                     )
                 except telegram_error.BadRequest: pass # Ignore if message is not modified
                 except Exception as edit_e: logger.warning(f"Could not edit broadcast status message: {edit_e}")

    finally:
         # Final summary message
         summary_msg = (f"✅ Broadcast Complete\n\nTarget: {target_type} = {target_value or 'N/A'}\n"
                        f"Sent to: {success_count}/{total_users}\n"
                        f"Failed: {fail_count}\n(Blocked/Deactivated: {block_count})")
         if status_message:
             try: await context.bot.edit_message_text(chat_id=admin_chat_id, message_id=status_message.message_id, text=summary_msg, parse_mode=None)
             except Exception: await send_message_with_retry(bot, admin_chat_id, summary_msg, parse_mode=None)
         else: await send_message_with_retry(bot, admin_chat_id, summary_msg, parse_mode=None)
         logger.info(f"Broadcast finished. Target: {target_type}={target_value}. Success: {success_count}, Failed: {fail_count}, Blocked: {block_count}")

# --- Message Handler for Broadcast Inactive Days ---
async def handle_adm_broadcast_inactive_days_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the number of days for inactive broadcast."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_broadcast_inactive_days': return
    if not update.message or not update.message.text: return

    lang, lang_data = _get_lang_data(context) # Use helper
    invalid_days_msg = lang_data.get("broadcast_invalid_days", "❌ Invalid number of days. Please enter a positive whole number.")
    days_too_large_msg = lang_data.get("broadcast_days_too_large", "❌ Number of days is too large. Please enter a smaller number.")

    try:
        days = int(update.message.text.strip())
        if days <= 0:
            await send_message_with_retry(context.bot, chat_id, invalid_days_msg, parse_mode=None)
            return # Keep state
        if days > 365 * 5: # Arbitrary limit to prevent nonsense
            await send_message_with_retry(context.bot, chat_id, days_too_large_msg, parse_mode=None)
            return # Keep state

        context.user_data['broadcast_target_value'] = days
        context.user_data['state'] = 'awaiting_broadcast_message' # Change state

        ask_msg_text = lang_data.get("broadcast_ask_message", "📝 Now send the message content (text, photo, video, or GIF with caption):")
        keyboard = [[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]]
        await send_message_with_retry(context.bot, chat_id, f"Targeting users inactive for >= {days} days.\n\n{ask_msg_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except ValueError:
        await send_message_with_retry(context.bot, chat_id, invalid_days_msg, parse_mode=None)
        return # Keep state

# --- Message Handler for Broadcast Content ---
async def handle_adm_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles receiving the message content for the broadcast, AFTER target is set."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_broadcast_message': return
    if not update.message: return

    lang, lang_data = _get_lang_data(context) # Use helper

    text = (update.message.text or update.message.caption or "").strip()
    media_file_id, media_type = None, None
    if update.message.photo: media_file_id, media_type = update.message.photo[-1].file_id, "photo"
    elif update.message.video: media_file_id, media_type = update.message.video.file_id, "video"
    elif update.message.animation: media_file_id, media_type = update.message.animation.file_id, "gif"

    if not text and not media_file_id:
        await send_message_with_retry(context.bot, chat_id, "Broadcast message cannot be empty. Please send text or media.", parse_mode=None)
        return

    target_type = context.user_data.get('broadcast_target_type', 'all')
    target_value = context.user_data.get('broadcast_target_value')

    context.user_data['broadcast_content'] = {
        'text': text, 'media_file_id': media_file_id, 'media_type': media_type,
        'target_type': target_type, 'target_value': target_value
    }
    context.user_data.pop('state', None)

    confirm_title = lang_data.get("broadcast_confirm_title", "📢 Confirm Broadcast")
    target_desc = lang_data.get("broadcast_confirm_target_all", "Target: All Users")
    if target_type == 'city': target_desc = lang_data.get("broadcast_confirm_target_city", "Target: Last Purchase in {city}").format(city=target_value)
    elif target_type == 'status': target_desc = lang_data.get("broadcast_confirm_target_status", "Target: Status - {status}").format(status=target_value)
    elif target_type == 'inactive': target_desc = lang_data.get("broadcast_confirm_target_inactive", "Target: Inactive >= {days} days").format(days=target_value)

    preview_label = lang_data.get("broadcast_confirm_preview", "Preview:")
    preview_msg = f"{confirm_title}\n\n{target_desc}\n\n{preview_label}\n"
    if media_file_id: preview_msg += f"{media_type.capitalize()} attached\n"
    text_preview = text[:500] + ('...' if len(text) > 500 else '')
    preview_msg += text_preview if text else "(No text)"
    preview_msg += f"\n\n{lang_data.get('broadcast_confirm_ask', 'Send this message?')}"

    keyboard = [
        [InlineKeyboardButton("✅ Yes, Send Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_broadcast")]
    ]
    await send_message_with_retry(context.bot, chat_id, preview_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# --- Welcome Message Management Handlers --- START
async def handle_adm_manage_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the paginated menu for managing welcome message templates."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        return await query.answer("Access Denied.", show_alert=True)

    lang, lang_data = _get_lang_data(context) # Use helper
    offset = 0
    if params and len(params) > 0 and params[0].isdigit():
        offset = int(params[0])

    # Fetch templates and active template name
    templates = get_welcome_message_templates(limit=TEMPLATES_PER_PAGE, offset=offset)
    total_templates = get_welcome_message_template_count()
    conn = None
    active_template_name = "default" # Default fallback
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use column name
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", ("active_welcome_message_name",))
        setting_row = c.fetchone()
        if setting_row and setting_row['setting_value']: # Check if value is not None/empty
            active_template_name = setting_row['setting_value'] # Use column name
    except sqlite3.Error as e:
        logger.error(f"DB error fetching active welcome template name: {e}")
    finally:
        if conn: conn.close()

    # Build message and keyboard
    title = lang_data.get("manage_welcome_title", "⚙️ Manage Welcome Messages")
    prompt = lang_data.get("manage_welcome_prompt", "Select a template to manage or activate:")
    msg_parts = [f"{title}\n\n{prompt}\n"] # Use list to build message
    keyboard = []

    if not templates and offset == 0:
        msg_parts.append("\nNo custom templates found. Add one?")
    else:
        for template in templates:
            name = template['name']
            # <<< FIX: Escape name and description >>>
            safe_name = helpers.escape_markdown(name, version=2)
            desc = template.get('description') or "No description"
            safe_desc = helpers.escape_markdown(desc, version=2)

            is_active = (name == active_template_name)
            # <<< FIX: Escape the parentheses in the active indicator >>>
            active_indicator_raw = lang_data.get("welcome_template_active", " (Active ✅)") if is_active else lang_data.get("welcome_template_inactive", "")
            active_indicator = active_indicator_raw.replace("(", "\\(").replace(")", "\\)") # Manually escape parentheses for MDv2


            # Display Name, Description, and Active Status
            msg_parts.append(f"\n📄 *{safe_name}*{active_indicator}\n_{safe_desc}_\n") # Removed extra newline

            # Buttons: Edit | Activate (if not active) | Delete (if not default and not active)
            row = [InlineKeyboardButton(lang_data.get("welcome_button_edit", "✏️ Edit"), callback_data=f"adm_edit_welcome|{name}|{offset}")]
            if not is_active:
                 row.append(InlineKeyboardButton(lang_data.get("welcome_button_activate", "✅ Activate"), callback_data=f"adm_activate_welcome|{name}|{offset}"))

            can_delete = not (name == "default") and not is_active # Cannot delete default or active
            if can_delete:
                 row.append(InlineKeyboardButton(lang_data.get("welcome_button_delete", "🗑️ Delete"), callback_data=f"adm_delete_welcome_confirm|{name}|{offset}"))
            keyboard.append(row)

        # Pagination
        total_pages = math.ceil(total_templates / TEMPLATES_PER_PAGE)
        current_page = (offset // TEMPLATES_PER_PAGE) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_manage_welcome|{max(0, offset - TEMPLATES_PER_PAGE)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"adm_manage_welcome|{offset + TEMPLATES_PER_PAGE}"))
        if nav_buttons: keyboard.append(nav_buttons)
        if total_pages > 1:
            # Escape page number indicator too
            page_indicator = f"Page {current_page}/{total_pages}"
            escaped_page_indicator = helpers.escape_markdown(page_indicator, version=2)
            msg_parts.append(f"\n{escaped_page_indicator}")


    # Add "Add New" and "Reset Default" buttons
    keyboard.append([InlineKeyboardButton(lang_data.get("welcome_button_add_new", "➕ Add New Template"), callback_data="adm_add_welcome_start")])
    keyboard.append([InlineKeyboardButton(lang_data.get("welcome_button_reset_default", "🔄 Reset to Built-in Default"), callback_data="adm_reset_default_confirm")]) # <<< Added Reset Button
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])

    final_msg = "".join(msg_parts)

    # Send/Edit message
    try:
        # Try sending with Markdown V2
        await query.edit_message_text(final_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing welcome management menu (Markdown V2): {e}. Message: {final_msg[:500]}...") # Log snippet
            # Fallback to plain text
            plain_msg_fallback = final_msg
            for char in ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
                plain_msg_fallback = plain_msg_fallback.replace(f'\\{char}', char) # Remove escapes first
            for char in ['*', '_', '`']: # Remove common markdown chars
                plain_msg_fallback = plain_msg_fallback.replace(char, '')

            try:
                await query.edit_message_text(plain_msg_fallback, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
                logger.info("Sent welcome management menu with plain text fallback due to Markdown V2 error.")
            except Exception as fallback_e:
                logger.error(f"Error editing welcome management menu (Fallback): {fallback_e}")
                await query.answer("Error displaying menu.", show_alert=True)
        else:
             await query.answer() # Acknowledge if not modified
    except Exception as e:
        logger.error(f"Unexpected error in handle_adm_manage_welcome: {e}", exc_info=True)
        await query.answer("An error occurred displaying the menu.", show_alert=True)

async def handle_adm_activate_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Activates the selected welcome message template."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[1].isdigit():
        return await query.answer("Error: Template name or offset missing.", show_alert=True)

    template_name = params[0]
    offset = int(params[1])
    lang, lang_data = _get_lang_data(context) # Use helper

    success = set_active_welcome_message(template_name) # Use helper from utils
    if success:
        msg_template = lang_data.get("welcome_activate_success", "✅ Template '{name}' activated.")
        await query.answer(msg_template.format(name=template_name))
        await handle_adm_manage_welcome(update, context, params=[str(offset)]) # Refresh menu at same page
    else:
        msg_template = lang_data.get("welcome_activate_fail", "❌ Failed to activate template '{name}'.")
        await query.answer(msg_template.format(name=template_name), show_alert=True)

async def handle_adm_add_welcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the process of adding a new welcome template (gets name)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    lang, lang_data = _get_lang_data(context) # Use helper

    context.user_data['state'] = 'awaiting_welcome_template_name'
    prompt = lang_data.get("welcome_add_name_prompt", "Enter a unique short name for the new template (e.g., 'default', 'promo_weekend'):")
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_welcome|0")]] # Go back to first page
    await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter template name in chat.")


async def handle_adm_edit_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Shows options for editing an existing welcome template (text or description)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[1].isdigit():
        return await query.answer("Error: Template name or offset missing.", show_alert=True)

    template_name = params[0]
    offset = int(params[1])
    lang, lang_data = _get_lang_data(context) # Use helper

    # Fetch current text and description
    current_text = ""
    current_description = ""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT template_text, description FROM welcome_messages WHERE name = ?", (template_name,))
        row = c.fetchone()
        if not row:
             await query.answer("Template not found.", show_alert=True)
             return await handle_adm_manage_welcome(update, context, params=[str(offset)])
        current_text = row['template_text']
        current_description = row['description'] or ""
    except sqlite3.Error as e:
        logger.error(f"DB error fetching template '{template_name}' for edit options: {e}")
        await query.answer("Error fetching template details.", show_alert=True)
        return await handle_adm_manage_welcome(update, context, params=[str(offset)])
    finally:
        if conn: conn.close()

    # Store info needed for potential edits
    context.user_data['editing_welcome_template_name'] = template_name
    context.user_data['editing_welcome_offset'] = offset

    # Display using plain text
    safe_name = template_name
    safe_desc = current_description or 'Not set'

    msg = f"✏️ Editing Template: {safe_name}\n\n"
    msg += f"📝 Description: {safe_desc}\n\n"
    msg += "Choose what to edit:"

    keyboard = [
        [InlineKeyboardButton(lang_data.get("welcome_button_edit_text","Edit Text"), callback_data=f"adm_edit_welcome_text|{template_name}")],
        [InlineKeyboardButton(lang_data.get("welcome_button_edit_desc","Edit Description"), callback_data=f"adm_edit_welcome_desc|{template_name}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"adm_manage_welcome|{offset}")]
    ]
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing edit welcome menu: {e}. Message: {msg}")
        else: await query.answer() # Acknowledge if not modified
    except Exception as e:
        logger.error(f"Unexpected error in handle_adm_edit_welcome: {e}")
        await query.answer("Error displaying edit menu.", show_alert=True)

async def handle_adm_edit_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Initiates editing the template text."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Template name missing.", show_alert=True)

    template_name = params[0]
    offset = context.user_data.get('editing_welcome_offset', 0) # Get offset from context
    lang, lang_data = _get_lang_data(context) # Use helper

    # Fetch current text to show in prompt
    current_text = ""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT template_text FROM welcome_messages WHERE name = ?", (template_name,))
        row = c.fetchone()
        if row: current_text = row['template_text']
    except sqlite3.Error as e: logger.error(f"DB error fetching text for edit: {e}")
    finally:
         if conn: conn.close()

    context.user_data['state'] = 'awaiting_welcome_template_edit' # Reusing state, but specifically for text
    context.user_data['editing_welcome_template_name'] = template_name # Ensure it's set
    context.user_data['editing_welcome_field'] = 'text' # Indicate we are editing text

    placeholders = "{username}, {status}, {progress_bar}, {balance_str}, {purchases}, {basket_count}" # Plain text placeholders
    prompt_template = lang_data.get("welcome_edit_text_prompt", "Editing Text for '{name}'. Current text:\n\n{current_text}\n\nPlease reply with the new text. Available placeholders:\n{placeholders}")
    # Display plain text
    prompt = prompt_template.format(
        name=template_name,
        current_text=current_text,
        placeholders=placeholders
    )
    if len(prompt) > 4000: prompt = prompt[:4000] + "\n[... Current text truncated ...]"

    # Go back to the specific template's edit menu
    keyboard = [[InlineKeyboardButton("❌ Cancel Edit", callback_data=f"adm_edit_welcome|{template_name}|{offset}")]]
    try:
        await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing edit text prompt: {e}")
        else: await query.answer()
    await query.answer("Enter new template text.")

async def handle_adm_edit_welcome_desc(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Initiates editing the template description."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Template name missing.", show_alert=True)

    template_name = params[0]
    offset = context.user_data.get('editing_welcome_offset', 0)
    lang, lang_data = _get_lang_data(context) # Use helper

    # Fetch current description
    current_desc = ""
    conn = None
    try:
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT description FROM welcome_messages WHERE name = ?", (template_name,))
        row = c.fetchone(); current_desc = row['description'] or ""
    except sqlite3.Error as e: logger.error(f"DB error fetching desc for edit: {e}")
    finally:
        if conn: conn.close()

    context.user_data['state'] = 'awaiting_welcome_description_edit' # New state for description edit
    context.user_data['editing_welcome_template_name'] = template_name # Ensure it's set
    context.user_data['editing_welcome_field'] = 'description' # Indicate we are editing description

    prompt_template = lang_data.get("welcome_edit_description_prompt", "Editing description for '{name}'. Current: '{current_desc}'.\n\nEnter new description or send '-' to skip.")
    prompt = prompt_template.format(name=template_name, current_desc=current_desc or "Not set")

    keyboard = [[InlineKeyboardButton("❌ Cancel Edit", callback_data=f"adm_edit_welcome|{template_name}|{offset}")]]
    await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new description.")

async def handle_adm_delete_welcome_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Confirms deletion of a welcome message template."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[1].isdigit():
         return await query.answer("Error: Template name or offset missing.", show_alert=True)

    template_name = params[0]
    offset = int(params[1])
    lang, lang_data = _get_lang_data(context) # Use helper

    # Fetch current active template
    conn = None
    active_template_name = "default"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", ("active_welcome_message_name",))
        row = c.fetchone(); active_template_name = row['setting_value'] if row else "default" # Use column name
    except sqlite3.Error as e: logger.error(f"DB error checking template status for delete: {e}")
    finally:
         if conn: conn.close()

    if template_name == "default":
        await query.answer("Cannot delete the 'default' template.", show_alert=True)
        return await handle_adm_manage_welcome(update, context, params=[str(offset)])

    # <<< Improvement: Prevent deleting the active template >>>
    if template_name == active_template_name:
        cannot_delete_msg = lang_data.get("welcome_cannot_delete_active", "❌ Cannot delete the active template. Activate another first.")
        await query.answer(cannot_delete_msg, show_alert=True)
        return await handle_adm_manage_welcome(update, context, params=[str(offset)]) # Refresh list

    context.user_data["confirm_action"] = f"delete_welcome_template|{template_name}"
    title = lang_data.get("welcome_delete_confirm_title", "⚠️ Confirm Deletion")
    text_template = lang_data.get("welcome_delete_confirm_text", "Are you sure you want to delete the welcome message template named '{name}'?")
    msg = f"{title}\n\n{text_template.format(name=template_name)}"

    keyboard = [
        [InlineKeyboardButton(lang_data.get("welcome_delete_button_yes", "✅ Yes, Delete Template"), callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_manage_welcome|{offset}")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# <<< Reset Default Welcome Handler >>>
async def handle_reset_default_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Confirms resetting the 'default' template to the built-in text and activating it."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    lang, lang_data = _get_lang_data(context)

    context.user_data["confirm_action"] = "reset_default_welcome"
    title = lang_data.get("welcome_reset_confirm_title", "⚠️ Confirm Reset")
    text = lang_data.get("welcome_reset_confirm_text", "Are you sure you want to reset the text of the 'default' template to the built-in version and activate it?")
    msg = f"{title}\n\n{text}"

    keyboard = [
        [InlineKeyboardButton(lang_data.get("welcome_reset_button_yes", "✅ Yes, Reset & Activate"), callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_welcome|0")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Welcome Message Management Handlers --- END


# --- Welcome Message Preview & Save Handlers --- START

async def _show_welcome_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a preview of the welcome message with dummy data."""
    query = update.callback_query # Could be None if called from message handler
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lang, lang_data = _get_lang_data(context)

    pending_template = context.user_data.get("pending_welcome_template")
    if not pending_template or not pending_template.get("name"): # Need at least name
        logger.error("Attempted to show welcome preview, but pending data missing.")
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Preview data lost.", parse_mode=None)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_welcome_template", None)
        # Attempt to go back to the management menu
        if query:
             await handle_adm_manage_welcome(update, context, params=["0"])
        return

    template_name = pending_template['name']
    template_text = pending_template.get('text', '') # Use get with fallback
    template_description = pending_template.get('description', 'Not set')
    is_editing = pending_template.get('is_editing', False)
    offset = pending_template.get('offset', 0)

    # Dummy data for formatting
    dummy_username = update.effective_user.first_name or "Admin"
    dummy_status = "VIP 👑"
    dummy_progress = get_progress_bar(10)
    dummy_balance = format_currency(123.45)
    dummy_purchases = 15
    dummy_basket = 2
    preview_text_raw = "_(Formatting Error)_" # Fallback preview

    try:
        # Format using the raw username and placeholders
        preview_text_raw = template_text.format(
            username=dummy_username,
            status=dummy_status,
            progress_bar=dummy_progress,
            balance_str=dummy_balance,
            purchases=dummy_purchases,
            basket_count=dummy_basket
        ) # Keep internal markdown

    except KeyError as e:
        logger.warning(f"KeyError formatting welcome preview for '{template_name}': {e}")
        err_msg_template = lang_data.get("welcome_invalid_placeholder", "⚠️ Formatting Error! Missing placeholder: `{key}`\n\nRaw Text:\n{text}")
        preview_text_raw = err_msg_template.format(key=e, text=template_text[:500]) # Show raw text in case of error
    except Exception as format_e:
        logger.error(f"Unexpected error formatting preview: {format_e}")
        err_msg_template = lang_data.get("welcome_formatting_error", "⚠️ Unexpected Formatting Error!\n\nRaw Text:\n{text}")
        preview_text_raw = err_msg_template.format(text=template_text[:500])

    # Prepare display message (plain text)
    title = lang_data.get("welcome_preview_title", "--- Welcome Message Preview ---")
    name_label = lang_data.get("welcome_preview_name", "Name")
    desc_label = lang_data.get("welcome_preview_desc", "Desc")
    confirm_prompt = lang_data.get("welcome_preview_confirm", "Save this template?")

    msg = f"{title}\n\n"
    msg += f"{name_label}: {template_name}\n"
    msg += f"{desc_label}: {template_description or 'Not set'}\n"
    msg += f"---\n"
    msg += f"{preview_text_raw}\n" # Display the formatted (and potentially error) message raw
    msg += f"---\n"
    msg += f"\n{confirm_prompt}"

    # Set state for confirmation callback
    context.user_data['state'] = 'awaiting_welcome_confirmation'

    # Go back to the specific template edit menu if editing, or manage menu if adding
    cancel_callback = f"adm_edit_welcome|{template_name}|{offset}" if is_editing else f"adm_manage_welcome|{offset}"

    keyboard = [
        [InlineKeyboardButton(lang_data.get("welcome_button_save", "💾 Save Template"), callback_data=f"confirm_save_welcome")],
        [InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)]
    ]

    # Send or edit the message (using plain text)
    message_to_edit = query.message if query else None
    if message_to_edit:
        try:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower():
                 logger.error(f"Error editing preview message: {e}")
                 # Send as new message if edit fails
                 await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
             else: await query.answer() # Ignore modification error
    else:
        # Send as new message if no original message to edit
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    if query:
        await query.answer()

# <<< NEW >>>
async def handle_confirm_save_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the 'Save Template' button after preview."""
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if context.user_data.get("state") != 'awaiting_welcome_confirmation':
        logger.warning("handle_confirm_save_welcome called in wrong state.")
        return await query.answer("Invalid state.", show_alert=True)

    pending_template = context.user_data.get("pending_welcome_template")
    if not pending_template or not pending_template.get("name") or pending_template.get("text") is None: # Text can be empty, but key must exist
        logger.error("Attempted to save welcome template, but pending data missing.")
        await query.edit_message_text("❌ Error: Save data lost. Please start again.", parse_mode=None)
        context.user_data.pop("state", None)
        context.user_data.pop("pending_welcome_template", None)
        return

    template_name = pending_template['name']
    template_text = pending_template['text']
    template_description = pending_template.get('description') # Can be None
    is_editing = pending_template.get('is_editing', False)
    offset = pending_template.get('offset', 0)
    lang, lang_data = _get_lang_data(context) # Use helper

    # Perform the actual save operation
    success = False
    if is_editing:
        success = update_welcome_message_template(template_name, template_text, template_description)
        msg_template = lang_data.get("welcome_edit_success", "✅ Template '{name}' updated.") if success else lang_data.get("welcome_edit_fail", "❌ Failed to update template '{name}'.")
    else:
        success = add_welcome_message_template(template_name, template_text, template_description)
        msg_template = lang_data.get("welcome_add_success", "✅ Welcome message template '{name}' added.") if success else lang_data.get("welcome_add_fail", "❌ Failed to add welcome message template.")

    # Clean up context
    context.user_data.pop("state", None)
    context.user_data.pop("pending_welcome_template", None)

    await query.edit_message_text(msg_template.format(name=template_name), parse_mode=None)

    # Go back to the management list
    await handle_adm_manage_welcome(update, context, params=[str(offset)])


# --- Welcome Message Management Handlers --- END

# --- Message Handlers for Welcome Message Management ---

async def handle_adm_welcome_template_name_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin entering the name for a new welcome template."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID or context.user_data.get("state") != 'awaiting_welcome_template_name': return
    if not update.message or not update.message.text: return

    template_name = update.message.text.strip()
    lang, lang_data = _get_lang_data(context) # Use helper

    if not template_name or len(template_name) > 50 or '|' in template_name:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid name. Please use a short, unique name without '|' (max 50 chars).")
        return # Keep state

    # Check if name exists
    templates = get_welcome_message_templates()
    if any(t['name'] == template_name for t in templates):
        exists_msg = lang_data.get("welcome_add_name_exists", "❌ Error: A template with the name '{name}' already exists.")
        await send_message_with_retry(context.bot, chat_id, exists_msg.format(name=template_name))
        return # Keep state

    # Store name and ask for text
    context.user_data['pending_welcome_template'] = {'name': template_name, 'is_editing': False}
    context.user_data['state'] = 'awaiting_welcome_template_text'

    placeholders = "`{username}`, `{status}`, `{progress_bar}`, `{balance_str}`, `{purchases}`, `{basket_count}`"
    prompt_template = lang_data.get("welcome_add_text_prompt", "Template Name: {name}\n\nPlease reply with the full welcome message text. Available placeholders:\n{placeholders}")
    prompt = prompt_template.format(name=template_name, placeholders=placeholders.replace('`','')) # Plain text display
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_welcome|0")]] # Back to first page

    await send_message_with_retry(context.bot, chat_id, prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_welcome_template_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin entering the text for a new/edited welcome template."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    current_state = context.user_data.get("state")
    if user_id != ADMIN_ID or current_state not in ['awaiting_welcome_template_text', 'awaiting_welcome_template_edit']: return
    if not update.message or not update.message.text: return

    template_text = update.message.text # Keep raw text
    lang, lang_data = _get_lang_data(context) # Use helper

    if len(template_text) > 3500: # Keep below Telegram limit
        await send_message_with_retry(context.bot, chat_id, "❌ Template text too long (max ~3500 chars). Please shorten it.")
        return # Keep state

    if 'pending_welcome_template' not in context.user_data:
        # This might happen if the state wasn't cleaned up properly, try to recover
        if current_state == 'awaiting_welcome_template_edit':
            name = context.user_data.get('editing_welcome_template_name')
            if name:
                context.user_data['pending_welcome_template'] = {'name': name, 'is_editing': True}
                logger.warning("Recovered pending_welcome_template context for editing.")
            else:
                 logger.error("State is awaiting_welcome_template_edit but name is missing and cannot recover.")
                 await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.", parse_mode=None)
                 context.user_data.pop('state', None)
                 return
        else:
            logger.error("State is awaiting_welcome_template_text but pending_welcome_template missing.")
            await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.", parse_mode=None)
            context.user_data.pop('state', None)
            return

    # Store the text
    context.user_data['pending_welcome_template']['text'] = template_text

    # Determine if adding or editing
    is_editing = (current_state == 'awaiting_welcome_template_edit')
    context.user_data['pending_welcome_template']['is_editing'] = is_editing

    if not is_editing:
        # If adding new, now ask for description
        context.user_data['state'] = 'awaiting_welcome_description'
        prompt_template = lang_data.get("welcome_add_description_prompt", "Optional: Enter a short description for this template (admin view only). Send '-' to skip.")
        template_name = context.user_data.get('pending_welcome_template',{}).get('name', 'New Template')
        prompt = f"Text for '{template_name}' received.\n\n{prompt_template}"
        offset = context.user_data.get('editing_welcome_offset', 0) # Use offset if available (though unlikely for add)
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_manage_welcome|{offset}")]]
        await send_message_with_retry(context.bot, chat_id, prompt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    else:
        # If editing text, show preview directly
        context.user_data.pop('state', None) # Clear current state before showing preview
        # Fetch description to include in preview
        template_name = context.user_data.get('pending_welcome_template', {}).get('name')
        if template_name:
            conn = None; current_desc = ""
            try:
                conn = get_db_connection(); c = conn.cursor()
                c.execute("SELECT description FROM welcome_messages WHERE name = ?", (template_name,))
                row = c.fetchone(); current_desc = row['description'] if row else ""
            except Exception as e: logger.error(f"Error fetching desc for preview: {e}")
            finally:
                 if conn: conn.close()
            context.user_data['pending_welcome_template']['description'] = current_desc # Add existing desc for preview
        await _show_welcome_preview(update, context)

# <<< NEW >>>
async def handle_adm_welcome_description_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the description for a NEW welcome template."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID or context.user_data.get("state") != 'awaiting_welcome_description': return
    if not update.message or not update.message.text: return

    description = update.message.text.strip()
    if description == '-': description = None # Treat '-' as skip/None
    elif len(description) > 200:
        await send_message_with_retry(context.bot, chat_id, "❌ Description too long (max 200 chars).")
        return # Keep state

    if 'pending_welcome_template' not in context.user_data:
        logger.error("State is awaiting_welcome_description but pending data missing.")
        context.user_data.pop('state', None)
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.")
        return

    context.user_data['pending_welcome_template']['description'] = description
    context.user_data.pop('state', None) # Clear state before showing preview
    await _show_welcome_preview(update, context)

# <<< NEW >>>
async def handle_adm_welcome_description_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the edited description for an EXISTING welcome template."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID or context.user_data.get("state") != 'awaiting_welcome_description_edit': return
    if not update.message or not update.message.text: return

    new_description = update.message.text.strip()
    template_name = context.user_data.get('editing_welcome_template_name')

    if not template_name:
        logger.error("State is awaiting_welcome_description_edit but name is missing.")
        context.user_data.pop('state', None)
        context.user_data.pop("editing_welcome_template_name", None) # Clean up
        context.user_data.pop("editing_welcome_field", None)
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.")
        return

    if new_description == '-':
        # User wants to skip editing description, treat as cancel of this specific edit step
        offset = context.user_data.get('editing_welcome_offset', 0)
        await handle_adm_edit_welcome(update, context, params=[template_name, str(offset)])
        return

    if len(new_description) > 200:
        await send_message_with_retry(context.bot, chat_id, "❌ Description too long (max 200 chars).")
        return # Keep state

    # Fetch the existing text (needed because we only edited the description)
    conn_text = None; existing_text = ""
    try:
        conn_text = get_db_connection(); c_text = conn_text.cursor()
        c_text.execute("SELECT template_text FROM welcome_messages WHERE name = ?", (template_name,))
        row_text = c_text.fetchone()
        if row_text: existing_text = row_text['template_text']
        else: logger.warning(f"Could not fetch existing text for template {template_name} during desc edit.")
    except Exception as e: logger.error(f"Error fetching existing text: {e}")
    finally:
        if conn_text: conn_text.close()

    # Prepare data for preview
    context.user_data['pending_welcome_template'] = {
        'name': template_name,
        'text': existing_text, # Use existing text
        'description': new_description if new_description else None, # Store new description (or None)
        'is_editing': True, # It's an edit overall
        'offset': context.user_data.get('editing_welcome_offset', 0)
    }
    context.user_data.pop("state", None)
    context.user_data.pop("editing_welcome_template_name", None) # Clean up specific edit state
    context.user_data.pop("editing_welcome_field", None) # Clean up field indicator
    await _show_welcome_preview(update, context)


# --- Confirmation Handler ---
async def handle_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles generic 'Yes' confirmation based on stored action in user_data."""
    query = update.callback_query
    user_id = query.from_user.id
    is_primary_admin = (user_id == ADMIN_ID)
    if not is_primary_admin:
        logger.warning(f"Non-primary admin {user_id} tried to confirm a destructive action.")
        await query.answer("Permission denied for this action.", show_alert=True)
        return

    user_specific_data = context.user_data
    action = user_specific_data.pop("confirm_action", None)

    if not action:
        try: await query.edit_message_text("❌ Error: No action pending confirmation.", parse_mode=None)
        except telegram_error.BadRequest: pass # Ignore if not modified
        return
    chat_id = query.message.chat_id
    action_parts = action.split("|")
    action_type = action_parts[0]
    action_params = action_parts[1:]
    logger.info(f"Admin {user_id} confirmed action: {action_type} with params: {action_params}")
    success_msg, next_callback = "✅ Action completed successfully!", "admin_menu"
    conn = None # Initialize conn
    try:
        conn = get_db_connection() # Use helper
        c = conn.cursor()
        c.execute("BEGIN")
        # --- Delete City Logic ---
        if action_type == "delete_city":
             if not action_params: raise ValueError("Missing city_id")
             city_id_str = action_params[0]; city_id_int = int(city_id_str)
             city_name = CITIES.get(city_id_str)
             if city_name:
                 c.execute("SELECT id FROM products WHERE city = ?", (city_name,))
                 product_ids_to_delete = [row['id'] for row in c.fetchall()] # Use column name
                 logger.info(f"Admin Action (delete_city): Deleting city '{city_name}'. Associated product IDs to be deleted: {product_ids_to_delete}")
                 if product_ids_to_delete:
                     placeholders = ','.join('?' * len(product_ids_to_delete))
                     c.execute(f"DELETE FROM product_media WHERE product_id IN ({placeholders})", product_ids_to_delete)
                     for pid in product_ids_to_delete:
                          media_dir_to_del = os.path.join(MEDIA_DIR, str(pid))
                          if await asyncio.to_thread(os.path.exists, media_dir_to_del):
                              asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_del, ignore_errors=True))
                              logger.info(f"Scheduled deletion of media dir: {media_dir_to_del}")
                 c.execute("DELETE FROM products WHERE city = ?", (city_name,)) # Actual product deletion
                 c.execute("DELETE FROM districts WHERE city_id = ?", (city_id_int,))
                 delete_city_result = c.execute("DELETE FROM cities WHERE id = ?", (city_id_int,))
                 if delete_city_result.rowcount > 0:
                     conn.commit(); load_all_data()
                     success_msg = f"✅ City '{city_name}' and contents deleted!"
                     next_callback = "adm_manage_cities"
                 else: conn.rollback(); success_msg = f"❌ Error: City '{city_name}' not found."
             else: conn.rollback(); success_msg = "❌ Error: City not found (already deleted?)."
        # --- Delete District Logic ---
        elif action_type == "remove_district":
             if len(action_params) < 2: raise ValueError("Missing city/dist_id")
             city_id_str, dist_id_str = action_params[0], action_params[1]
             city_id_int, dist_id_int = int(city_id_str), int(dist_id_str)
             city_name = CITIES.get(city_id_str)
             c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (dist_id_int, city_id_int))
             dist_res = c.fetchone(); district_name = dist_res['name'] if dist_res else None # Use column name
             if city_name and district_name:
                 c.execute("SELECT id FROM products WHERE city = ? AND district = ?", (city_name, district_name))
                 product_ids_to_delete = [row['id'] for row in c.fetchall()] # Use column name
                 logger.info(f"Admin Action (remove_district): Deleting district '{district_name}' in '{city_name}'. Associated product IDs to be deleted: {product_ids_to_delete}")
                 if product_ids_to_delete:
                     placeholders = ','.join('?' * len(product_ids_to_delete))
                     c.execute(f"DELETE FROM product_media WHERE product_id IN ({placeholders})", product_ids_to_delete)
                     for pid in product_ids_to_delete:
                          media_dir_to_del = os.path.join(MEDIA_DIR, str(pid))
                          if await asyncio.to_thread(os.path.exists, media_dir_to_del):
                              asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_del, ignore_errors=True))
                              logger.info(f"Scheduled deletion of media dir: {media_dir_to_del}")
                 c.execute("DELETE FROM products WHERE city = ? AND district = ?", (city_name, district_name)) # Actual product deletion
                 delete_dist_result = c.execute("DELETE FROM districts WHERE id = ? AND city_id = ?", (dist_id_int, city_id_int))
                 if delete_dist_result.rowcount > 0:
                     conn.commit(); load_all_data()
                     success_msg = f"✅ District '{district_name}' removed from {city_name}!"
                     next_callback = f"adm_manage_districts_city|{city_id_str}"
                 else: conn.rollback(); success_msg = f"❌ Error: District '{district_name}' not found."
             else: conn.rollback(); success_msg = "❌ Error: City or District not found."
        # --- Delete Product Logic ---
        elif action_type == "confirm_remove_product":
             if not action_params: raise ValueError("Missing product_id")
             product_id = int(action_params[0])
             c.execute("SELECT ci.id as city_id, di.id as dist_id, p.product_type FROM products p LEFT JOIN cities ci ON p.city = ci.name LEFT JOIN districts di ON p.district = di.name AND ci.id = di.city_id WHERE p.id = ?", (product_id,))
             back_details_tuple = c.fetchone() # Result is already a Row object
             logger.info(f"Admin Action (confirm_remove_product): Deleting product ID {product_id}")
             c.execute("DELETE FROM product_media WHERE product_id = ?", (product_id,))
             delete_prod_result = c.execute("DELETE FROM products WHERE id = ?", (product_id,)) # Actual product deletion
             if delete_prod_result.rowcount > 0:
                  conn.commit()
                  success_msg = f"✅ Product ID {product_id} removed!"
                  media_dir_to_delete = os.path.join(MEDIA_DIR, str(product_id))
                  if await asyncio.to_thread(os.path.exists, media_dir_to_delete):
                       asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_delete, ignore_errors=True))
                       logger.info(f"Scheduled deletion of media dir: {media_dir_to_delete}")
                  if back_details_tuple and all([back_details_tuple['city_id'], back_details_tuple['dist_id'], back_details_tuple['product_type']]):
                      next_callback = f"adm_manage_products_type|{back_details_tuple['city_id']}|{back_details_tuple['dist_id']}|{back_details_tuple['product_type']}" # Use column names
                  else: next_callback = "adm_manage_products"
             else: conn.rollback(); success_msg = f"❌ Error: Product ID {product_id} not found."
        # --- Safe Delete Product Type Logic ---
        elif action_type == "delete_type":
              if not action_params: raise ValueError("Missing type_name")
              type_name = action_params[0]
              c.execute("SELECT COUNT(*) FROM products WHERE product_type = ?", (type_name,))
              product_count = c.fetchone()[0]
              c.execute("SELECT COUNT(*) FROM reseller_discounts WHERE product_type = ?", (type_name,))
              reseller_discount_count = c.fetchone()[0]
              if product_count == 0 and reseller_discount_count == 0:
                  delete_type_result = c.execute("DELETE FROM product_types WHERE name = ?", (type_name,))
                  if delete_type_result.rowcount > 0:
                       conn.commit(); load_all_data()
                       success_msg = f"✅ Type '{type_name}' deleted!"
                       next_callback = "adm_manage_types"
                  else: conn.rollback(); success_msg = f"❌ Error: Type '{type_name}' not found."
              else:
                  conn.rollback();
                  error_msg_parts = []
                  if product_count > 0: error_msg_parts.append(f"{product_count} product(s)")
                  if reseller_discount_count > 0: error_msg_parts.append(f"{reseller_discount_count} reseller discount rule(s)")
                  usage_details = " and ".join(error_msg_parts)
                  success_msg = f"❌ Error: Cannot delete type '{type_name}' as it is used by {usage_details}."
                  next_callback = "adm_manage_types"
        # --- Force Delete Product Type Logic (CASCADE) ---
        elif action_type == "force_delete_type_CASCADE":
            if not action_params: raise ValueError("Missing type_name for force delete")
            type_name = action_params[0]
            # Clean up the user_data entry now that we are processing it
            user_specific_data.pop('force_delete_type_name', None)
            logger.warning(f"Admin {user_id} initiated FORCE DELETE for type '{type_name}' and all associated data.")

            c.execute("SELECT id FROM products WHERE product_type = ?", (type_name,))
            product_ids_to_delete_media_for = [row['id'] for row in c.fetchall()]

            if product_ids_to_delete_media_for:
                placeholders = ','.join('?' * len(product_ids_to_delete_media_for))
                c.execute(f"DELETE FROM product_media WHERE product_id IN ({placeholders})", product_ids_to_delete_media_for)
                logger.info(f"Force delete: Deleted media entries for {len(product_ids_to_delete_media_for)} products of type '{type_name}'.")
                for pid in product_ids_to_delete_media_for:
                    media_dir_to_del = os.path.join(MEDIA_DIR, str(pid))
                    if await asyncio.to_thread(os.path.exists, media_dir_to_del):
                        asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_del, ignore_errors=True))
                        logger.info(f"Force delete: Scheduled deletion of media dir: {media_dir_to_del}")

            delete_products_res = c.execute("DELETE FROM products WHERE product_type = ?", (type_name,))
            products_deleted_count = delete_products_res.rowcount if delete_products_res else 0
            delete_discounts_res = c.execute("DELETE FROM reseller_discounts WHERE product_type = ?", (type_name,))
            discounts_deleted_count = delete_discounts_res.rowcount if delete_discounts_res else 0
            delete_type_res = c.execute("DELETE FROM product_types WHERE name = ?", (type_name,))

            if delete_type_res.rowcount > 0:
                conn.commit(); load_all_data()
                log_admin_action(admin_id=user_id, action="PRODUCT_TYPE_FORCE_DELETE",
                                 reason=f"Type: '{type_name}'. Deleted {products_deleted_count} products, {discounts_deleted_count} discount rules.",
                                 old_value=type_name)
                success_msg = (f"💣 Type '{type_name}' and all associated data FORCE DELETED.\n"
                               f"Deleted: {products_deleted_count} products, {discounts_deleted_count} discount rules.")
            else:
                conn.rollback()
                success_msg = f"❌ Error: Type '{type_name}' not found during final delete step. It might have been deleted already or partial changes occurred."
            next_callback = "adm_manage_types"
        # --- Product Type Reassignment Logic ---
        elif action_type == "confirm_reassign_type":
            if len(action_params) < 2: raise ValueError("Missing old_type_name or new_type_name for reassign")
            old_type_name, new_type_name = action_params[0], action_params[1]
            load_all_data()

            if old_type_name == new_type_name:
                success_msg = "❌ Error: Old and new type names cannot be the same."
                next_callback = "adm_reassign_type_start"
            elif not (old_type_name in PRODUCT_TYPES and new_type_name in PRODUCT_TYPES):
                success_msg = "❌ Error: One or both product types not found. Ensure they exist."
                next_callback = "adm_reassign_type_start"
            else:
                logger.info(f"Admin {user_id} confirmed reassignment from '{old_type_name}' to '{new_type_name}'.")
                update_products_res = c.execute("UPDATE products SET product_type = ? WHERE product_type = ?", (new_type_name, old_type_name))
                products_reassigned = update_products_res.rowcount if update_products_res else 0
                reseller_reassigned = 0
                try:
                    update_reseller_res = c.execute("UPDATE reseller_discounts SET product_type = ? WHERE product_type = ?", (new_type_name, old_type_name))
                    reseller_reassigned = update_reseller_res.rowcount if update_reseller_res else 0
                except sqlite3.IntegrityError as ie:
                    logger.warning(f"IntegrityError reassigning reseller_discounts from '{old_type_name}' to '{new_type_name}': {ie}. Deleting old conflicting rules.")
                    delete_conflicting_reseller_rules = c.execute("DELETE FROM reseller_discounts WHERE product_type = ?", (old_type_name,))
                    reseller_reassigned = delete_conflicting_reseller_rules.rowcount if delete_conflicting_reseller_rules else 0
                    logger.info(f"Deleted {reseller_reassigned} discount rules for old type '{old_type_name}' due to conflict on reassign.")

                delete_type_res = c.execute("DELETE FROM product_types WHERE name = ?", (old_type_name,))
                type_deleted = delete_type_res.rowcount > 0

                if type_deleted:
                    conn.commit(); load_all_data()
                    log_admin_action(admin_id=user_id, action=ACTION_PRODUCT_TYPE_REASSIGN,
                                     reason=f"From '{old_type_name}' to '{new_type_name}'. Reassigned {products_reassigned} products, affected {reseller_reassigned} discount entries.",
                                     old_value=old_type_name, new_value=new_type_name)
                    success_msg = (f"✅ Type '{old_type_name}' reassigned to '{new_type_name}' and deleted.\n"
                                   f"Reassigned: {products_reassigned} products. Affected discount entries: {reseller_reassigned}.")
                else:
                    conn.rollback()
                    success_msg = f"❌ Error: Could not delete old type '{old_type_name}'. No changes made."
                next_callback = "adm_manage_types"
        # --- Delete General Discount Code Logic ---
        elif action_type == "delete_discount":
             if not action_params: raise ValueError("Missing discount_id")
             code_id = int(action_params[0])
             c.execute("SELECT code FROM discount_codes WHERE id = ?", (code_id,))
             code_res = c.fetchone(); code_text = code_res['code'] if code_res else f"ID {code_id}"
             delete_disc_result = c.execute("DELETE FROM discount_codes WHERE id = ?", (code_id,))
             if delete_disc_result.rowcount > 0:
                 conn.commit(); success_msg = f"✅ Discount code {code_text} deleted!"
                 next_callback = "adm_manage_discounts"
             else: conn.rollback(); success_msg = f"❌ Error: Discount code {code_text} not found."
        # --- Delete Review Logic ---
        elif action_type == "delete_review":
            if not action_params: raise ValueError("Missing review_id")
            review_id = int(action_params[0])
            delete_rev_result = c.execute("DELETE FROM reviews WHERE review_id = ?", (review_id,))
            if delete_rev_result.rowcount > 0:
                conn.commit(); success_msg = f"✅ Review ID {review_id} deleted!"
                next_callback = "adm_manage_reviews|0"
            else: conn.rollback(); success_msg = f"❌ Error: Review ID {review_id} not found."
        # <<< Welcome Message Delete Logic >>>
        elif action_type == "delete_welcome_template":
            if not action_params: raise ValueError("Missing template_name")
            name_to_delete = action_params[0]
            delete_wm_result = c.execute("DELETE FROM welcome_messages WHERE name = ?", (name_to_delete,))
            if delete_wm_result.rowcount > 0:
                 conn.commit(); success_msg = f"✅ Welcome template '{name_to_delete}' deleted!"
                 next_callback = "adm_manage_welcome|0"
            else: conn.rollback(); success_msg = f"❌ Error: Welcome template '{name_to_delete}' not found."
        # <<< Reset Welcome Message Logic >>>
        elif action_type == "reset_default_welcome":
            try:
                built_in_text = LANGUAGES['en']['welcome']
                c.execute("UPDATE welcome_messages SET template_text = ? WHERE name = ?", (built_in_text, "default"))
                c.execute("INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
                          ("active_welcome_message_name", "default"))
                conn.commit(); success_msg = "✅ 'default' welcome template reset and activated."
            except Exception as reset_e:
                 conn.rollback(); logger.error(f"Error resetting default welcome message: {reset_e}", exc_info=True)
                 success_msg = "❌ Error resetting default template."
            next_callback = "adm_manage_welcome|0"
        # <<< Delete Reseller Discount Rule Logic >>>
        elif action_type == "confirm_delete_reseller_discount":
            if len(action_params) < 2: raise ValueError("Missing reseller_id or product_type")
            try:
                reseller_id = int(action_params[0]); product_type = action_params[1]
                c.execute("SELECT discount_percentage FROM reseller_discounts WHERE reseller_user_id = ? AND product_type = ?", (reseller_id, product_type))
                old_res = c.fetchone(); old_value = old_res['discount_percentage'] if old_res else None
                delete_res_result = c.execute("DELETE FROM reseller_discounts WHERE reseller_user_id = ? AND product_type = ?", (reseller_id, product_type))
                if delete_res_result.rowcount > 0:
                    conn.commit(); log_admin_action(user_id, ACTION_RESELLER_DISCOUNT_DELETE, reseller_id, reason=f"Type: {product_type}", old_value=old_value)
                    success_msg = f"✅ Reseller discount rule deleted for {product_type}."
                else: conn.rollback(); success_msg = f"❌ Error: Reseller discount rule for {product_type} not found."
                next_callback = f"reseller_manage_specific|{reseller_id}"
            except (ValueError, IndexError) as param_err:
                conn.rollback(); logger.error(f"Invalid params for delete reseller discount: {action_params} - {param_err}")
                success_msg = "❌ Error processing request."; next_callback = "admin_menu"
        # <<< Clear All Reservations Logic >>>
        elif action_type == "clear_all_reservations":
            logger.warning(f"ADMIN ACTION: Admin {user_id} is clearing ALL reservations and baskets.")
            update_products_res = c.execute("UPDATE products SET reserved = 0 WHERE reserved > 0")
            products_cleared = update_products_res.rowcount if update_products_res else 0
            update_users_res = c.execute("UPDATE users SET basket = '' WHERE basket IS NOT NULL AND basket != ''")
            baskets_cleared = update_users_res.rowcount if update_users_res else 0
            conn.commit()
            log_admin_action(admin_id=user_id, action="CLEAR_ALL_RESERVATIONS", reason=f"Cleared {products_cleared} reservations and {baskets_cleared} user baskets.")
            success_msg = f"✅ Cleared {products_cleared} product reservations and emptied {baskets_cleared} user baskets."
            next_callback = "admin_menu"
        else:
            logger.error(f"Unknown confirmation action type: {action_type}")
            conn.rollback(); success_msg = "❌ Unknown action confirmed."
            next_callback = "admin_menu"

        try: await query.edit_message_text(success_msg, parse_mode=None)
        except telegram_error.BadRequest: pass

        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data=next_callback)]]
        await send_message_with_retry(context.bot, chat_id, "Action complete. What next?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except (sqlite3.Error, ValueError, OSError, Exception) as e:
        logger.error(f"Error executing confirmed action '{action}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        error_text = str(e)
        try: await query.edit_message_text(f"❌ An error occurred: {error_text}", parse_mode=None)
        except Exception as edit_err: logger.error(f"Failed to edit message with error: {edit_err}")
    finally:
        if conn: conn.close()
        # Clean up specific user_data keys used by certain flows after confirmation
        if action_type.startswith("force_delete_type_CASCADE"):
            user_specific_data.pop('force_delete_type_name', None)
        elif action_type.startswith("confirm_reassign_type"):
            user_specific_data.pop('reassign_old_type_name', None)
            user_specific_data.pop('reassign_new_type_name', None)

# --- END OF FILE admin_features.py ---