# --- START OF FILE admin_product_management.py ---

import sqlite3
import os
import logging
import json
import tempfile # For media downloads
import shutil # For media moving/deletion
import time
# import secrets # Not directly used by functions here (discounts are in admin_features)
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict
# import math # Not directly used by functions here (pagination for welcome/reviews is in admin_features)
from decimal import Decimal # Ensure Decimal is imported

# --- Telegram Imports ---
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaAnimation # For media handling
)
from telegram.constants import ParseMode # Keep for reference
from telegram.ext import ContextTypes, JobQueue # Import JobQueue for media group processing
from telegram import helpers # For Markdown escaping if needed by functions here (admin_menu)
import telegram.error as telegram_error

# --- Local Imports ---
from utils import (
    CITIES, DISTRICTS, PRODUCT_TYPES, ADMIN_ID, LANGUAGES, THEMES, # For product addition, menus
    BOT_MEDIA, SIZES, # For product addition
    # fetch_reviews, # Moved to admin_features
    format_currency, send_message_with_retry,
    # get_date_range, # Moved to admin_features (sales)
    TOKEN, load_all_data,
    # format_discount_value, # Moved to admin_features
    SECONDARY_ADMIN_IDS, # For permission checks
    get_db_connection, MEDIA_DIR, BOT_MEDIA_JSON_PATH, # For product media and bot media
    DEFAULT_PRODUCT_EMOJI, # For product display
    # fetch_user_ids_for_broadcast, # Moved to admin_features
    # get_welcome_message_templates, get_welcome_message_template_count, # Moved to admin_features
    # add_welcome_message_template, update_welcome_message_template, # Moved to admin_features
    # delete_welcome_message_template, set_active_welcome_message, DEFAULT_WELCOME_MESSAGE, # Moved to admin_features
    # get_user_status, get_progress_bar, # Not directly used here, maybe by welcome preview in admin_features
    _get_lang_data, # For localized messages
    log_admin_action, ACTION_PRODUCT_TYPE_REASSIGN, # For logging actions
    # ACTION_RESELLER_DISCOUNT_DELETE # Not used here, confirm_yes is in admin_features
)

# --- Import viewer admin handlers (for main admin menu links) ---
try:
    from viewer_admin import (
        handle_viewer_admin_menu,
        handle_manage_users_start,
        handle_viewer_added_products,
        handle_viewer_view_product_media
    )
except ImportError:
    logger_dummy_viewer = logging.getLogger(__name__ + "_dummy_viewer")
    logger_dummy_viewer.error("Could not import handlers from viewer_admin.py.")
    async def handle_viewer_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "Secondary admin menu handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)
    async def handle_manage_users_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "Manage Users handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)
    async def handle_viewer_added_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "Added Products Log handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)
    async def handle_viewer_view_product_media(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "View Product Media handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)
# ------------------------------------

# --- Import Reseller Management Handlers (for main admin menu links) ---
try:
    from reseller_management import (
        handle_manage_resellers_menu,
        handle_reseller_manage_id_message, 
        handle_reseller_toggle_status,
        handle_manage_reseller_discounts_select_reseller,
        # Other reseller handlers are not directly called from this file's menu
    )
except ImportError:
    logger_dummy_reseller = logging.getLogger(__name__ + "_dummy_reseller")
    logger_dummy_reseller.error("Could not import handlers from reseller_management.py.")
    async def handle_manage_resellers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "Reseller Status Mgmt handler not found."
        if query: await query.edit_message_text(msg)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg)
    async def handle_manage_reseller_discounts_select_reseller(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query; msg = "Reseller Discount Mgmt handler not found."
        if query: await query.edit_message_text(msg)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg)
    async def handle_reseller_manage_id_message(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
    async def handle_reseller_toggle_status(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass
# ------------------------------------------

# Import stock handler (for main admin menu link)
try: from stock import handle_view_stock
except ImportError:
    logger_dummy_stock = logging.getLogger(__name__ + "_dummy_stock")
    logger_dummy_stock.error("Could not import handle_view_stock from stock.py.")
    async def handle_view_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        query = update.callback_query
        msg = "Stock viewing handler not found."
        if query: await query.edit_message_text(msg, parse_mode=None)
        else: await send_message_with_retry(context.bot, update.effective_chat.id, msg, parse_mode=None)

# Logging setup
logger = logging.getLogger(__name__)

# --- Constants for Media Group Handling & Bulk Add ---
MEDIA_GROUP_COLLECTION_DELAY = 2.0 # Seconds to wait for more media in a group
BULK_ADD_LIMIT = 10 # Max items per bulk session

# --- Helper Function to Remove Existing Job ---
def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Removes a job by name if it exists."""
    if not hasattr(context, 'job_queue') or not context.job_queue:
        logger.warning("Job queue not available in context for remove_job_if_exists.")
        return False
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
        logger.debug(f"Removed existing job: {name}")
    return True

# --- Helper to Prepare and Confirm Drop (Handles Download) ---
async def _prepare_and_confirm_drop(
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict, 
    chat_id: int,
    admin_user_id: int, 
    text: str,
    collected_media_info: list
    ):
    """Downloads media (if any) and presents the confirmation message."""
    required_context = ["admin_city", "admin_district", "admin_product_type", "pending_drop_size", "pending_drop_price"]
    if not all(k in user_data for k in required_context):
        logger.error(f"_prepare_and_confirm_drop: Context lost for admin {admin_user_id}.")
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start adding product again.", parse_mode=None)
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price", "collecting_media_group_id", "collected_media"]
        for key in keys_to_clear: user_data.pop(key, None)
        return

    temp_dir = None
    media_list_for_db = []
    download_errors = 0

    if collected_media_info:
        try:
            temp_dir = await asyncio.to_thread(tempfile.mkdtemp)
            logger.info(f"Created temp dir for media download: {temp_dir} (Admin: {admin_user_id})")
            for i, media_info in enumerate(collected_media_info):
                media_type = media_info['type']
                file_id = media_info['file_id']
                file_extension = ".jpg" if media_type == "photo" else ".mp4" if media_type in ["video", "gif"] else ".dat"
                temp_file_path = os.path.join(temp_dir, f"{file_id}{file_extension}")
                try:
                    logger.info(f"Downloading media {i+1}/{len(collected_media_info)} ({file_id}) to {temp_file_path}")
                    file_obj = await context.bot.get_file(file_id)
                    await file_obj.download_to_drive(custom_path=temp_file_path)
                    if not await asyncio.to_thread(os.path.exists, temp_file_path) or await asyncio.to_thread(os.path.getsize, temp_file_path) == 0:
                        raise IOError(f"Downloaded file {temp_file_path} is missing or empty.")
                    media_list_for_db.append({"type": media_type, "path": temp_file_path, "file_id": file_id})
                    logger.info(f"Media download {i+1} successful.")
                except (telegram_error.TelegramError, IOError, OSError) as e:
                    logger.error(f"Error downloading/verifying media {i+1} ({file_id}): {e}")
                    download_errors += 1
                except Exception as e:
                    logger.error(f"Unexpected error downloading media {i+1} ({file_id}): {e}", exc_info=True)
                    download_errors += 1
            if download_errors > 0:
                await send_message_with_retry(context.bot, chat_id, f"⚠️ Warning: {download_errors} media file(s) failed to download. Adding drop with successfully downloaded media only.", parse_mode=None)
        except Exception as e:
             logger.error(f"Error setting up/during media download loop admin {admin_user_id}: {e}", exc_info=True)
             await send_message_with_retry(context.bot, chat_id, "⚠️ Warning: Error during media processing. Drop will be added without media.", parse_mode=None)
             media_list_for_db = [] 
             if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True); temp_dir = None

    user_data["pending_drop_admin_id"] = admin_user_id

    user_data["pending_drop"] = {
        "city": user_data["admin_city"], "district": user_data["admin_district"],
        "product_type": user_data["admin_product_type"], "size": user_data["pending_drop_size"],
        "price": user_data["pending_drop_price"], "original_text": text,
        "media": media_list_for_db, 
        "temp_dir": temp_dir 
    }
    user_data.pop("state", None) 

    city_name = user_data['admin_city']
    dist_name = user_data['admin_district']
    type_name = user_data['admin_product_type']
    type_emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)
    size_name = user_data['pending_drop_size']
    price_str = format_currency(user_data['pending_drop_price'])
    text_preview = text[:200] + ("..." if len(text) > 200 else "")
    text_display = text_preview if text_preview else "No details text provided"
    media_count = len(user_data["pending_drop"]["media"]) 
    total_submitted_media = len(collected_media_info) 
    media_status = f"{media_count}/{total_submitted_media} Downloaded" if total_submitted_media > 0 else "No"
    if download_errors > 0: media_status += " (Errors)"


    msg = (f"📦 Confirm New Drop\n\n🏙️ City: {city_name}\n🏘️ District: {dist_name}\n{type_emoji} Type: {type_name}\n"
           f"📏 Size: {size_name}\n💰 Price: {price_str} EUR\n📝 Details: {text_display}\n"
           f"📸 Media Attached: {media_status}\n\nAdd this drop?")
    keyboard = [[InlineKeyboardButton("✅ Yes, Add Drop", callback_data="confirm_add_drop"),
                InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_add")]]
    await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# --- Job Function to Process Collected Media Group ---
async def _process_collected_media(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to process a collected media group."""
    job_data = context.job.data
    admin_user_id = job_data.get("user_id") 
    chat_id = job_data.get("chat_id")
    media_group_id = job_data.get("media_group_id")

    if not admin_user_id or not chat_id or not media_group_id:
        logger.error(f"Job _process_collected_media missing user_id, chat_id, or media_group_id in data: {job_data}")
        return

    logger.info(f"Job executing: Process media group {media_group_id} for admin {admin_user_id}")
    user_data = context.application.user_data.get(admin_user_id, {}) 
    if not user_data:
         logger.error(f"Job {media_group_id}: Could not find user_data for admin {admin_user_id}.")
         return

    collected_info = user_data.get('collected_media', {}).get(media_group_id)
    if not collected_info or 'media' not in collected_info:
        logger.warning(f"Job {media_group_id}: No collected media info found in user_data for admin {admin_user_id}. Might be already processed or cancelled.")
        user_data.pop('collecting_media_group_id', None)
        if 'collected_media' in user_data:
            user_data['collected_media'].pop(media_group_id, None)
            if not user_data['collected_media']:
                user_data.pop('collected_media', None)
        return

    collected_media = collected_info.get('media', [])
    caption = collected_info.get('caption', '')

    user_data.pop('collecting_media_group_id', None)
    if 'collected_media' in user_data and media_group_id in user_data['collected_media']:
        del user_data['collected_media'][media_group_id]
        if not user_data['collected_media']:
            user_data.pop('collected_media', None)

    await _prepare_and_confirm_drop(context, user_data, chat_id, admin_user_id, caption, collected_media)


# --- Modified Handler for Drop Details Message ---
async def handle_adm_drop_details_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the message containing drop text and optional media (single or group)."""
    if not update.message or not update.effective_user:
        logger.warning("handle_adm_drop_details_message received invalid update.")
        return

    admin_user_id = update.effective_user.id 
    chat_id = update.effective_chat.id
    user_specific_data = context.user_data 

    if admin_user_id != ADMIN_ID and admin_user_id not in SECONDARY_ADMIN_IDS:
        logger.warning(f"Non-admin {admin_user_id} attempted to send drop details.")
        return

    if user_specific_data.get("state") != "awaiting_drop_details":
        logger.debug(f"Ignoring drop details message from admin {admin_user_id}, state is not 'awaiting_drop_details' (state: {user_specific_data.get('state')})")
        return

    required_context = ["admin_city", "admin_district", "admin_product_type", "pending_drop_size", "pending_drop_price"]
    if not all(k in user_specific_data for k in required_context):
        logger.warning(f"Context lost for admin {admin_user_id} before processing drop details.")
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start adding product again.", parse_mode=None)
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price", "collecting_media_group_id", "collected_media"]
        for key in keys_to_clear: user_specific_data.pop(key, None)
        return

    media_group_id = update.message.media_group_id
    job_name = f"process_media_group_{admin_user_id}_{media_group_id}" if media_group_id else None

    media_type, file_id = None, None
    if update.message.photo: media_type, file_id = "photo", update.message.photo[-1].file_id
    elif update.message.video: media_type, file_id = "video", update.message.video.file_id
    elif update.message.animation: media_type, file_id = "gif", update.message.animation.file_id

    text = (update.message.caption or update.message.text or "").strip()

    if media_group_id:
        logger.debug(f"Received message part of media group {media_group_id} from admin {admin_user_id}")
        if 'collected_media' not in user_specific_data:
            user_specific_data['collected_media'] = {}

        if media_group_id not in user_specific_data['collected_media']:
            user_specific_data['collected_media'][media_group_id] = {'media': [], 'caption': None}
            logger.info(f"Started collecting media for group {media_group_id} admin {admin_user_id}")
            user_specific_data['collecting_media_group_id'] = media_group_id

        if media_type and file_id:
            if not any(m['file_id'] == file_id for m in user_specific_data['collected_media'][media_group_id]['media']):
                user_specific_data['collected_media'][media_group_id]['media'].append(
                    {'type': media_type, 'file_id': file_id}
                )
                logger.debug(f"Added media {file_id} ({media_type}) to group {media_group_id}")

        if text: 
             user_specific_data['collected_media'][media_group_id]['caption'] = text
             logger.debug(f"Stored/updated caption for group {media_group_id}")

        remove_job_if_exists(job_name, context) 
        if hasattr(context, 'job_queue') and context.job_queue:
            context.job_queue.run_once(
                _process_collected_media,
                when=timedelta(seconds=MEDIA_GROUP_COLLECTION_DELAY),
                data={'media_group_id': media_group_id, 'chat_id': chat_id, 'user_id': admin_user_id}, 
                name=job_name,
                job_kwargs={'misfire_grace_time': 15}
            )
            logger.debug(f"Scheduled/Rescheduled job {job_name} for media group {media_group_id}")
        else:
            logger.error("JobQueue not found in context. Cannot schedule media group processing.")
            await send_message_with_retry(context.bot, chat_id, "❌ Error: Internal components missing. Cannot process media group.", parse_mode=None)

    else: 
        if user_specific_data.get('collecting_media_group_id'):
            logger.warning(f"Received single message from admin {admin_user_id} while potentially collecting media group {user_specific_data['collecting_media_group_id']}. Ignoring for drop.")
            return

        logger.debug(f"Received single message (or text only) for drop details from admin {admin_user_id}")
        user_specific_data.pop('collecting_media_group_id', None)
        user_specific_data.pop('collected_media', None)

        single_media_info = []
        if media_type and file_id:
            single_media_info.append({'type': media_type, 'file_id': file_id})

        await _prepare_and_confirm_drop(context, user_specific_data, chat_id, admin_user_id, text, single_media_info)


# --- Admin Callback Handlers ---
async def handle_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays the main admin dashboard, handling both command and callback."""
    user = update.effective_user
    query = update.callback_query
    if not user:
        logger.warning("handle_admin_menu triggered without effective_user.")
        if query: await query.answer("Error: Could not identify user.", show_alert=True)
        return

    user_id = user.id
    chat_id = update.effective_chat.id
    is_primary_admin = (user_id == ADMIN_ID)
    is_secondary_admin = (user_id in SECONDARY_ADMIN_IDS)

    if not is_primary_admin and not is_secondary_admin:
        logger.warning(f"Non-admin user {user_id} attempted to access admin menu via {'command' if not query else 'callback'}.")
        msg = "Access denied."
        if query: await query.answer(msg, show_alert=True)
        else: await send_message_with_retry(context.bot, chat_id, msg, parse_mode=None)
        return

    if is_secondary_admin and not is_primary_admin:
        logger.info(f"Redirecting secondary admin {user_id} to viewer admin menu.")
        try:
            return await handle_viewer_admin_menu(update, context)
        except NameError:
            logger.error("handle_viewer_admin_menu not found, check imports.")
            fallback_msg = "Viewer admin menu handler is missing."
            if query: await query.edit_message_text(fallback_msg)
            else: await send_message_with_retry(context.bot, chat_id, fallback_msg)
            return

    total_users, total_user_balance, active_products, total_sales_value = 0, Decimal('0.0'), 0, Decimal('0.0')
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users")
        res_users = c.fetchone(); total_users = res_users['count'] if res_users else 0
        c.execute("SELECT COALESCE(SUM(balance), 0.0) as total_bal FROM users")
        res_balance = c.fetchone(); total_user_balance = Decimal(str(res_balance['total_bal'])) if res_balance else Decimal('0.0')
        c.execute("SELECT COUNT(*) as count FROM products WHERE available > reserved")
        res_products = c.fetchone(); active_products = res_products['count'] if res_products else 0
        c.execute("SELECT COALESCE(SUM(price_paid), 0.0) as total_sales FROM purchases")
        res_sales = c.fetchone(); total_sales_value = Decimal(str(res_sales['total_sales'])) if res_sales else Decimal('0.0')
    except sqlite3.Error as e:
        logger.error(f"DB error fetching admin dashboard data: {e}", exc_info=True)
        error_message = "❌ Error loading admin data."
        if query:
            try: await query.edit_message_text(error_message, parse_mode=None)
            except Exception: pass
        else: await send_message_with_retry(context.bot, chat_id, error_message, parse_mode=None)
        return
    finally:
        if conn: conn.close()

    total_user_balance_str = format_currency(total_user_balance)
    total_sales_value_str = format_currency(total_sales_value)
    msg = (
       f"🔧 Admin Dashboard (Primary)\n\n"
       f"👥 Total Users: {total_users}\n"
       f"💰 Sum of User Balances: {total_user_balance_str} EUR\n"
       f"📈 Total Sales Value: {total_sales_value_str} EUR\n"
       f"📦 Active Products: {active_products}\n\n"
       "Select an action:"
    )

    keyboard = [
        [InlineKeyboardButton("📊 Sales Analytics", callback_data="sales_analytics_menu")],
        [InlineKeyboardButton("➕ Add Products", callback_data="adm_city")],
        [InlineKeyboardButton("📦➕ Bulk Add Products", callback_data="adm_bulk_start_setup")], 
        [InlineKeyboardButton("🗑️ Manage Products", callback_data="adm_manage_products")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="adm_manage_users|0")],
        [InlineKeyboardButton("👷 Manage Workers", callback_data="manage_workers_menu")],  # NEW: Worker Management
        [InlineKeyboardButton("👑 Manage Resellers", callback_data="manage_resellers_menu")],
        [InlineKeyboardButton("🏷️ Manage Reseller Discounts", callback_data="manage_reseller_discounts_select_reseller|0")],
        [InlineKeyboardButton("🏷️ Manage Discount Codes", callback_data="adm_manage_discounts")],
        [InlineKeyboardButton("👋 Manage Welcome Msg", callback_data="adm_manage_welcome|0")],
        [InlineKeyboardButton("📦 View Bot Stock", callback_data="view_stock")],
        [InlineKeyboardButton("📜 View Added Products Log", callback_data="viewer_added_products|0")],
        [InlineKeyboardButton("🗺️ Manage Districts", callback_data="adm_manage_districts")],
        [InlineKeyboardButton("🏙️ Manage Cities", callback_data="adm_manage_cities")],
        [InlineKeyboardButton("🧩 Manage Product Types", callback_data="adm_manage_types")],
        [InlineKeyboardButton("🔄 Reassign Product Type", callback_data="adm_reassign_type_start")],
        [InlineKeyboardButton("🚫 Manage Reviews", callback_data="adm_manage_reviews|0")],
        [InlineKeyboardButton("📦🏭 Bulk Stock Management", callback_data="admin_bulk_stock_menu")],
        [InlineKeyboardButton("🧹 Clear ALL Reservations", callback_data="adm_clear_reservations_confirm")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast_start")],
        [InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")],
        [InlineKeyboardButton("📸 Set Bot Media", callback_data="adm_set_media")],
        [InlineKeyboardButton("🏠 User Home Menu", callback_data="back_start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        try:
            await query.edit_message_text(msg, reply_markup=reply_markup, parse_mode=None)
        except telegram_error.BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.error(f"Error editing admin menu message: {e}")
                await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
            else:
                await query.answer()
        except Exception as e:
            logger.error(f"Unexpected error editing admin menu: {e}", exc_info=True)
            await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)
    else:
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=reply_markup, parse_mode=None)

# --- Add Product Flow Handlers (Permission checks updated for secondary admins) ---
async def handle_adm_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    lang, lang_data = _get_lang_data(context)
    if not CITIES:
        return await query.edit_message_text("No cities configured. Please add a city first via 'Manage Cities'.", parse_mode=None)
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_dist|{c}")] for c in sorted_city_ids]
    
    # This handler is for single add, not bulk add city selection after this point.
    # bulk_flow_step would not be 'city' here for single add.
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_menu")])
    select_city_text = lang_data.get("admin_select_city", "Select City to Add Product:")
    await query.edit_message_text(select_city_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_dist(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found. Please select again.", parse_mode=None)
    districts_in_city = DISTRICTS.get(city_id, {})
    lang, lang_data = _get_lang_data(context)
    select_district_template = lang_data.get("admin_select_district", "Select District in {city}:")
    
    # This handler is only for single add, bulk has its own district selection path
    back_callback_dist = "adm_city" 

    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back to Cities", callback_data=back_callback_dist)]]
        return await query.edit_message_text(f"No districts found for {city_name}. Please add districts via 'Manage Districts'.",
                                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
    keyboard = []
                                                     
    for d in sorted_district_ids:
        dist_name = districts_in_city.get(d)
        if dist_name:
            # For single add, this callback remains adm_type
            callback_data_dist_item = f"adm_type|{city_id}|{d}" 
            keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=callback_data_dist_item)])
        else: logger.warning(f"District name missing for ID {d} in city {city_id}")
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data=back_callback_dist)])

    select_district_text = select_district_template.format(city=city_name)
    await query.edit_message_text(select_district_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    # This is for SINGLE add product flow's type selection
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    if not params or len(params) < 2: return await query.answer("Error: City or District ID missing.", show_alert=True)
    city_id, dist_id = params[0], params[1] 
    
    context.user_data["admin_city_id_single"] = city_id 
    context.user_data["admin_district_id_single"] = dist_id

    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)

    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found for single add. Please select again.", parse_mode=None)
    
    lang, lang_data = _get_lang_data(context)
    select_type_text = lang_data.get("admin_select_type", "Select Product Type:")
    if not PRODUCT_TYPES:
        return await query.edit_message_text("No product types configured. Add types via 'Manage Product Types'.", parse_mode=None)

    keyboard = []
    for type_name_iter, emoji in sorted(PRODUCT_TYPES.items()):
        callback_data_type = f"adm_add|{city_id}|{dist_id}|{type_name_iter}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name_iter}", callback_data=callback_data_type)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_dist|{city_id}")])
    await query.edit_message_text(select_type_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_add(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    if not params or len(params) < 3: return await query.answer("Error: Location/Type info missing.", show_alert=True)
    city_id, dist_id, p_type = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found. Please select again.", parse_mode=None)
    type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
    context.user_data["admin_city_id"] = city_id
    context.user_data["admin_district_id"] = dist_id
    context.user_data["admin_product_type"] = p_type
    context.user_data["admin_city"] = city_name
    context.user_data["admin_district"] = district_name
    keyboard = [[InlineKeyboardButton(f"📏 {s}", callback_data=f"adm_size|{s}")] for s in SIZES]
    keyboard.append([InlineKeyboardButton("📏 Custom Size", callback_data="adm_custom_size")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Types", callback_data=f"adm_type|{city_id}|{dist_id}")])
    await query.edit_message_text(f"📦 Adding {type_emoji} {p_type} in {city_name} / {district_name}\n\nSelect size:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_size(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    if not params: return await query.answer("Error: Size missing.", show_alert=True)
    size = params[0]
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        return await query.edit_message_text("❌ Error: Context lost. Please start adding the product again.", parse_mode=None)
    context.user_data["pending_drop_size"] = size
    context.user_data["state"] = "awaiting_price"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await query.edit_message_text(f"Size set to {size}. Please reply with the price (e.g., 12.50 or 12.5):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter price in chat.")

async def handle_adm_custom_size(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    user_id_check = query.from_user.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        return await query.edit_message_text("❌ Error: Context lost. Please start adding the product again.", parse_mode=None)
    context.user_data["state"] = "awaiting_custom_size"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await query.edit_message_text("Please reply with the custom size (e.g., 10g, 1/4 oz):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter custom size in chat.")

async def handle_confirm_add_drop(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_uploader_id = query.from_user.id 
    if admin_uploader_id != ADMIN_ID and admin_uploader_id not in SECONDARY_ADMIN_IDS:
        return await query.answer("Access denied.", show_alert=True)

    chat_id = query.message.chat_id
    user_specific_data = context.user_data
    pending_drop = user_specific_data.get("pending_drop")
    uploader_admin_id_for_db = user_specific_data.pop("pending_drop_admin_id", admin_uploader_id)
    if uploader_admin_id_for_db != admin_uploader_id:
        logger.warning(f"Confirming admin {admin_uploader_id} is different from initiating admin {uploader_admin_id_for_db} for drop.")

    if not pending_drop:
        logger.error(f"Confirmation 'yes' received for add drop, but no pending_drop data found for admin {admin_uploader_id}.")
        user_specific_data.pop("state", None)
        return await query.edit_message_text("❌ Error: No pending drop data found. Please start again.", parse_mode=None)

    city = pending_drop.get("city"); district = pending_drop.get("district"); p_type = pending_drop.get("product_type")
    size = pending_drop.get("size"); price = pending_drop.get("price"); original_text = pending_drop.get("original_text", "")
    media_list = pending_drop.get("media", []); temp_dir = pending_drop.get("temp_dir")

    if not all([city, district, p_type, size, price is not None]):
        logger.error(f"Missing data in pending_drop for admin {admin_uploader_id}: {pending_drop}")
        if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price", "admin_city_id", "admin_district_id", "admin_product_type", "admin_city", "admin_district"]
        for key in keys_to_clear: user_specific_data.pop(key, None)
        return await query.edit_message_text("❌ Error: Incomplete drop data. Please start again.", parse_mode=None)

    product_name = f"{p_type} {size} {int(time.time())}"; conn = None; product_id = None
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("BEGIN")
        insert_params = (
            city, district, p_type, size, product_name, price, original_text,
            uploader_admin_id_for_db, 
            datetime.now(timezone.utc).isoformat()
        )
        logger.debug(f"Inserting product with params count: {len(insert_params)}")
        c.execute("""INSERT INTO products
                        (city, district, product_type, size, name, price, available, reserved, original_text, added_by, added_date)
                     VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""", insert_params)
        product_id = c.lastrowid

        if product_id and media_list and temp_dir:
            final_media_dir = os.path.join(MEDIA_DIR, str(product_id)); await asyncio.to_thread(os.makedirs, final_media_dir, exist_ok=True); media_inserts = []
            for media_item in media_list:
                if "path" in media_item and "type" in media_item and "file_id" in media_item:
                    temp_file_path = media_item["path"]
                    if await asyncio.to_thread(os.path.exists, temp_file_path):
                        new_filename = os.path.basename(temp_file_path); final_persistent_path = os.path.join(final_media_dir, new_filename)
                        try: await asyncio.to_thread(shutil.move, temp_file_path, final_persistent_path); media_inserts.append((product_id, media_item["type"], final_persistent_path, media_item["file_id"]))
                        except OSError as move_err: logger.error(f"Error moving media {temp_file_path}: {move_err}")
                    else: logger.warning(f"Temp media not found: {temp_file_path}")
                else: logger.warning(f"Incomplete media item: {media_item}")
            if media_inserts: c.executemany("INSERT INTO product_media (product_id, media_type, file_path, telegram_file_id) VALUES (?, ?, ?, ?)", media_inserts)

        conn.commit(); logger.info(f"Added product {product_id} ({product_name}) by admin {uploader_admin_id_for_db}.")
        if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True); logger.info(f"Cleaned temp dir: {temp_dir}")
        await query.edit_message_text("✅ Drop Added Successfully!", parse_mode=None)
        ctx_city_id = user_specific_data.get('admin_city_id'); ctx_dist_id = user_specific_data.get('admin_district_id'); ctx_p_type = user_specific_data.get('admin_product_type')
        add_another_callback = f"adm_add|{ctx_city_id}|{ctx_dist_id}|{ctx_p_type}" if all([ctx_city_id, ctx_dist_id, ctx_p_type]) else "admin_menu"
        keyboard = [ [InlineKeyboardButton("➕ Add Another Same Type", callback_data=add_another_callback)],
                     [InlineKeyboardButton("🔧 Admin Menu", callback_data="admin_menu"), InlineKeyboardButton("🏠 User Home", callback_data="back_start")] ]
        await send_message_with_retry(context.bot, chat_id, "What next?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except (sqlite3.Error, OSError, Exception) as e:
        try: conn.rollback() if conn and conn.in_transaction else None
        except Exception as rb_err: logger.error(f"Rollback failed: {rb_err}")
        logger.error(f"Error saving confirmed drop for admin {admin_uploader_id}: {e}", exc_info=True)
        await query.edit_message_text("❌ Error: Failed to save the drop. Please check logs and try again.", parse_mode=None)
        if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir): await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True); logger.info(f"Cleaned temp dir after error: {temp_dir}")
    finally:
        if conn: conn.close()
        keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price"]
        for key in keys_to_clear: user_specific_data.pop(key, None)


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    admin_user_id = update.effective_user.id 
    user_specific_data = context.user_data 
    pending_drop = user_specific_data.get("pending_drop")
    if pending_drop and "temp_dir" in pending_drop and pending_drop["temp_dir"]:
        temp_dir_path = pending_drop["temp_dir"]
        if await asyncio.to_thread(os.path.exists, temp_dir_path):
            try: await asyncio.to_thread(shutil.rmtree, temp_dir_path, ignore_errors=True); logger.info(f"Cleaned temp dir on cancel: {temp_dir_path}")
            except Exception as e: logger.error(f"Error cleaning temp dir {temp_dir_path}: {e}")
    keys_to_clear = ["state", "pending_drop", "pending_drop_size", "pending_drop_price", "admin_city_id", "admin_district_id", "admin_product_type", "admin_city", "admin_district", "collecting_media_group_id", "collected_media", "pending_drop_admin_id"]
    for key in keys_to_clear: user_specific_data.pop(key, None)
    if 'collecting_media_group_id' in user_specific_data: 
        media_group_id = user_specific_data.pop('collecting_media_group_id', None)
        if media_group_id: job_name = f"process_media_group_{admin_user_id}_{media_group_id}"; remove_job_if_exists(job_name, context)
    if query:
         try:
             await query.edit_message_text("❌ Add Product Cancelled", parse_mode=None)
         except telegram_error.BadRequest as e:
             if "message is not modified" in str(e).lower():
                 pass 
             else:
                 logger.error(f"Error editing cancel message: {e}")
         keyboard = [[InlineKeyboardButton("🔧 Admin Menu", callback_data="admin_menu"), InlineKeyboardButton("🏠 User Home", callback_data="back_start")]]; await send_message_with_retry(context.bot, query.message.chat_id, "Returning to Admin Menu.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.message: await send_message_with_retry(context.bot, update.message.chat_id, "Add product cancelled.")
    else: logger.info("Add product flow cancelled internally (no query/message object).")


# --- Manage Geography Handlers (Primary Admin Only) ---
async def handle_adm_manage_cities(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Use 'Add New City'.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")],
                                                                      [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = []
    for c in sorted_city_ids:
        city_name = CITIES.get(c,'N/A')
        keyboard.append([
             InlineKeyboardButton(f"🏙️ {city_name}", callback_data=f"adm_edit_city|{c}"),
             InlineKeyboardButton(f"🗑️ Delete", callback_data=f"adm_delete_city|{c}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Add New City", callback_data="adm_add_city")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🏙️ Manage Cities\n\nSelect a city or action:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_add_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    context.user_data["state"] = "awaiting_new_city_name"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text("🏙️ Please reply with the name for the new city:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter city name in chat.")

async def handle_adm_edit_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_edit_city_name"
    context.user_data["edit_city_id"] = city_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text(f"✏️ Editing city: {city_name}\n\nPlease reply with the new name for this city:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new city name in chat.")

async def handle_adm_delete_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["confirm_action"] = f"delete_city|{city_id}"
    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Are you sure you want to delete city: {city_name}?\n\n"
           f"🚨 This will permanently delete this city, all its districts, and all products listed within those districts!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete City", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_cities")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_manage_districts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Add a city first.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id,''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c, 'N/A')}", callback_data=f"adm_manage_districts_city|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🗺️ Manage Districts\n\nSelect the city whose districts you want to manage:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_manage_districts_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    districts_in_city = {}
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, name FROM districts WHERE city_id = ? ORDER BY name", (int(city_id),))
        districts_in_city = {str(row['id']): row['name'] for row in c.fetchall()}
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Failed to reload districts for city {city_id}: {e}")
        districts_in_city = DISTRICTS.get(city_id, {}) 
    finally:
        if conn: conn.close()

    msg = f"🗺️ Districts in {city_name}\n\n"
    keyboard = []
    if not districts_in_city: msg += "No districts found for this city."
    else:
        sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
        for d_id in sorted_district_ids:
            dist_name = districts_in_city.get(d_id)
            if dist_name:
                 keyboard.append([
                     InlineKeyboardButton(f"✏️ Edit {dist_name}", callback_data=f"adm_edit_district|{city_id}|{d_id}"),
                     InlineKeyboardButton(f"🗑️ Delete {dist_name}", callback_data=f"adm_remove_district|{city_id}|{d_id}")
                 ])
            else: logger.warning(f"District name missing for ID {d_id} in city {city_id} (manage view)")
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New District", callback_data=f"adm_add_district|{city_id}")],
        [InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_districts")]
    ])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing manage districts city message: {e}")
        else: await query.answer()

async def handle_adm_add_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_new_district_name"
    context.user_data["admin_add_district_city_id"] = city_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(f"➕ Adding district to {city_name}\n\nPlease reply with the name for the new district:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter district name in chat.")

async def handle_adm_edit_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = None
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id), int(city_id)))
        res = c.fetchone(); district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch district name for edit: {e}")
    finally:
         if conn: conn.close()
    if not city_name or district_name is None:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    context.user_data["state"] = "awaiting_edit_district_name"
    context.user_data["edit_city_id"] = city_id
    context.user_data["edit_district_id"] = dist_id
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(f"✏️ Editing district: {district_name} in {city_name}\n\nPlease reply with the new name for this district:",
                           reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new district name in chat.")

async def handle_adm_remove_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = None
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id), int(city_id)))
        res = c.fetchone(); district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch district name for delete confirmation: {e}")
    finally:
        if conn: conn.close()
    if not city_name or district_name is None:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    context.user_data["confirm_action"] = f"remove_district|{city_id}|{dist_id}"
    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Are you sure you want to delete district: {district_name} from {city_name}?\n\n"
           f"🚨 This will permanently delete this district and all products listed within it!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete District", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=f"adm_manage_districts_city|{city_id}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Manage Products Handlers (Primary Admin Only) ---
async def handle_adm_manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not CITIES:
         return await query.edit_message_text("No cities configured. Add a city first.", parse_mode=None,
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]))
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id,''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_manage_products_city|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    await query.edit_message_text("🗑️ Manage Products\n\nSelect the city where the products are located:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_manage_products_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found.", parse_mode=None)
    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
         keyboard = [[InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_products")]]
         return await query.edit_message_text(f"No districts found for {city_name}. Cannot manage products.",
                                 reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda d_id: districts_in_city.get(d_id,''))
    keyboard = []
    for d in sorted_district_ids:
         dist_name = districts_in_city.get(d)
         if dist_name:
             keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=f"adm_manage_products_dist|{city_id}|{d}")])
         else: logger.warning(f"District name missing for ID {d} in city {city_id} (manage products)")
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data="adm_manage_products")])
    await query.edit_message_text(f"🗑️ Manage Products in {city_name}\n\nSelect district:",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_manage_products_dist(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    city_id, dist_id = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT product_type FROM products WHERE city = ? AND district = ? ORDER BY product_type", (city_name, district_name))
        product_types_in_dist = sorted([row['product_type'] for row in c.fetchall()])
        if not product_types_in_dist:
             keyboard = [[InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_manage_products_city|{city_id}")]]
             return await query.edit_message_text(f"No product types found in {city_name} / {district_name}.",
                                     reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        keyboard = []
        for pt in product_types_in_dist:
             emoji = PRODUCT_TYPES.get(pt, DEFAULT_PRODUCT_EMOJI)
             keyboard.append([InlineKeyboardButton(f"{emoji} {pt}", callback_data=f"adm_manage_products_type|{city_id}|{dist_id}|{pt}")])

        keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"adm_manage_products_city|{city_id}")])
        await query.edit_message_text(f"🗑️ Manage Products in {city_name} / {district_name}\n\nSelect product type:",
                                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error fetching product types for managing in {city_name}/{district_name}: {e}", exc_info=True)
        await query.edit_message_text("❌ Error fetching product types.", parse_mode=None)
    finally:
        if conn: conn.close()


async def handle_adm_manage_products_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params or len(params) < 3: return await query.answer("Error: Location/Type info missing.", show_alert=True)
    city_id, dist_id, p_type = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found.", parse_mode=None)

    type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, size, price, available, reserved, name
            FROM products WHERE city = ? AND district = ? AND product_type = ?
            ORDER BY size, price, id
        """, (city_name, district_name, p_type))
        products = c.fetchall()
        msg = f"🗑️ Products: {type_emoji} {p_type} in {city_name} / {district_name}\n\n"
        keyboard = []
        full_msg = msg

        if not products:
            full_msg += "No products of this type found here."
        else:
             header = "ID | Size | Price | Status (Avail/Reserved)\n" + "----------------------------------------\n"
             full_msg += header
             items_text_list = []
             for prod in products:
                prod_id, size_str, price_str = prod['id'], prod['size'], format_currency(prod['price'])
                status_str = f"{prod['available']}/{prod['reserved']}"
                items_text_list.append(f"{prod_id} | {size_str} | {price_str}€ | {status_str}")
                keyboard.append([InlineKeyboardButton(f"🗑️ Delete ID {prod_id}", callback_data=f"adm_delete_prod|{prod_id}")])
             full_msg += "\n".join(items_text_list)

        keyboard.append([InlineKeyboardButton("⬅️ Back to Types", callback_data=f"adm_manage_products_dist|{city_id}|{dist_id}")])
        try:
            await query.edit_message_text(full_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        except telegram_error.BadRequest as e:
             if "message is not modified" not in str(e).lower(): logger.error(f"Error editing manage products type: {e}.")
             else: await query.answer() 
    except sqlite3.Error as e:
        logger.error(f"DB error fetching products for deletion: {e}", exc_info=True)
        await query.edit_message_text("❌ Error fetching products.", parse_mode=None)
    finally:
        if conn: conn.close()


async def handle_adm_delete_prod(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Product ID missing.", show_alert=True)
    try: product_id = int(params[0])
    except ValueError: return await query.answer("Error: Invalid Product ID.", show_alert=True)
    product_name = f"Product ID {product_id}"
    product_details = ""
    back_callback = "adm_manage_products"
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT p.name, p.city, p.district, p.product_type, p.size, p.price, ci.id as city_id, di.id as dist_id
            FROM products p LEFT JOIN cities ci ON p.city = ci.name
            LEFT JOIN districts di ON p.district = di.name AND ci.id = di.city_id
            WHERE p.id = ?
        """, (product_id,))
        result = c.fetchone()
        if result:
            type_name = result['product_type']
            emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)
            product_name = result['name'] or product_name
            product_details = f"{emoji} {type_name} {result['size']} ({format_currency(result['price'])}€) in {result['city']}/{result['district']}"
            if result['city_id'] and result['dist_id'] and result['product_type']:
                back_callback = f"adm_manage_products_type|{result['city_id']}|{result['dist_id']}|{result['product_type']}"
            else: logger.warning(f"Could not retrieve full details for product {product_id} during delete confirmation.")
        else:
            return await query.edit_message_text("Error: Product not found.", parse_mode=None)
    except sqlite3.Error as e:
         logger.warning(f"Could not fetch full details for product {product_id} for delete confirmation: {e}")
    finally:
        if conn: conn.close()

    context.user_data["confirm_action"] = f"confirm_remove_product|{product_id}"
    msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to permanently delete this specific product instance?\n"
           f"Product ID: {product_id}\nDetails: {product_details}\n\n🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Product", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=back_callback)]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Manage Product Types Handlers (Primary Admin Only) ---
async def handle_adm_manage_types(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    load_all_data() 
    if not PRODUCT_TYPES: msg = "🧩 Manage Product Types\n\nNo product types configured."
    else: msg = "🧩 Manage Product Types\n\nSelect a type to edit or delete:"
    keyboard = []
    for type_name, emoji in sorted(PRODUCT_TYPES.items()):
         keyboard.append([
             InlineKeyboardButton(f"{emoji} {type_name}", callback_data=f"adm_edit_type_menu|{type_name}"),
             InlineKeyboardButton(f"🗑️ Delete", callback_data=f"adm_delete_type|{type_name}")
         ])
    keyboard.extend([
        [InlineKeyboardButton("➕ Add New Type", callback_data="adm_add_type")],
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
    ])
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# --- Edit Type Menu ---
async def handle_adm_edit_type_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    lang, lang_data = _get_lang_data(context)
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: Type name missing.", show_alert=True)

    type_name = params[0]
    current_emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)

    current_description = ""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT description FROM product_types WHERE name = ?", (type_name,))
        res = c.fetchone()
        if res: current_description = res['description'] or "(Description not set)"
        else: current_description = "(Type not found in DB)"
    except sqlite3.Error as e:
        logger.error(f"Error fetching description for type {type_name}: {e}")
        current_description = "(DB Error fetching description)"
    finally:
        if conn: conn.close()


    safe_name = type_name
    safe_desc = current_description

    msg_template = lang_data.get("admin_edit_type_menu", "🧩 Editing Type: {type_name}\n\nCurrent Emoji: {emoji}\nDescription: {description}\n\nWhat would you like to do?")
    msg = msg_template.format(type_name=safe_name, emoji=current_emoji, description=safe_desc)

    change_emoji_button_text = lang_data.get("admin_edit_type_emoji_button", "✏️ Change Emoji")

    keyboard = [
        [InlineKeyboardButton(change_emoji_button_text, callback_data=f"adm_change_type_emoji|{type_name}")],
        [InlineKeyboardButton(f"🗑️ Delete {type_name}", callback_data=f"adm_delete_type|{type_name}")],
        [InlineKeyboardButton("⬅️ Back to Types", callback_data="adm_manage_types")]
    ]

    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" in str(e).lower(): await query.answer()
        else:
            logger.error(f"Error editing type menu: {e}. Message: {msg}")
            await query.answer("Error displaying menu.", show_alert=True)

# --- Change Type Emoji Prompt ---
async def handle_adm_change_type_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    lang, lang_data = _get_lang_data(context)
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Type name missing.", show_alert=True)
    type_name = params[0]

    context.user_data["state"] = "awaiting_edit_type_emoji"
    context.user_data["edit_type_name"] = type_name
    current_emoji = PRODUCT_TYPES.get(type_name, DEFAULT_PRODUCT_EMOJI)

    prompt_text = lang_data.get("admin_enter_type_emoji", "✍️ Please reply with a single emoji for the product type:")
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm_edit_type_menu|{type_name}")]]
    await query.edit_message_text(f"Current Emoji: {current_emoji}\n\n{prompt_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter new emoji in chat.")

# --- Add Type asks for name first ---
async def handle_adm_add_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    context.user_data["state"] = "awaiting_new_type_name"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_types")]]
    await query.edit_message_text("🧩 Please reply with the name for the new product type:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter type name in chat.")

async def handle_adm_delete_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    if not params: return await query.answer("Error: Type name missing.", show_alert=True)
    type_name_to_delete = params[0]
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products WHERE product_type = ?", (type_name_to_delete,))
        product_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM reseller_discounts WHERE product_type = ?", (type_name_to_delete,))
        reseller_discount_count = c.fetchone()[0]

        if product_count > 0 or reseller_discount_count > 0:
            error_msg_parts = []
            if product_count > 0: error_msg_parts.append(f"{product_count} product(s)")
            if reseller_discount_count > 0: error_msg_parts.append(f"{reseller_discount_count} reseller discount rule(s)")
            usage_details = " and ".join(error_msg_parts)
            context.user_data['force_delete_type_name'] = type_name_to_delete
            force_delete_msg = (
                f"⚠️ Type '{type_name_to_delete}' is currently used by {usage_details}.\n\n"
                f"You can 'Force Delete' to remove this type AND all associated products/discount rules.\n\n"
                f"🚨 THIS IS IRREVERSIBLE AND WILL DELETE THE LISTED ITEMS."
            )
            keyboard = [
                [InlineKeyboardButton(f"💣 Force Delete Type & {usage_details}", callback_data="confirm_force_delete_prompt")],
                [InlineKeyboardButton("⬅️ Back to Manage Types", callback_data="adm_manage_types")]
            ]
            await query.edit_message_text(force_delete_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        else:
            context.user_data["confirm_action"] = f"delete_type|{type_name_to_delete}"
            msg = (f"⚠️ Confirm Deletion\n\nAre you sure you want to delete product type: {type_name_to_delete}?\n\n"
                   f"🚨 This action is irreversible!")
            keyboard = [[InlineKeyboardButton("✅ Yes, Delete Type", callback_data="confirm_yes"),
                         InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_types")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error checking product type usage for '{type_name_to_delete}': {e}", exc_info=True)
        await query.edit_message_text("❌ Error checking type usage.", parse_mode=None)
    finally:
        if conn: conn.close()

async def handle_confirm_force_delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    type_name = context.user_data.get('force_delete_type_name')
    if not type_name:
        logger.error("handle_confirm_force_delete_prompt: force_delete_type_name not found in user_data.")
        await query.edit_message_text("Error: Could not retrieve type name for force delete. Please try again.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm_manage_types")]]))
        return
    context.user_data["confirm_action"] = f"force_delete_type_CASCADE|{type_name}"
    product_count = 0; reseller_discount_count = 0; conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM products WHERE product_type = ?", (type_name,))
        product_count_res = c.fetchone();
        if product_count_res: product_count = product_count_res[0]
        c.execute("SELECT COUNT(*) FROM reseller_discounts WHERE product_type = ?", (type_name,))
        reseller_discount_count_res = c.fetchone()
        if reseller_discount_count_res: reseller_discount_count = reseller_discount_count_res[0]
    except sqlite3.Error as e:
        logger.error(f"DB error fetching counts for force delete confirmation of '{type_name}': {e}")
        await query.edit_message_text("Error fetching item counts for confirmation. Cannot proceed.", parse_mode=None)
        return
    finally:
        if conn: conn.close()
    usage_details_parts = []
    if product_count > 0: usage_details_parts.append(f"{product_count} product(s)")
    if reseller_discount_count > 0: usage_details_parts.append(f"{reseller_discount_count} reseller discount rule(s)")
    usage_details = " and ".join(usage_details_parts) if usage_details_parts else "associated items"
    msg = (f"🚨🚨🚨 FINAL CONFIRMATION 🚨🚨🚨\n\n"
           f"Are you ABSOLUTELY SURE you want to delete product type '{type_name}'?\n\n"
           f"This will also PERMANENTLY DELETE:\n"
           f"  • All {usage_details} linked to this type.\n"
           f"  • All media associated with those products.\n\n"
           f"THIS ACTION CANNOT BE UNDONE AND WILL RESULT IN DATA LOSS.")
    keyboard = [[InlineKeyboardButton("✅ YES, I understand, DELETE ALL", callback_data="confirm_yes")],
                 [InlineKeyboardButton("❌ NO, Cancel Force Delete", callback_data="adm_manage_types")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# --- Set Bot Media Handlers ---
async def handle_adm_set_media(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    lang, lang_data = _get_lang_data(context)
    set_media_prompt_text = lang_data.get("set_media_prompt_plain", "Send a photo, video, or GIF to display above all messages:")
    context.user_data["state"] = "awaiting_bot_media"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]
    await query.edit_message_text(set_media_prompt_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send photo, video, or GIF.")

async def handle_adm_clear_reservations_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access denied.", show_alert=True)
    context.user_data["confirm_action"] = "clear_all_reservations"
    msg = (f"⚠️ Confirm Action: Clear All Reservations\n\n"
           f"Are you sure you want to clear ALL product reservations and empty ALL user baskets?\n\n"
           f"🚨 This action cannot be undone and will affect all users!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Clear Reservations", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="admin_menu")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Admin Message Handlers (Used when state is set) ---
async def handle_adm_add_city_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_city_name": return
    text = update.message.text.strip()
    if not text: return await send_message_with_retry(context.bot, chat_id, "City name cannot be empty.", parse_mode=None)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO cities (name) VALUES (?)", (text,))
        new_city_id = c.lastrowid
        conn.commit()
        load_all_data()
        context.user_data.pop("state", None)
        success_text = f"✅ City '{text}' added successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: City '{text}' already exists.", parse_mode=None)
    except sqlite3.Error as e:
        logger.error(f"DB error adding city '{text}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add city.", parse_mode=None)
        context.user_data.pop("state", None)
    finally:
        if conn: conn.close()

async def handle_adm_add_district_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_district_name": return
    text = update.message.text.strip()
    city_id_str = context.user_data.get("admin_add_district_city_id")
    city_name = CITIES.get(city_id_str)
    if not city_id_str or not city_name:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not determine city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
        return
    if not text: return await send_message_with_retry(context.bot, chat_id, "District name cannot be empty.", parse_mode=None)
    conn = None
    try:
        city_id_int = int(city_id_str)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO districts (city_id, name) VALUES (?, ?)", (city_id_int, text))
        conn.commit()
        load_all_data()
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
        success_text = f"✅ District '{text}' added to {city_name}!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: District '{text}' already exists in {city_name}.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error adding district '{text}' to city {city_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add district.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("admin_add_district_city_id", None)
    finally:
        if conn: conn.close()

async def handle_adm_edit_district_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_edit_district_name": return
    new_name = update.message.text.strip()
    city_id_str = context.user_data.get("edit_city_id")
    dist_id_str = context.user_data.get("edit_district_id")
    city_name = CITIES.get(city_id_str)
    old_district_name = None
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM districts WHERE id = ? AND city_id = ?", (int(dist_id_str), int(city_id_str)))
        res = c.fetchone(); old_district_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch old district name for edit: {e}")
    finally:
        if conn: conn.close()
    if not city_id_str or not dist_id_str or not city_name or old_district_name is None:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not find district/city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        return
    if not new_name: return await send_message_with_retry(context.bot, chat_id, "New district name cannot be empty.", parse_mode=None)
    if new_name == old_district_name:
        await send_message_with_retry(context.bot, chat_id, "New name is the same. No changes.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        return await send_message_with_retry(context.bot, chat_id, "No changes detected.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    conn = None
    try:
        city_id_int, dist_id_int = int(city_id_str), int(dist_id_str)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("UPDATE districts SET name = ? WHERE id = ? AND city_id = ?", (new_name, dist_id_int, city_id_int))
        c.execute("UPDATE products SET district = ? WHERE district = ? AND city = ?", (new_name, old_district_name, city_name))
        conn.commit()
        load_all_data()
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
        success_text = f"✅ District updated to '{new_name}' successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Districts", callback_data=f"adm_manage_districts_city|{city_id_str}")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: District '{new_name}' already exists.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error updating district {dist_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to update district.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None); context.user_data.pop("edit_district_id", None)
    finally:
         if conn: conn.close()


async def handle_adm_edit_city_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_edit_city_name": return
    new_name = update.message.text.strip()
    city_id_str = context.user_data.get("edit_city_id")
    old_name = None
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT name FROM cities WHERE id = ?", (int(city_id_str),))
        res = c.fetchone(); old_name = res['name'] if res else None
    except (sqlite3.Error, ValueError) as e: logger.error(f"Failed to fetch old city name for edit: {e}")
    finally:
        if conn: conn.close()
    if not city_id_str or old_name is None:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Could not find city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        return
    if not new_name: return await send_message_with_retry(context.bot, chat_id, "New city name cannot be empty.", parse_mode=None)
    if new_name == old_name:
        await send_message_with_retry(context.bot, chat_id, "New name is the same. No changes.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        return await send_message_with_retry(context.bot, chat_id, "No changes detected.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    conn = None
    try:
        city_id_int = int(city_id_str)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("UPDATE cities SET name = ? WHERE id = ?", (new_name, city_id_int))
        c.execute("UPDATE products SET city = ? WHERE city = ?", (new_name, old_name))
        conn.commit()
        load_all_data()
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
        success_text = f"✅ City updated to '{new_name}' successfully!"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Cities", callback_data="adm_manage_cities")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: City '{new_name}' already exists.", parse_mode=None)
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"DB/Value error updating city {city_id_str}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to update city.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_city_id", None)
    finally:
         if conn: conn.close()


async def handle_adm_custom_size_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_check = update.effective_user.id 
    chat_id = update.effective_chat.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS: return 

    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_custom_size": return
    custom_size = update.message.text.strip()
    if not custom_size: return await send_message_with_retry(context.bot, chat_id, "Custom size cannot be empty.", parse_mode=None)
    if len(custom_size) > 50: return await send_message_with_retry(context.bot, chat_id, "Custom size too long (max 50 chars).", parse_mode=None)
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type"]):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        context.user_data.pop("state", None)
        return
    context.user_data["pending_drop_size"] = custom_size
    context.user_data["state"] = "awaiting_price"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    await send_message_with_retry(context.bot, chat_id, f"Custom size set to '{custom_size}'. Reply with the price (e.g., 12.50):",
                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_price_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_check = update.effective_user.id 
    chat_id = update.effective_chat.id
    if user_id_check != ADMIN_ID and user_id_check not in SECONDARY_ADMIN_IDS: return 

    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_price": return
    price_text = update.message.text.strip().replace(',', '.')
    try:
        price = round(float(price_text), 2)
        if price <= 0: raise ValueError("Price must be positive")
    except ValueError:
        return await send_message_with_retry(context.bot, chat_id, "❌ Invalid Price Format. Enter positive number (e.g., 12.50):", parse_mode=None)
    if not all(k in context.user_data for k in ["admin_city", "admin_district", "admin_product_type", "pending_drop_size"]):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("pending_drop_size", None)
        return
    context.user_data["pending_drop_price"] = price
    context.user_data["state"] = "awaiting_drop_details"
    keyboard = [[InlineKeyboardButton("❌ Cancel Add", callback_data="cancel_add")]]
    price_f = format_currency(price)
    await send_message_with_retry(context.bot, chat_id,
                                  f"Price set to {price_f} EUR. Now send drop details:\n"
                                  f"- Send text only, OR\n"
                                  f"- Send photo(s)/video(s) WITH text caption, OR\n"
                                  f"- Forward a message containing media and text.",
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_bot_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID: return 
    if not update.message: return
    if context.user_data.get("state") != "awaiting_bot_media": return

    new_media_type, file_to_download, file_extension, file_id = None, None, None, None
    if update.message.photo: file_to_download, new_media_type, file_extension, file_id = update.message.photo[-1], "photo", ".jpg", update.message.photo[-1].file_id
    elif update.message.video: file_to_download, new_media_type, file_extension, file_id = update.message.video, "video", ".mp4", update.message.video.file_id
    elif update.message.animation: file_to_download, new_media_type, file_extension, file_id = update.message.animation, "gif", ".mp4", update.message.animation.file_id
    elif update.message.document and update.message.document.mime_type and 'gif' in update.message.document.mime_type.lower():
         file_to_download, new_media_type, file_extension, file_id = update.message.document, "gif", ".gif", update.message.document.file_id
    else: return await send_message_with_retry(context.bot, chat_id, "❌ Invalid Media Type. Send photo, video, or GIF.", parse_mode=None)
    if not file_to_download or not file_id: return await send_message_with_retry(context.bot, chat_id, "❌ Could not identify media file.", parse_mode=None)

    context.user_data.pop("state", None)
    await send_message_with_retry(context.bot, chat_id, "⏳ Downloading and saving new media...", parse_mode=None)

    final_media_path = os.path.join(MEDIA_DIR, f"bot_media{file_extension}")
    temp_download_path = final_media_path + ".tmp"

    try:
        logger.info(f"Downloading new bot media ({new_media_type}) ID {file_id} to {temp_download_path}")
        file_obj = await context.bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=temp_download_path)
        logger.info("Media download successful to temp path.")

        if not await asyncio.to_thread(os.path.exists, temp_download_path) or await asyncio.to_thread(os.path.getsize, temp_download_path) == 0:
             raise IOError("Downloaded file is empty or missing.")

        old_media_path_global = BOT_MEDIA.get("path")
        if old_media_path_global and old_media_path_global != final_media_path and await asyncio.to_thread(os.path.exists, old_media_path_global):
            try:
                await asyncio.to_thread(os.remove, old_media_path_global)
                logger.info(f"Removed old bot media file: {old_media_path_global}")
            except OSError as e:
                logger.warning(f"Could not remove old bot media file '{old_media_path_global}': {e}")

        await asyncio.to_thread(shutil.move, temp_download_path, final_media_path)
        logger.info(f"Moved media to final path: {final_media_path}")

        BOT_MEDIA["type"] = new_media_type
        BOT_MEDIA["path"] = final_media_path

        try:
            def write_json_sync(path, data):
                try:
                    with open(path, 'w') as f:
                        json.dump(data, f, indent=4)
                    logger.info(f"Successfully wrote updated BOT_MEDIA to {path}: {data}")
                    return True
                except Exception as e_sync:
                    logger.error(f"Failed during synchronous write to {path}: {e_sync}")
                    return False

            write_successful = await asyncio.to_thread(write_json_sync, BOT_MEDIA_JSON_PATH, BOT_MEDIA)

            if not write_successful:
                raise IOError(f"Failed to write bot media configuration to {BOT_MEDIA_JSON_PATH}")

        except Exception as e:
            logger.error(f"Error during bot media JSON writing process: {e}")
            await send_message_with_retry(context.bot, chat_id, f"❌ Error saving media configuration: {e}", parse_mode=None)
            if await asyncio.to_thread(os.path.exists, final_media_path):
                 try: await asyncio.to_thread(os.remove, final_media_path)
                 except OSError: pass
            return

        await send_message_with_retry(context.bot, chat_id, "✅ Bot Media Updated Successfully!", parse_mode=None)
        keyboard = [[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]
        await send_message_with_retry(context.bot, chat_id, "Changes applied.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except (telegram_error.TelegramError, IOError, OSError) as e:
        logger.error(f"Error downloading/saving bot media: {e}")
        await send_message_with_retry(context.bot, chat_id, "❌ Error downloading or saving media. Please try again.", parse_mode=None)
        if await asyncio.to_thread(os.path.exists, temp_download_path):
            try: await asyncio.to_thread(os.remove, temp_download_path)
            except OSError: pass
    except Exception as e:
        logger.error(f"Unexpected error updating bot media: {e}", exc_info=True)
        await send_message_with_retry(context.bot, chat_id, "❌ An unexpected error occurred.", parse_mode=None)
    finally:
        if 'temp_download_path' in locals() and await asyncio.to_thread(os.path.exists, temp_download_path):
             try: await asyncio.to_thread(os.remove, temp_download_path)
             except OSError as e: logger.warning(f"Could not remove temp dl file '{temp_download_path}': {e}")


# --- Add Product Type Handlers (Primary Admin Only) ---
async def handle_adm_add_type_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lang, lang_data = _get_lang_data(context)

    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_type_name": return

    type_name = update.message.text.strip()
    if not type_name: return await send_message_with_retry(context.bot, chat_id, "Product type name cannot be empty.", parse_mode=None)
    if len(type_name) > 100: return await send_message_with_retry(context.bot, chat_id, "Product type name too long (max 100 chars).", parse_mode=None)
    if type_name.lower() in [pt.lower() for pt in PRODUCT_TYPES.keys()]:
        return await send_message_with_retry(context.bot, chat_id, f"❌ Error: Type '{type_name}' already exists.", parse_mode=None)

    context.user_data["new_type_name"] = type_name
    context.user_data["state"] = "awaiting_new_type_emoji"
    prompt_text = lang_data.get("admin_enter_type_emoji", "✍️ Please reply with a single emoji for the product type:")
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="adm_manage_types")]]
    await send_message_with_retry(context.bot, chat_id, f"Type name set to: {type_name}\n\n{prompt_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_add_type_emoji_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lang, lang_data = _get_lang_data(context)

    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_new_type_emoji": return

    emoji = update.message.text.strip()
    type_name = context.user_data.get("new_type_name")

    if not type_name:
        logger.error(f"State is awaiting_new_type_emoji but new_type_name missing for user {user_id}")
        context.user_data.pop("state", None)
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start adding the type again.", parse_mode=None)
        return

    is_likely_emoji = len(emoji) == 1 and ord(emoji) > 256
    if not is_likely_emoji:
        invalid_emoji_msg = lang_data.get("admin_invalid_emoji", "❌ Invalid input. Please send a single emoji.")
        await send_message_with_retry(context.bot, chat_id, invalid_emoji_msg, parse_mode=None)
        return

    conn=None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO product_types (name, emoji) VALUES (?, ?)", (type_name, emoji))
        conn.commit()
        load_all_data()
        context.user_data.pop("state", None)
        context.user_data.pop("new_type_name", None)

        emoji_set_msg = lang_data.get("admin_type_emoji_set", "Emoji set to {emoji}.")
        success_text = f"✅ Product Type '{type_name}' added!\n{emoji_set_msg.format(emoji=emoji)}"
        keyboard = [[InlineKeyboardButton("⬅️ Manage Types", callback_data="adm_manage_types")]]
        await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    except sqlite3.IntegrityError:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: Product type '{type_name}' already exists.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_type_name", None)
    except sqlite3.Error as e:
        logger.error(f"DB error adding product type '{type_name}' with emoji '{emoji}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to add type.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("new_type_name", None)
    finally:
        if conn: conn.close()

async def handle_adm_edit_type_emoji_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lang, lang_data = _get_lang_data(context)

    if user_id != ADMIN_ID: return
    if not update.message or not update.message.text: return
    if context.user_data.get("state") != "awaiting_edit_type_emoji": return

    new_emoji = update.message.text.strip()
    type_name = context.user_data.get("edit_type_name")

    if not type_name:
        logger.error(f"State is awaiting_edit_type_emoji but edit_type_name missing for user {user_id}")
        context.user_data.pop("state", None)
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start editing the type again.", parse_mode=None)
        return

    is_likely_emoji = len(new_emoji) == 1 and ord(new_emoji) > 256
    if not is_likely_emoji:
        invalid_emoji_msg = lang_data.get("admin_invalid_emoji", "❌ Invalid input. Please send a single emoji.")
        await send_message_with_retry(context.bot, chat_id, invalid_emoji_msg, parse_mode=None)
        return

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        update_result = c.execute("UPDATE product_types SET emoji = ? WHERE name = ?", (new_emoji, type_name))
        conn.commit()

        if update_result.rowcount == 0:
            logger.warning(f"Attempted to update emoji for non-existent type: {type_name}")
            await send_message_with_retry(context.bot, chat_id, f"❌ Error: Type '{type_name}' not found.", parse_mode=None)
        else:
            load_all_data()
            success_msg_template = lang_data.get("admin_type_emoji_updated", "✅ Emoji updated successfully for {type_name}!")
            success_text = success_msg_template.format(type_name=type_name) + f" New emoji: {new_emoji}"
            keyboard = [[InlineKeyboardButton("⬅️ Manage Types", callback_data="adm_manage_types")]]
            await send_message_with_retry(context.bot, chat_id, success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

        context.user_data.pop("state", None)
        context.user_data.pop("edit_type_name", None)

    except sqlite3.Error as e:
        logger.error(f"DB error updating emoji for type '{type_name}': {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Failed to update emoji.", parse_mode=None)
        context.user_data.pop("state", None); context.user_data.pop("edit_type_name", None)
    finally:
        if conn: conn.close()


# --- New Handlers for Reassign Product Type (Primary Admin Only) ---
async def handle_adm_reassign_type_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)

    context.user_data['state'] = 'awaiting_reassign_old_type_name'
    prompt_msg = ("🔄 Reassign Product Type\n\n"
                    "Please reply with the EXACT name of the product type you want to reassign (the old/long one):")
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]
    await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Enter old product type name.")

async def _ask_for_reassign_new_type_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    old_type_name = context.user_data.get('reassign_old_type_name')

    if not old_type_name:
        await send_message_with_retry(context.bot, chat_id, "Error: Old type name missing from context.", parse_mode=None)
        context.user_data.pop('state', None)
        return

    context.user_data['state'] = 'awaiting_reassign_new_type_name'
    prompt_msg = (f"Old type: '{old_type_name}'\n\n"
                    "Now, please reply with the EXACT name of an EXISTING product type to reassign to (the new/shorter one).\n"
                    "Ensure the new type already exists in 'Manage Product Types'.")
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="admin_menu")]]
    await send_message_with_retry(context.bot, chat_id, prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_reassign_old_type_name_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID or context.user_data.get("state") != 'awaiting_reassign_old_type_name': return
    if not update.message or not update.message.text: return

    old_type_name = update.message.text.strip()
    load_all_data()

    if not old_type_name:
        await send_message_with_retry(context.bot, chat_id, "Old product type name cannot be empty. Please try again.", parse_mode=None)
        return
    if old_type_name not in PRODUCT_TYPES:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: Product type '{old_type_name}' not found. Please check 'Manage Product Types' and try again.", parse_mode=None)
        return

    context.user_data['reassign_old_type_name'] = old_type_name
    await _ask_for_reassign_new_type_name(update, context)


async def handle_adm_reassign_new_type_name_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id != ADMIN_ID or context.user_data.get("state") != 'awaiting_reassign_new_type_name': return
    if not update.message or not update.message.text: return

    new_type_name = update.message.text.strip()
    old_type_name = context.user_data.get('reassign_old_type_name')
    load_all_data()

    if not old_type_name:
        await send_message_with_retry(context.bot, chat_id, "Error: Old type name missing. Please start over.", parse_mode=None)
        context.user_data.pop('state', None); context.user_data.pop('reassign_old_type_name', None)
        return

    if not new_type_name:
        await send_message_with_retry(context.bot, chat_id, "New product type name cannot be empty. Please try again.", parse_mode=None)
        return
    if new_type_name not in PRODUCT_TYPES:
        await send_message_with_retry(context.bot, chat_id, f"❌ Error: New product type '{new_type_name}' not found. Please create it first via 'Manage Product Types' and try again.", parse_mode=None)
        return
    if new_type_name == old_type_name:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: New product type name cannot be the same as the old one. Please enter a different new type name.", parse_mode=None)
        return

    context.user_data.pop('state', None)
    context.user_data["confirm_action"] = f"confirm_reassign_type|{old_type_name}|{new_type_name}"

    msg = (f"⚠️ Confirm Reassignment\n\n"
            f"Old Type: '{old_type_name}'\n"
            f"New Type: '{new_type_name}'\n\n"
            f"Are you sure you want to reassign ALL products and reseller discount rules from '{old_type_name}' to '{new_type_name}', and then permanently DELETE the type '{old_type_name}'?\n\n"
            f"🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Reassign & Delete", callback_data="confirm_yes")],
                    [InlineKeyboardButton("❌ No, Cancel", callback_data="adm_manage_types")]]
    await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# --- Bulk Product Add Handlers (Primary Admin Only) ---
async def handle_adm_bulk_start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Starts the bulk product adding flow - Step 1: City Selection."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    
    context.user_data['bulk_common_details'] = {}
    context.user_data['bulk_items_added_count'] = 0
    context.user_data['bulk_flow_step'] = 'city' # Used by shared city/dist/type handlers to adjust back buttons
    
    lang, lang_data = _get_lang_data(context)
    if not CITIES:
        return await query.edit_message_text("No cities configured. Please add a city first via 'Manage Cities'.", 
                                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]]),
                                             parse_mode=None)
    
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"adm_bulk_city_chosen|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Cancel Bulk Add", callback_data="admin_menu")])
    await query.edit_message_text("📦 Bulk Add Products: Step 1 - Select City", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_bulk_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow - Step 2: District Selection."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params: return await query.answer("Error: City ID missing.", show_alert=True)
    
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found. Please select again.", 
                                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to City Select (Bulk)", callback_data="adm_bulk_start_setup")]]),
                                             parse_mode=None)

    context.user_data['bulk_common_details']['city_id'] = city_id
    context.user_data['bulk_common_details']['city_name'] = city_name
    context.user_data['bulk_flow_step'] = 'district' 

    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back to City Select (Bulk)", callback_data="adm_bulk_start_setup")]]
        return await query.edit_message_text(f"No districts found for {city_name}. Cannot proceed with bulk add.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
    keyboard = [[InlineKeyboardButton(f"🏘️ {districts_in_city.get(d)}", callback_data=f"adm_bulk_district_chosen|{city_id}|{d}")] for d in sorted_district_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to City Select (Bulk)", callback_data="adm_bulk_start_setup")])
    await query.edit_message_text(f"📦 Bulk Add Products: Step 2 - Select District for {city_name}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_adm_bulk_district_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow - Step 3: Ask detail definition method (Existing Combo or Manual)."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)

    city_id, dist_id = params
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    city_name = context.user_data.get('bulk_common_details', {}).get('city_name', CITIES.get(city_id, "Selected City")) 
    
    if not district_name: 
        await query.edit_message_text("Error: District context lost. Please start over.", 
                                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Bulk Add", callback_data="admin_menu")]]),
                                             parse_mode=None)
        return

    context.user_data['bulk_common_details']['district_id'] = dist_id
    context.user_data['bulk_common_details']['district_name'] = district_name
    context.user_data['bulk_flow_step'] = 'detail_method' 

    msg = (f"📦 Bulk Add in {city_name} / {district_name}\n\n"
           "Step 3: How do you want to define product Type, Size, and Price for this bulk session?")
    keyboard = [
        [InlineKeyboardButton("📋 Use Existing Product Combo", callback_data=f"adm_bulk_select_existing_type_start|{city_id}|{dist_id}")],
        [InlineKeyboardButton("✍️ Set Details Manually", callback_data=f"adm_bulk_manual_type_select|{city_id}|{dist_id}")],
        [InlineKeyboardButton("⬅️ Back to District Select (Bulk)", callback_data=f"adm_bulk_city_chosen|{city_id}")],
        [InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]
    ]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_adm_bulk_ask_detail_method(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): # Defined as requested
    """This function is called after district selection in bulk flow to ask user how they want to define details."""
    # This function's logic is now integrated into handle_adm_bulk_district_chosen.
    # For safety, if it's called directly, we can just re-show the options.
    # However, the callback "adm_bulk_district_chosen" now directly leads to this choice.
    # This function can act as a redundant entry point or be removed if handle_adm_bulk_district_chosen is always the primary path.
    # For now, let it call the logic.
    await handle_adm_bulk_district_chosen(update, context, params)

async def handle_adm_bulk_manual_type_select(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow (Manual Path) - Step 3a: Product Type Selection."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)
    
    city_id, dist_id = params
    if 'city_name' not in context.user_data.get('bulk_common_details', {}) or \
       'district_name' not in context.user_data.get('bulk_common_details', {}):
        # This check is good, ensures context from previous step (handle_adm_bulk_ask_detail_method) is present
        await query.edit_message_text("Error: City/District context lost for manual type selection. Please start over.", 
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Bulk Add", callback_data="admin_menu")]]))
        return
        
    context.user_data['bulk_flow_step'] = 'type' # To guide handle_adm_type for bulk_manual
    
    lang, lang_data = _get_lang_data(context)
    select_type_text = lang_data.get("admin_select_type", "Select Product Type:")
    if not PRODUCT_TYPES:
        return await query.edit_message_text("No product types configured. Add types via 'Manage Product Types'.", parse_mode=None)

    keyboard = []
    for type_name_iter, emoji in sorted(PRODUCT_TYPES.items()):
        # Next callback will be adm_bulk_type_chosen
        callback_data_type = f"adm_bulk_type_chosen|{city_id}|{dist_id}|{type_name_iter}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name_iter}", callback_data=callback_data_type)])
    
    # Back button for bulk manual type selection goes to the method choice
    back_callback_type = f"adm_bulk_ask_detail_method|{city_id}|{dist_id}"
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=back_callback_type)])
    await query.edit_message_text(f"📦 Bulk Add (Manual): Step 3a - {select_type_text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


async def handle_adm_bulk_select_existing_type_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow (Existing Combo Path) - List existing product types in the location."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2: return await query.answer("Error: City/District ID missing.", show_alert=True)

    city_id, dist_id = params
    bulk_details = context.user_data.get('bulk_common_details', {})
    city_name = bulk_details.get('city_name', CITIES.get(city_id)) 
    district_name = bulk_details.get('district_name', DISTRICTS.get(city_id, {}).get(dist_id))

    if not city_name or not district_name:
        await query.edit_message_text("Error: City/District context lost. Please start over.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Bulk Add", callback_data="admin_menu")]]))
        return
    
    context.user_data['bulk_common_details']['city_id'] = city_id
    context.user_data['bulk_common_details']['city_name'] = city_name
    context.user_data['bulk_common_details']['district_id'] = dist_id
    context.user_data['bulk_common_details']['district_name'] = district_name
    context.user_data['bulk_flow_step'] = 'existing_type_selection'

    conn = None; available_types_in_location = []
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT product_type FROM products WHERE city = ? AND district = ? AND (available > 0 OR reserved > 0) ORDER BY product_type", 
                  (city_name, district_name))
        available_types_in_location = [row['product_type'] for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"DB error fetching existing types for bulk add in {city_name}/{district_name}: {e}")
        await query.edit_message_text("Error fetching product types. Please try again.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    if not available_types_in_location:
        msg = f"No existing product types found for sale in {city_name}/{district_name}.\n\nTry setting details manually?"
        keyboard = [
            [InlineKeyboardButton("✍️ Set Details Manually", callback_data=f"adm_bulk_manual_type_select|{city_id}|{dist_id}")],
            [InlineKeyboardButton("⬅️ Back to Bulk Options", callback_data=f"adm_bulk_ask_detail_method|{city_id}|{dist_id}")],
            [InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        return

    keyboard_buttons = []
    for p_type_name in available_types_in_location:
        emoji = PRODUCT_TYPES.get(p_type_name, DEFAULT_PRODUCT_EMOJI)
        keyboard_buttons.append([InlineKeyboardButton(f"{emoji} {p_type_name}", callback_data=f"adm_bulk_select_existing_combo_start|{p_type_name}")])
    
    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back to Bulk Options", callback_data=f"adm_bulk_ask_detail_method|{city_id}|{dist_id}")])
    await query.edit_message_text(f"📦 Bulk Add: Select Existing Product Type in {city_name}/{district_name}",
                                  reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=None)

async def handle_adm_bulk_select_existing_combo_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow (Existing Combo Path) - List existing size/price combos for the chosen type."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 1 : 
        return await query.answer("Error: Product type missing for combo selection.", show_alert=True)

    p_type_name = params[0]
    bulk_details = context.user_data.get('bulk_common_details', {})
    city_name = bulk_details.get('city_name')
    district_name = bulk_details.get('district_name')
    city_id = bulk_details.get('city_id') 
    dist_id = bulk_details.get('district_id')

    if not city_name or not district_name or not city_id or not dist_id:
        await query.edit_message_text("Error: City/District context lost for bulk combo selection. Please start over.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel Bulk Add", callback_data="admin_menu")]]))
        return

    conn = None; existing_combos = []
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT DISTINCT size, price 
            FROM products 
            WHERE city = ? AND district = ? AND product_type = ? AND (available > 0 OR reserved > 0)
            ORDER BY price, size
        """, (city_name, district_name, p_type_name))
        existing_combos = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching existing combos for {p_type_name} in {city_name}/{district_name}: {e}")
        await query.edit_message_text("Error fetching product combinations. Please try again.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    if not existing_combos:
        msg = f"No existing Size/Price combinations found for {p_type_name} in {city_name}/{district_name}.\n\nTry setting details manually?"
        keyboard = [
            [InlineKeyboardButton("✍️ Set Details Manually", callback_data=f"adm_bulk_manual_type_select|{city_id}|{dist_id}")],
            [InlineKeyboardButton("⬅️ Back to Select Type (Bulk)", callback_data=f"adm_bulk_select_existing_type_start|{city_id}|{dist_id}")],
             [InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]
        ]
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        return

    keyboard_buttons = []
    type_emoji = PRODUCT_TYPES.get(p_type_name, DEFAULT_PRODUCT_EMOJI)
    for combo in existing_combos:
        size, price_val_decimal = combo['size'], Decimal(str(combo['price']))
        price_str_for_button = format_currency(price_val_decimal)
        callback_data_combo = f"adm_bulk_apply_existing_combo|{p_type_name}|{size}|{price_val_decimal:.2f}" 
        keyboard_buttons.append([InlineKeyboardButton(f"{type_emoji} {size} - {price_str_for_button} EUR", callback_data=callback_data_combo)])
    
    keyboard_buttons.append([InlineKeyboardButton("⬅️ Back to Select Type (Bulk)", callback_data=f"adm_bulk_select_existing_type_start|{city_id}|{dist_id}")])
    await query.edit_message_text(f"📦 Bulk Add: Select Existing Combination for {p_type_name} in {city_name}/{district_name}",
                                  reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode=None)

async def handle_adm_bulk_apply_existing_combo(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow (Existing Combo Path) - Apply combo and ask for forwarded drops."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 3:
        return await query.answer("Error: Incomplete combo data.", show_alert=True)

    p_type, size, price_str = params
    try:
        price = Decimal(price_str) 
    except Exception:
        logger.error(f"Invalid price string '{price_str}' in apply_existing_combo.")
        await query.answer("Error: Invalid price data in selection.", show_alert=True)
        return

    context.user_data['bulk_common_details']['product_type'] = p_type
    context.user_data['bulk_common_details']['size'] = size
    context.user_data['bulk_common_details']['price'] = float(price) 

    context.user_data['bulk_items_added_count'] = 0 
    context.user_data['state'] = 'awaiting_bulk_forwarded_drops'
    context.user_data.pop('bulk_flow_step', None) 

    common_details = context.user_data['bulk_common_details']
    setup_summary = (
        f"📦 Bulk Add Setup Complete (using existing combo):\n"
        f"🏙️ City: {common_details['city_name']}\n"
        f"🏘️ District: {common_details['district_name']}\n"
        f"🧩 Type: {common_details['product_type']}\n"
        f"📏 Size: {common_details['size']}\n"
        f"💰 Price: {format_currency(common_details['price'])} EUR\n\n"
        f"Step 6: Now forward up to {BULK_ADD_LIMIT} messages. Each message should contain media (photo/video/GIF) and a text caption for details.\n\n"
        f"Type /done_bulk when finished or after reaching the limit."
    )
    await query.edit_message_text(setup_summary, 
                                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]]), 
                                   parse_mode=None)
    await query.answer("Details set. Forward product messages.")


# --- Bulk Add Flow: Message Handlers for Manual Size, Price, and Forwarded Drops ---
async def handle_adm_bulk_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Bulk Add Flow (Manual Path) - Type selected, ask for Size."""
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 3: return await query.answer("Error: Location/Type info missing for bulk type chosen.", show_alert=True)
    
    city_id, dist_id, p_type = params
    if 'bulk_common_details' not in context.user_data:
        context.user_data['bulk_common_details'] = {} 
    context.user_data['bulk_common_details']['city_id'] = city_id
    context.user_data['bulk_common_details']['city_name'] = CITIES.get(city_id)
    context.user_data['bulk_common_details']['district_id'] = dist_id
    context.user_data['bulk_common_details']['district_name'] = DISTRICTS.get(city_id, {}).get(dist_id)
    context.user_data['bulk_common_details']['product_type'] = p_type
    
    context.user_data['state'] = 'awaiting_bulk_size_input' 
    context.user_data.pop('bulk_flow_step', None) 

    city_name = context.user_data['bulk_common_details'].get('city_name', "N/A")
    dist_name = context.user_data['bulk_common_details'].get('district_name', "N/A")
    type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
    
    await query.edit_message_text(f"📦 Bulk Add (Manual) for {type_emoji} {p_type} in {city_name}/{dist_name}.\n\nStep 4: Please reply with the common Size for these items (e.g., 2g, 1 piece).",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]]))
    await query.answer("Enter common size.")

async def handle_adm_bulk_size_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bulk Add Flow - Handles Size input, asks for Price."""
    if update.effective_user.id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_bulk_size_input': return
    if not update.message or not update.message.text: return

    size = update.message.text.strip()
    if not size:
        await update.message.reply_text("Size cannot be empty. Please try again.", parse_mode=None)
        return
    if len(size) > 50:
        await update.message.reply_text("Size too long (max 50 chars). Please try again.", parse_mode=None)
        return

    context.user_data['bulk_common_details']['size'] = size
    context.user_data['state'] = 'awaiting_bulk_price_input'
    
    await update.message.reply_text(f"Common size set to: '{size}'.\n\nStep 5 (Manual): Please reply with the common Price (e.g., 12.50) for these items.",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]]), parse_mode=None)

async def handle_adm_bulk_price_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bulk Add Flow - Handles Price input, asks for forwarded drops."""
    if update.effective_user.id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_bulk_price_input': return
    if not update.message or not update.message.text: return

    price_text = update.message.text.strip().replace(',', '.')
    try:
        price = round(float(price_text), 2)
        if price <= 0: raise ValueError("Price must be positive.")
    except ValueError:
        await update.message.reply_text("❌ Invalid Price. Enter positive number (e.g., 12.50). Try again.", parse_mode=None)
        return

    context.user_data['bulk_common_details']['price'] = price
    context.user_data['bulk_items_added_count'] = 0 
    context.user_data['state'] = 'awaiting_bulk_forwarded_drops'
    
    common_details = context.user_data['bulk_common_details']
    setup_summary = (
        f"📦 Bulk Add Setup Complete (Manual Entry):\n"
        f"🏙️ City: {common_details['city_name']}\n"
        f"🏘️ District: {common_details['district_name']}\n"
        f"🧩 Type: {common_details['product_type']}\n"
        f"📏 Size: {common_details['size']}\n"
        f"💰 Price: {format_currency(common_details['price'])} EUR\n\n"
        f"Step 6: Now forward up to {BULK_ADD_LIMIT} messages. Each message should contain media (photo/video/GIF) and a text caption for details.\n\n"
        f"Type /done_bulk when finished or after reaching the limit."
    )
    await update.message.reply_text(setup_summary, 
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Bulk Add", callback_data="admin_menu")]]), parse_mode=None)

async def _add_single_bulk_item_to_db(context: ContextTypes.DEFAULT_TYPE, common_details: dict, message_media_info: list, original_text: str, admin_id: int) -> bool:
    """Helper function to add a single item from the bulk flow to the database."""
    city = common_details['city_name']
    district = common_details['district_name']
    p_type = common_details['product_type']
    size = common_details['size']
    price = common_details['price']
    
    current_bulk_session_item_index = context.user_data.get('bulk_items_added_count', 0) 
    product_name = f"{p_type} {size} BULK_{int(time.time())}_{current_bulk_session_item_index}"
    
    temp_dir = None
    conn = None
    product_id = None
    
    try:
        media_list_for_db = []
        if message_media_info: 
            temp_dir_base = await asyncio.to_thread(tempfile.mkdtemp, prefix="bulk_item_")
            temp_dir = os.path.join(temp_dir_base, str(int(time.time()*1000))) 
            await asyncio.to_thread(os.makedirs, temp_dir, exist_ok=True)

            for i, media_info_item in enumerate(message_media_info):
                m_type = media_info_item['type']
                file_id_tg = media_info_item['file_id'] 
                file_extension = ".jpg" if m_type == "photo" else ".mp4" if m_type in ["video", "gif"] else ".dat"
                temp_file_name = f"media_{i}_{file_id_tg}{file_extension}"
                temp_file_path = os.path.join(temp_dir, temp_file_name)
                try:
                    file_obj = await context.bot.get_file(file_id_tg)
                    await file_obj.download_to_drive(custom_path=temp_file_path)
                    if not await asyncio.to_thread(os.path.exists, temp_file_path) or await asyncio.to_thread(os.path.getsize, temp_file_path) == 0:
                        raise IOError(f"Bulk downloaded file {temp_file_path} is missing or empty.")
                    media_list_for_db.append({"type": m_type, "path": temp_file_path, "file_id": file_id_tg})
                except Exception as e:
                    logger.error(f"Error downloading media for bulk item ({file_id_tg}): {e}")
                    pass 

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        insert_params = (city, district, p_type, size, product_name, price, original_text, admin_id, datetime.now(timezone.utc).isoformat())
        c.execute("""INSERT INTO products
                        (city, district, product_type, size, name, price, available, reserved, original_text, added_by, added_date)
                     VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""", insert_params)
        product_id = c.lastrowid

        if product_id and media_list_for_db and temp_dir: 
            final_media_dir = os.path.join(MEDIA_DIR, str(product_id))
            await asyncio.to_thread(os.makedirs, final_media_dir, exist_ok=True)
            media_inserts = []
            for media_item_db in media_list_for_db:
                temp_p = media_item_db["path"]
                if await asyncio.to_thread(os.path.exists, temp_p):
                    new_fname = os.path.basename(temp_p) 
                    final_p_path = os.path.join(final_media_dir, new_fname)
                    try:
                        await asyncio.to_thread(shutil.move, temp_p, final_p_path)
                        media_inserts.append((product_id, media_item_db["type"], final_p_path, media_item_db["file_id"]))
                    except OSError as move_err:
                        logger.error(f"Error moving bulk media {temp_p}: {move_err}")
                else:
                    logger.warning(f"Temp bulk media not found during move: {temp_p}")
            if media_inserts:
                c.executemany("INSERT INTO product_media (product_id, media_type, file_path, telegram_file_id) VALUES (?, ?, ?, ?)", media_inserts)
        
        conn.commit()
        logger.info(f"Bulk Added: Product {product_id} ({product_name}) by admin {admin_id}.")
        return True
    except Exception as e:
        if conn and conn.in_transaction: conn.rollback()
        logger.error(f"Error adding single bulk item to DB for product '{product_name}': {e}", exc_info=True)
        return False
    finally:
        if conn: conn.close()
        if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir):
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)


async def handle_adm_bulk_forwarded_drops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bulk Add Flow - Processes forwarded messages to create products."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if admin_id != ADMIN_ID: return
    if context.user_data.get("state") != 'awaiting_bulk_forwarded_drops': return
    if not update.message: return

    if update.message.text and update.message.text.strip().lower() == '/done_bulk':
        await _finish_bulk_add_session(update, context, "Bulk add session finished by '/done_bulk' text.")
        return

    common_details = context.user_data.get('bulk_common_details')
    if not common_details:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Bulk setup details lost. Please start over.", parse_mode=None)
        await _finish_bulk_add_session(update, context, "Bulk session aborted due to missing setup data.")
        return

    current_count = context.user_data.get('bulk_items_added_count', 0)
    if current_count >= BULK_ADD_LIMIT:
        logger.info(f"Bulk add limit already reached, but another message received. Finishing session.")
        await _finish_bulk_add_session(update, context, f"Bulk add limit of {BULK_ADD_LIMIT} reached.")
        return

    original_text = (update.message.caption or "").strip() 
    media_info_list = []
    if update.message.photo: media_info_list.append({'type': 'photo', 'file_id': update.message.photo[-1].file_id})
    elif update.message.video: media_info_list.append({'type': 'video', 'file_id': update.message.video.file_id})
    elif update.message.animation: media_info_list.append({'type': 'gif', 'file_id': update.message.animation.file_id})
    
    if not media_info_list:
        await send_message_with_retry(context.bot, chat_id, "⚠️ Message has no media. Please forward a message with media and caption. This item was skipped.", parse_mode=None)
        return
    
    if not original_text: 
        await send_message_with_retry(context.bot, chat_id, "⚠️ Message media has no caption. Caption is used for product details. This item was skipped.", parse_mode=None)
        return

    add_success = await _add_single_bulk_item_to_db(context, common_details, media_info_list, original_text, admin_id)

    if add_success:
        context.user_data['bulk_items_added_count'] += 1
        count_now = context.user_data['bulk_items_added_count']
        await send_message_with_retry(context.bot, chat_id, f"✅ Item {count_now}/{BULK_ADD_LIMIT} added. Forward next or type /done_bulk.", parse_mode=None)
        if count_now >= BULK_ADD_LIMIT:
             await send_message_with_retry(context.bot, chat_id, f"Limit of {BULK_ADD_LIMIT} items reached. Finishing up...", parse_mode=None)
             await _finish_bulk_add_session(update, context, f"Bulk add limit of {BULK_ADD_LIMIT} reached.")
    else:
        await send_message_with_retry(context.bot, chat_id, "❌ Failed to add this item. Please check logs. You can try forwarding again or type /done_bulk.", parse_mode=None)

async def _finish_bulk_add_session(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str = "Bulk add session ended."):
    """Cleans up bulk add context and shows admin menu."""
    chat_id = None
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    elif update.message:
        chat_id = update.message.chat_id
    
    if not chat_id: 
        logger.error("_finish_bulk_add_session: could not determine chat_id."); 
        return

    count = context.user_data.get('bulk_items_added_count', 0)
    final_message = f"{message}\nTotal items added in this session: {count}."
    
    keys_to_pop = ['state', 'bulk_common_details', 'bulk_items_added_count', 'bulk_flow_step']
    for key in keys_to_pop:
        context.user_data.pop(key, None)
    
    await send_message_with_retry(context.bot, chat_id, final_message, parse_mode=None)
    
    kb = [[InlineKeyboardButton("🔧 Admin Menu", callback_data="admin_menu")]]
    await send_message_with_retry(context.bot, chat_id, "Returning to Admin Menu.", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)


async def handle_done_bulk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /done_bulk command to end a bulk session."""
    if update.effective_user.id != ADMIN_ID:
        logger.warning(f"Non-admin user {update.effective_user.id} attempted /done_bulk")
        return

    active_bulk_state = context.user_data.get('state')
    if active_bulk_state == 'awaiting_bulk_forwarded_drops' or 'bulk_common_details' in context.user_data:
        logger.info(f"Admin {update.effective_user.id} used /done_bulk command.")
        await _finish_bulk_add_session(update, context, "Bulk add session finished by /done_bulk command.")
    else:
        await update.message.reply_text("No active bulk add session to finish.", parse_mode=None)

# --- END OF FILE admin_product_management.py ---
