# --- START OF FILE main.py ---

import logging
import asyncio
import os
import signal
import sqlite3 # Keep for error handling if needed directly
from functools import wraps
from datetime import timedelta, datetime, timezone
import threading # Added for Flask thread
import json # Added for webhook processing
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hmac # For webhook signature verification
import hashlib # For webhook signature verification
import re # For flexible text parsing in worker interface

# --- Telegram Imports ---
from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, Defaults, ContextTypes,
    CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    PicklePersistence, JobQueue
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest, NetworkError, RetryAfter, TelegramError

# --- Flask Imports ---
from flask import Flask, request, Response # Added for webhook server
import nest_asyncio # Added to allow nested asyncio loops

# --- Local Imports ---
from utils import (
    TOKEN, ADMIN_ID, init_db, load_all_data, LANGUAGES, THEMES,
    SUPPORT_USERNAME, BASKET_TIMEOUT, clear_all_expired_baskets,
    SECONDARY_ADMIN_IDS, WEBHOOK_URL,
    NOWPAYMENTS_IPN_SECRET,
    get_db_connection,
    DATABASE_PATH,
    get_pending_deposit, remove_pending_deposit, FEE_ADJUSTMENT,
    send_message_with_retry,
    log_admin_action,
    format_currency,
    MEDIA_DIR,
    get_user_roles,  # NEW: Import for worker role checking
    PRODUCT_TYPES,    # NEW: Import for worker interface
    DEFAULT_PRODUCT_EMOJI  # NEW: Import for worker interface fallback emoji
)
import user # Import user module
from user import (
    start, handle_shop, handle_city_selection, handle_district_selection,
    handle_type_selection, handle_product_selection, handle_add_to_basket,
    handle_view_basket, handle_clear_basket, handle_remove_from_basket,
    handle_profile, handle_language_selection, handle_price_list,
    handle_price_list_city, handle_reviews_menu, handle_leave_review,
    handle_view_reviews, handle_leave_review_message, handle_back_start,
    handle_user_discount_code_message, apply_discount_start, remove_discount,
    handle_refill, handle_view_history,
    handle_refill_amount_message, validate_discount_code,
    handle_apply_discount_basket_pay,
    handle_skip_discount_basket_pay,
    handle_basket_discount_code_message,
    _show_crypto_choices_for_basket,
    handle_pay_single_item,
    handle_confirm_pay, 
    handle_apply_discount_single_pay,
    handle_skip_discount_single_pay,
    handle_single_item_discount_code_message
)



# Corrected imports based on your file structure
import admin_product_management 
import admin_features 
import admin_workers # <<< NEW: Import admin_workers
import worker_interface  # <<< NEW: Import worker interface

# NEW: Import bulk stock management modules
from bulk_stock_management import BulkStockManager
from admin_bulk_stock import BULK_STOCK_HANDLERS, AdminBulkStockMessageHandlers
from admin_bulk_stock_complete import COMPLETE_BULK_STOCK_HANDLERS, handle_bulk_stock_message_updates

from viewer_admin import (
    handle_viewer_admin_menu,
    handle_viewer_added_products,
    handle_viewer_view_product_media,
    handle_manage_users_start,
    handle_view_user_profile,
    handle_adjust_balance_start,
    handle_toggle_ban_user,
    handle_adjust_balance_amount_message,
    handle_adjust_balance_reason_message
)
try:
    from reseller_management import (
        handle_manage_resellers_menu,
        handle_reseller_manage_id_message,
        handle_reseller_toggle_status,
        handle_manage_reseller_discounts_select_reseller,
        handle_manage_specific_reseller_discounts,
        handle_reseller_add_discount_select_type,
        handle_reseller_add_discount_enter_percent,
        handle_reseller_edit_discount,
        handle_reseller_percent_message,
        handle_reseller_delete_discount_confirm,
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
    async def handle_manage_specific_reseller_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass
    async def handle_reseller_add_discount_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass
    async def handle_reseller_add_discount_enter_percent(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass
    async def handle_reseller_edit_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass
    async def handle_reseller_percent_message(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
    async def handle_reseller_delete_discount_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None): pass

import payment
from payment import credit_user_balance
from stock import handle_view_stock

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
logging.getLogger('apscheduler.executors.default').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

nest_asyncio.apply()

flask_app = Flask(__name__)
telegram_app: Application | None = None
main_loop = None

# --- Callback Data Parsing Decorator ---
def callback_query_router(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        if query and query.data:
            parts = query.data.split('|')
            command = parts[0]
            params = parts[1:]
            
            KNOWN_HANDLERS = {
                # User Handlers (from user.py)
                "start": user.start, "back_start": user.handle_back_start, "shop": user.handle_shop,
                "city": user.handle_city_selection, "dist": user.handle_district_selection,
                "type": user.handle_type_selection, "product": user.handle_product_selection,
                "add": user.handle_add_to_basket,
                "pay_single_item": user.handle_pay_single_item,
                "view_basket": user.handle_view_basket,
                "clear_basket": user.handle_clear_basket, "remove": user.handle_remove_from_basket,
                "profile": user.handle_profile, "language": user.handle_language_selection,
                "price_list": user.handle_price_list, "price_list_city": user.handle_price_list_city,
                "reviews": user.handle_reviews_menu, "leave_review": user.handle_leave_review,
                "view_reviews": user.handle_view_reviews, "leave_review_now": user.handle_leave_review_now,
                "refill": user.handle_refill,
                "view_history": user.handle_view_history,
                "apply_discount_start": user.apply_discount_start, "remove_discount": user.remove_discount,
                "confirm_pay": user.handle_confirm_pay,
                "apply_discount_basket_pay": user.handle_apply_discount_basket_pay,
                "skip_discount_basket_pay": user.handle_skip_discount_basket_pay,
                "apply_discount_single_pay": user.handle_apply_discount_single_pay,
                "skip_discount_single_pay": user.handle_skip_discount_single_pay,
                "single_item_discount_code_message": user.handle_single_item_discount_code_message,

                # Payment Handlers (from payment.py)
                "select_basket_crypto": payment.handle_select_basket_crypto,
                "cancel_crypto_payment": payment.handle_cancel_crypto_payment,
                "select_refill_crypto": payment.handle_select_refill_crypto,

                # Admin Product Management Handlers (from admin_product_management.py)
                "admin_menu": admin_product_management.handle_admin_menu,
                "adm_city": admin_product_management.handle_adm_city, 
                "adm_dist": admin_product_management.handle_adm_dist, 
                "adm_type": admin_product_management.handle_adm_type, 
                "adm_add": admin_product_management.handle_adm_add, 
                "adm_size": admin_product_management.handle_adm_size, 
                "adm_custom_size": admin_product_management.handle_adm_custom_size,
                "confirm_add_drop": admin_product_management.handle_confirm_add_drop, 
                "cancel_add": admin_product_management.cancel_add,
                "adm_manage_cities": admin_product_management.handle_adm_manage_cities, 
                "adm_add_city": admin_product_management.handle_adm_add_city,
                "adm_edit_city": admin_product_management.handle_adm_edit_city, 
                "adm_delete_city": admin_product_management.handle_adm_delete_city,
                "adm_manage_districts": admin_product_management.handle_adm_manage_districts, 
                "adm_manage_districts_city": admin_product_management.handle_adm_manage_districts_city,
                "adm_add_district": admin_product_management.handle_adm_add_district, 
                "adm_edit_district": admin_product_management.handle_adm_edit_district,
                "adm_remove_district": admin_product_management.handle_adm_remove_district,
                "adm_manage_products": admin_product_management.handle_adm_manage_products, 
                "adm_manage_products_city": admin_product_management.handle_adm_manage_products_city,
                "adm_manage_products_dist": admin_product_management.handle_adm_manage_products_dist, 
                "adm_manage_products_type": admin_product_management.handle_adm_manage_products_type,
                "adm_delete_prod": admin_product_management.handle_adm_delete_prod,
                "adm_manage_types": admin_product_management.handle_adm_manage_types,
                "adm_edit_type_menu": admin_product_management.handle_adm_edit_type_menu,
                "adm_change_type_emoji": admin_product_management.handle_adm_change_type_emoji,
                "adm_add_type": admin_product_management.handle_adm_add_type,
                "adm_delete_type": admin_product_management.handle_adm_delete_type,
                "adm_reassign_type_start": admin_product_management.handle_adm_reassign_type_start,
                "adm_set_media": admin_product_management.handle_adm_set_media,
                "confirm_force_delete_prompt": admin_product_management.handle_confirm_force_delete_prompt, 
                "adm_bulk_start_setup": admin_product_management.handle_adm_bulk_start_setup,
                "adm_bulk_city_chosen": admin_product_management.handle_adm_bulk_city_chosen,
                "adm_bulk_district_chosen": admin_product_management.handle_adm_bulk_district_chosen, 
                "adm_bulk_ask_detail_method": admin_product_management.handle_adm_bulk_ask_detail_method, 
                "adm_bulk_manual_type_select": admin_product_management.handle_adm_bulk_manual_type_select, 
                "adm_bulk_select_existing_type_start": admin_product_management.handle_adm_bulk_select_existing_type_start, 
                "adm_bulk_select_existing_combo_start": admin_product_management.handle_adm_bulk_select_existing_combo_start, 
                "adm_bulk_apply_existing_combo": admin_product_management.handle_adm_bulk_apply_existing_combo, 
                "adm_bulk_type_chosen": admin_product_management.handle_adm_bulk_type_chosen, 
                
                # Admin Features Handlers (from admin_features.py)
                "sales_analytics_menu": admin_features.handle_sales_analytics_menu, 
                "sales_dashboard": admin_features.handle_sales_dashboard,
                "sales_select_period": admin_features.handle_sales_select_period, 
                "sales_run": admin_features.handle_sales_run,
                "adm_manage_discounts": admin_features.handle_adm_manage_discounts, 
                "adm_toggle_discount": admin_features.handle_adm_toggle_discount,
                "adm_delete_discount": admin_features.handle_adm_delete_discount, 
                "adm_add_discount_start": admin_features.handle_adm_add_discount_start,
                "adm_use_generated_code": admin_features.handle_adm_use_generated_code, 
                "adm_set_discount_type": admin_features.handle_adm_set_discount_type,
                "confirm_yes": admin_features.handle_confirm_yes, 
                "adm_broadcast_start": admin_features.handle_adm_broadcast_start,
                "adm_broadcast_target_type": admin_features.handle_adm_broadcast_target_type,
                "adm_broadcast_target_city": admin_features.handle_adm_broadcast_target_city,
                "adm_broadcast_target_status": admin_features.handle_adm_broadcast_target_status,
                "cancel_broadcast": admin_features.handle_cancel_broadcast,
                "confirm_broadcast": admin_features.handle_confirm_broadcast,
                "adm_manage_reviews": admin_features.handle_adm_manage_reviews,
                "adm_delete_review_confirm": admin_features.handle_adm_delete_review_confirm,
                "adm_manage_welcome": admin_features.handle_adm_manage_welcome,
                "adm_activate_welcome": admin_features.handle_adm_activate_welcome,
                "adm_add_welcome_start": admin_features.handle_adm_add_welcome_start,
                "adm_edit_welcome": admin_features.handle_adm_edit_welcome,
                "adm_delete_welcome_confirm": admin_features.handle_adm_delete_welcome_confirm,
                "adm_edit_welcome_text": admin_features.handle_adm_edit_welcome_text,
                "adm_edit_welcome_desc": admin_features.handle_adm_edit_welcome_desc,
                "adm_reset_default_confirm": admin_features.handle_reset_default_welcome,
                "confirm_save_welcome": admin_features.handle_confirm_save_welcome,
                "adm_clear_reservations_confirm": admin_product_management.handle_adm_clear_reservations_confirm,

                # Viewer Admin Handlers (from viewer_admin.py)
                "viewer_admin_menu": handle_viewer_admin_menu,
                "viewer_added_products": handle_viewer_added_products,
                "viewer_view_product_media": handle_viewer_view_product_media,
                "adm_manage_users": handle_manage_users_start, 
                "adm_view_user": handle_view_user_profile,
                "adm_adjust_balance_start": handle_adjust_balance_start,
                "adm_toggle_ban": handle_toggle_ban_user,

                # Reseller Management Handlers (from reseller_management.py)
                "manage_resellers_menu": handle_manage_resellers_menu,
                "reseller_toggle_status": handle_reseller_toggle_status,
                "manage_reseller_discounts_select_reseller": handle_manage_reseller_discounts_select_reseller,
                "reseller_manage_specific": handle_manage_specific_reseller_discounts,
                "reseller_add_discount_select_type": handle_reseller_add_discount_select_type,
                "reseller_add_discount_enter_percent": handle_reseller_add_discount_enter_percent,
                "reseller_edit_discount": handle_reseller_edit_discount,
                "reseller_delete_discount_confirm": handle_reseller_delete_discount_confirm,

                # Stock Handler (from stock.py)
                "view_stock": handle_view_stock,

                # === Worker Management Callbacks (from admin_workers.py) START ===
                "manage_workers_menu": admin_workers.handle_manage_workers_menu,
                "adm_add_worker_prompt_id": admin_workers.handle_adm_add_worker_prompt_id,
                "adm_confirm_make_worker": admin_workers.handle_adm_confirm_make_worker,
                "adm_view_workers_list": admin_workers.handle_adm_view_workers_list,
                "adm_view_specific_worker": admin_workers.handle_adm_view_specific_worker,
                "adm_worker_toggle_status": admin_workers.handle_adm_worker_toggle_status,
                "adm_worker_remove_confirm": admin_workers.handle_adm_worker_remove_confirm,
                "adm_remove_worker_menu": admin_workers.handle_adm_remove_worker_menu,
                
                # NEW: Enhanced Worker Management Callbacks
                "adm_worker_analytics_menu": admin_workers.handle_adm_worker_analytics_menu,
                "adm_worker_analytics_view": admin_workers.handle_adm_worker_analytics_view,
                
                # Enhanced Worker Interface Callbacks (from worker_interface.py)
                "worker_admin_menu": worker_interface.handle_worker_admin_menu,
                "worker_select_category": worker_interface.handle_worker_select_category,
                "worker_category_chosen": worker_interface.handle_worker_category_chosen,
                "worker_add_single": worker_interface.handle_worker_add_single,
                "worker_add_bulk": worker_interface.handle_worker_add_bulk,
                "worker_single_city": worker_interface.handle_worker_single_city,
                "worker_single_district": worker_interface.handle_worker_single_district,
                "worker_bulk_city": worker_interface.handle_worker_bulk_city,
                "worker_bulk_district": worker_interface.handle_worker_bulk_district,
                "worker_bulk_finish": worker_interface.handle_worker_bulk_finish,
                "worker_confirm_single_product": worker_interface.handle_worker_confirm_single_product,
                "worker_confirm_bulk_products": worker_interface.handle_worker_confirm_bulk_products,
                "worker_bulk_forwarded_drops": worker_interface.handle_worker_bulk_forwarded_drops,
                
                # Alternative names for worker handlers
                "worker_menu": worker_interface.handle_worker_admin_menu,  # Alternative name
                "worker_main": worker_interface.handle_worker_admin_menu,  # Alternative name
                
                # NEW: Bulk Stock Management Handlers (from admin_bulk_stock.py)
                **BULK_STOCK_HANDLERS,
                **COMPLETE_BULK_STOCK_HANDLERS,
            }
            
            target_func = KNOWN_HANDLERS.get(command)

            if target_func and asyncio.iscoroutinefunction(target_func):
                try:
                    await target_func(update, context, params)
                except Exception as handler_error:
                    logger.error(f"Error in handler {command}: {handler_error}", exc_info=True)
                    try: 
                        await query.answer("An error occurred processing your request. Please try again.", show_alert=True)
                    except Exception as answer_error:
                        logger.error(f"Error answering callback query after handler error: {answer_error}")
            else:
                logger.warning(f"No async handler function found or mapped for callback command: {command}")
                try: await query.answer("Unknown action.", show_alert=True)
                except Exception as e: logger.error(f"Error answering unknown callback query {command}: {e}")
        elif query:
            logger.warning("Callback query handler received update without data.")
            try: await query.answer()
            except Exception as e: logger.error(f"Error answering callback query without data: {e}")
        else:
            logger.warning("Callback query handler received update without query object.")
    return wrapper

@callback_query_router
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

# --- State Validation Helper ---
async def _validate_and_cleanup_state(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str) -> bool:
    """Validate state integrity and return True if valid, False if corrupted"""
    user_id = update.effective_user.id
    
    # Define required context for each state
    state_requirements = {
        "awaiting_drop_details": ["admin_city", "admin_district", "admin_product_type", "pending_drop_size", "pending_drop_price"],
        "awaiting_worker_single_product": ["worker_selected_category", "worker_single_city", "worker_single_district"],
        "awaiting_worker_bulk_details": ["worker_selected_category", "worker_bulk_city", "worker_bulk_district"],
        "awaiting_worker_bulk_forwarded_drops": ["worker_selected_category", "worker_bulk_city", "worker_bulk_district"],
        "awaiting_welcome_template_edit": ["editing_welcome_template_name"],
        "awaiting_welcome_confirmation": ["pending_welcome_template"],
        "awaiting_balance_adjustment_amount": ["adjust_balance_target_user_id"],
        "awaiting_balance_adjustment_reason": ["adjust_balance_target_user_id", "adjust_balance_amount"],
        "awaiting_basket_discount_code": ["basket_pay_snapshot", "basket_pay_total_eur"],
        "awaiting_single_item_discount_code": ["single_item_pay_snapshot", "single_item_pay_final_eur"],
        "awaiting_review": [],  # No specific requirements
        "awaiting_refill_amount": [],  # No specific requirements
        "awaiting_user_discount_code": [],  # No specific requirements
    }
    
    required_keys = state_requirements.get(state, [])
    
    # Check if all required context exists
    for key in required_keys:
        if key not in context.user_data:
            logger.warning(f"State {state} missing required context key: {key} for user {user_id}")
            return False
    
    # Additional validation for specific states
    if state in ["awaiting_worker_single_product", "awaiting_worker_bulk_details", "awaiting_worker_bulk_forwarded_drops"]:
        # Verify worker permissions
        try:
            user_roles = get_user_roles(user_id)
            if not user_roles['is_worker']:
                logger.warning(f"Non-worker user {user_id} in worker state {state}")
                return False
        except Exception as e:
            logger.error(f"Error checking worker permissions for user {user_id}: {e}")
            return False
    
    if state.startswith("awaiting_balance_adjustment"):
        # Verify admin permissions
        if user_id != ADMIN_ID:
            logger.warning(f"Non-admin user {user_id} in admin balance adjustment state")
            return False
    
    if state.startswith("awaiting_welcome"):
        # Verify admin permissions
        if user_id != ADMIN_ID:
            logger.warning(f"Non-admin user {user_id} in welcome management state")
            return False
    
    return True

# --- Central Message Handler (for states) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        text = update.message.text.strip() if update.message.text else ""
        
        # NEW: State validation and cleanup
        current_state = context.user_data.get("state")
        if current_state:
            # Validate state integrity and clear corrupted states
            if await _validate_and_cleanup_state(update, context, current_state):
                logger.debug(f"State {current_state} validated for user {user_id}")
            else:
                logger.warning(f"Corrupted state {current_state} cleared for user {user_id}")
                context.user_data.pop("state", None)
                # Clear related state data
                state_related_keys = [
                    "admin_city", "admin_district", "admin_product_type", "pending_drop_size", "pending_drop_price",
                    "worker_selected_category", "worker_single_city", "worker_bulk_city",
                    "editing_welcome_template_name", "pending_welcome_template",
                    "adjust_balance_target_user_id", "adjust_balance_amount",
                    "basket_pay_snapshot", "single_item_pay_snapshot"
                ]
                for key in state_related_keys:
                    context.user_data.pop(key, None)
        
        # Handle /commands
        if text.startswith('/admin'):
            # Check user roles for admin access
            user_roles = get_user_roles(user_id)
            
            # Debug logging to see what roles are detected
            logger.info(f"DEBUG: User {user_id} roles: {user_roles}")
            
            if user_roles['is_primary'] or user_roles['is_secondary']:
                # Full admin access
                logger.info(f"DEBUG: Routing user {user_id} to admin panel")
                await admin_product_management.handle_admin_menu(update, context)
            elif user_roles['is_worker']:
                # Limited worker access - call worker interface
                logger.info(f"DEBUG: Routing user {user_id} to worker interface")
                await worker_interface.handle_worker_admin_menu(update, context)
            else:
                logger.info(f"DEBUG: Denying access to user {user_id}")
                await update.message.reply_text("Access denied.")
            return
        elif text.startswith('/done_bulk'):
            if user_id in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
                await admin_product_management.handle_done_bulk_command(update, context)
            else:
                await update.message.reply_text("Access denied.")
            return
        elif text.startswith('/start'):
            await user.start(update, context)
            return

        # NEW: Handle bulk stock management text input
        if user_id in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await AdminBulkStockMessageHandlers.handle_bulk_stock_text_input(update, context)
            await handle_bulk_stock_message_updates(update, context)
        
        # Admin Workers Message Handling
        await admin_workers.handle_admin_worker_message(update, context)
        
        # Admin Bulk Add Message Handling (for forwarded messages and size/price input)
        if user_id in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await admin_product_management.handle_adm_bulk_size_message(update, context)
            await admin_product_management.handle_adm_bulk_price_message(update, context)
            await admin_product_management.handle_adm_bulk_forwarded_drops(update, context)
            # Admin Product Management message handlers
            await admin_product_management.handle_adm_drop_details_message(update, context)
            await admin_product_management.handle_adm_custom_size_message(update, context)
            await admin_product_management.handle_adm_price_message(update, context)
            await admin_product_management.handle_adm_bot_media_message(update, context)
            await admin_product_management.handle_adm_add_city_message(update, context)
            await admin_product_management.handle_adm_edit_city_message(update, context)
            await admin_product_management.handle_adm_add_district_message(update, context)
            await admin_product_management.handle_adm_edit_district_message(update, context)
            await admin_product_management.handle_adm_add_type_message(update, context)
            await admin_product_management.handle_adm_add_type_emoji_message(update, context)
            await admin_product_management.handle_adm_edit_type_emoji_message(update, context)
            await admin_product_management.handle_adm_reassign_old_type_name_message(update, context)
            await admin_product_management.handle_adm_reassign_new_type_name_message(update, context)
            
            # Admin Features message handlers
            await admin_features.handle_adm_discount_code_message(update, context)
            await admin_features.handle_adm_discount_value_message(update, context)
            await admin_features.handle_adm_broadcast_message(update, context)
            await admin_features.handle_adm_broadcast_inactive_days_message(update, context)
            await admin_features.handle_adm_welcome_template_name_message(update, context)
            await admin_features.handle_adm_welcome_template_text_message(update, context)
            await admin_features.handle_adm_welcome_description_message(update, context)
            await admin_features.handle_adm_welcome_description_edit_message(update, context)
        
        # Worker Message Handling (simplified product adding)
        await handle_worker_single_product_message(update, context)
        await handle_worker_bulk_forwarded_drops_message(update, context)
        
        # Reseller Management Message Handling  
        await handle_reseller_manage_id_message(update, context)
        await handle_reseller_percent_message(update, context)
        
        # User Message Handling
        await user.handle_user_discount_code_message(update, context)
        await user.handle_refill_amount_message(update, context)
        await user.handle_leave_review_message(update, context)
        await user.handle_basket_discount_code_message(update, context)
        await user.handle_single_item_discount_code_message(update, context)
        
        # Viewer Admin Message Handling
        await handle_adjust_balance_amount_message(update, context)
        await handle_adjust_balance_reason_message(update, context)
        
    except Exception as e:
        logger.error(f"Unexpected error in handle_message: {e}", exc_info=True)
        await update.message.reply_text("An error occurred.")


# --- Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    logger.error(f"Caught error type: {type(context.error)}")
    
    # Debug logging
    if isinstance(update, Update):
        if update.message and update.message.text:
            logger.error(f"DEBUG: Error occurred while processing message: '{update.message.text}' from user {update.effective_user.id if update.effective_user else 'Unknown'}")
    
    chat_id = None
    user_id = None

    if isinstance(update, Update):
        if update.effective_chat: chat_id = update.effective_chat.id
        if update.effective_user: user_id = update.effective_user.id

    logger.debug(f"Error context: user_data={context.user_data}, chat_data={context.chat_data}")

    if chat_id:
        error_message = "An internal error occurred. Please try again later or contact support."
        if isinstance(context.error, BadRequest):
            error_str_lower = str(context.error).lower()
            if "message is not modified" in error_str_lower:
                logger.debug(f"Ignoring 'message is not modified' error for chat {chat_id}.")
                return
            if "query is too old" in error_str_lower:
                 logger.debug(f"Ignoring 'query is too old' error for chat {chat_id}.")
                 return
            logger.warning(f"Telegram API BadRequest for chat {chat_id} (User: {user_id}): {context.error}")
            if "can't parse entities" in error_str_lower:
                error_message = "An error occurred displaying the message due to formatting. Please try again."
            else:
                 error_message = "An error occurred communicating with Telegram. Please try again."
        elif isinstance(context.error, NetworkError):
            logger.warning(f"Telegram API NetworkError for chat {chat_id} (User: {user_id}): {context.error}")
            error_message = "A network error occurred. Please check your connection and try again."
        elif isinstance(context.error, Forbidden):
             logger.warning(f"Forbidden error for chat {chat_id} (User: {user_id}): Bot possibly blocked or kicked.")
             return 
        elif isinstance(context.error, RetryAfter):
             retry_seconds = context.error.retry_after + 1
             logger.warning(f"Rate limit hit during update processing for chat {chat_id}. Error: {context.error}")
             return
        elif isinstance(context.error, sqlite3.Error): 
            logger.error(f"Database error during update handling for chat {chat_id} (User: {user_id}): {context.error}", exc_info=True)
        elif isinstance(context.error, NameError): 
             logger.error(f"NameError encountered for chat {chat_id} (User: {user_id}): {context.error}", exc_info=True)
             if 'clear_expired_basket' in str(context.error): 
                 error_message = "An internal processing error occurred (payment). Please try again."
             elif 'handle_adm_welcome_' in str(context.error):
                 error_message = "An internal processing error occurred (welcome msg). Please try again."
             else:
                 error_message = "An internal processing error occurred. Please try again or contact support if it persists."
        elif isinstance(context.error, AttributeError):
             logger.error(f"AttributeError encountered for chat {chat_id} (User: {user_id}): {context.error}", exc_info=True)
             if "'NoneType' object has no attribute 'get'" in str(context.error) and "_process_collected_media" in str(context.error.__traceback__): 
                 error_message = "An internal processing error occurred (media group). Please try again."
             elif "'module' object has no attribute" in str(context.error) and "handle_confirm_pay" in str(context.error):
                 error_message = "A critical configuration error occurred. Please contact support immediately."
             else:
                 error_message = "An unexpected internal error occurred. Please contact support."
        else: 
             logger.exception(f"An unexpected error occurred during update handling for chat {chat_id} (User: {user_id}).") 
             error_message = "An unexpected error occurred. Please contact support." 
        try:
            bot_instance = context.bot if hasattr(context, 'bot') else (telegram_app.bot if telegram_app else None)
            if bot_instance:
                await send_message_with_retry(bot_instance, chat_id, error_message, parse_mode=None)
            else:
                logger.error("Could not get bot instance to send error message.")
        except Exception as e:
            logger.error(f"Failed to send error message to user {chat_id}: {e}")


# --- Bot Setup Functions ---
async def post_init(application: Application) -> None:
    logger.info("Running post_init setup...")
    logger.info("Setting bot commands...")
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot / Main menu"),
        BotCommand("admin", "Access admin panel (Admin only)"),
        BotCommand("done_bulk", "Finish bulk product adding session (Admin only)"), 
    ])
    logger.info("Post_init finished.")

async def post_shutdown(application: Application) -> None:
    logger.info("Running post_shutdown cleanup...")
    logger.info("Post_shutdown finished.")

async def clear_expired_baskets_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    logger.debug("Running background job: clear_expired_baskets_job")
    try:
        await asyncio.to_thread(clear_all_expired_baskets)
    except Exception as e:
        logger.error(f"Error in background job clear_expired_baskets_job: {e}", exc_info=True)

# NEW: Background job for bulk stock monitoring
async def bulk_stock_monitoring_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    """Background job to monitor stock levels and send worker notifications"""
    logger.debug("Running background job: bulk_stock_monitoring_job")
    try:
        notifications_sent = await asyncio.to_thread(BulkStockManager.check_stock_levels_and_notify)
        if notifications_sent > 0:
            logger.info(f"Bulk stock monitoring job sent {notifications_sent} worker notifications")
    except Exception as e:
        logger.error(f"Error in background job bulk_stock_monitoring_job: {e}", exc_info=True)

# NEW: Background job for worker achievements and notifications
async def worker_achievements_notification_job_wrapper(context: ContextTypes.DEFAULT_TYPE):
    """Background job to check worker achievements and send notifications"""
    logger.debug("Running background job: worker_achievements_notification_job")
    try:
        # TODO: Import the function from admin_workers when implemented
        # from admin_workers import check_worker_achievements_and_notify
        # await check_worker_achievements_and_notify(context)
        logger.info("Worker achievements notification job - function not implemented yet")
    except Exception as e:
        logger.error(f"Error in background job worker_achievements_notification_job: {e}", exc_info=True)

# --- Flask Webhook Routes ---
def verify_nowpayments_signature(request_data_bytes, signature_header, secret_key):
    if not secret_key or not signature_header:
        logger.warning("IPN Secret Key or signature header missing. Cannot verify webhook.")
        return False
    try:
        ordered_data = json.dumps(json.loads(request_data_bytes), sort_keys=True, separators=(',', ':'))
        hmac_hash = hmac.new(secret_key.encode('utf-8'), ordered_data.encode('utf-8'), hashlib.sha512).hexdigest()
        return hmac.compare_digest(hmac_hash, signature_header)
    except Exception as e:
        logger.error(f"Error during signature verification: {e}", exc_info=True)
        return False

@flask_app.route("/webhook", methods=['POST'])
def nowpayments_webhook():
    global telegram_app, main_loop, NOWPAYMENTS_IPN_SECRET
    if not telegram_app or not main_loop:
        logger.error("Webhook received but Telegram app or event loop not initialized.")
        return Response(status=503)

    raw_body = request.get_data() 
    signature = request.headers.get('x-nowpayments-sig')

    if NOWPAYMENTS_IPN_SECRET:
        try:
            temp_ordered_data = json.dumps(json.loads(raw_body), sort_keys=True, separators=(',', ':'))
            expected_signature = hmac.new(NOWPAYMENTS_IPN_SECRET.encode('utf-8'), temp_ordered_data.encode('utf-8'), hashlib.sha512).hexdigest()
            logger.info(f"NOWPayments IPN Received. Signature: {signature}. Expected (if verified): {expected_signature}")
        except Exception as sig_calc_e:
            logger.error(f"Error calculating expected signature for logging: {sig_calc_e}")

    if NOWPAYMENTS_IPN_SECRET:
        if not verify_nowpayments_signature(raw_body, signature, NOWPAYMENTS_IPN_SECRET):
            logger.error("Webhook signature verification FAILED. Request will be ignored.")
            return Response("Signature verification failed", status=403)
        logger.info("Webhook signature VERIFIED successfully.")
    else:
        logger.warning("!!! NOWPayments signature verification is DISABLED (NOWPAYMENTS_IPN_SECRET not set) !!!")


    try:
        data = json.loads(raw_body) 
    except json.JSONDecodeError:
        logger.warning("Webhook received non-JSON request.")
        return Response("Invalid Request: Not JSON", status=400)

    logger.info(f"NOWPayments IPN Data: {json.dumps(data)}") 

    required_keys = ['payment_id', 'payment_status', 'pay_currency', 'actually_paid']
    if not all(key in data for key in required_keys):
        logger.error(f"Webhook missing required keys. Data: {data}")
        return Response("Missing required keys", status=400)

    payment_id = data.get('payment_id')
    status = data.get('payment_status')
    pay_currency = data.get('pay_currency')
    actually_paid_str = data.get('actually_paid')
    parent_payment_id = data.get('parent_payment_id')

    if parent_payment_id:
         logger.info(f"Ignoring child payment webhook update {payment_id} (parent: {parent_payment_id}).")
         return Response("Child payment ignored", status=200)

    if status in ['finished', 'confirmed', 'partially_paid'] and actually_paid_str is not None:
        logger.info(f"Processing '{status}' payment: {payment_id}")
        try:
            actually_paid_decimal = Decimal(str(actually_paid_str))
            if actually_paid_decimal <= 0:
                logger.warning(f"Ignoring webhook for payment {payment_id} with zero 'actually_paid'.")
                if status != 'confirmed': 
                    asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="zero_paid"), main_loop)
                return Response("Zero amount paid", status=200)

            pending_info = asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(get_pending_deposit, payment_id), main_loop
            ).result()

            if not pending_info:
                 logger.warning(f"Webhook Warning: Pending deposit {payment_id} not found.")
                 return Response("Pending deposit not found", status=200)

            user_id = pending_info['user_id']
            stored_currency = pending_info['currency']
            target_eur_decimal = Decimal(str(pending_info['target_eur_amount']))
            expected_crypto_decimal = Decimal(str(pending_info.get('expected_crypto_amount', '0.0')))
            is_purchase = pending_info.get('is_purchase') == 1
            basket_snapshot = pending_info.get('basket_snapshot')
            discount_code_used = pending_info.get('discount_code_used')
            log_prefix = "PURCHASE" if is_purchase else "REFILL"

            if stored_currency.lower() != pay_currency.lower():
                 logger.error(f"Currency mismatch {log_prefix} {payment_id}. DB: {stored_currency}, Webhook: {pay_currency}")
                 asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="currency_mismatch"), main_loop)
                 return Response("Currency mismatch", status=400)

            paid_eur_equivalent = Decimal('0.0')
            if expected_crypto_decimal > Decimal('0.0'):
                proportion = actually_paid_decimal / expected_crypto_decimal
                paid_eur_equivalent = (proportion * target_eur_decimal).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            else:
                logger.error(f"{log_prefix} {payment_id}: Cannot calculate EUR equivalent (expected crypto amount is zero).")
                asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="zero_expected_crypto"), main_loop)
                return Response("Cannot calculate EUR equivalent", status=400)

            logger.info(f"{log_prefix} {payment_id}: User {user_id} paid {actually_paid_decimal} {pay_currency}. Approx EUR value: {paid_eur_equivalent:.2f}. Target EUR: {target_eur_decimal:.2f}")

            dummy_context = ContextTypes.DEFAULT_TYPE(application=telegram_app, chat_id=user_id, user_id=user_id) if telegram_app else None
            if not dummy_context:
                logger.error(f"Cannot process {log_prefix} {payment_id}, telegram_app not ready.")
                return Response("Internal error: App not ready", status=503)

            if is_purchase:
                if actually_paid_decimal >= expected_crypto_decimal:
                    logger.info(f"{log_prefix} {payment_id}: Sufficient payment received. Finalizing purchase.")
                    finalize_future = asyncio.run_coroutine_threadsafe(
                        payment.process_successful_crypto_purchase(user_id, basket_snapshot, discount_code_used, payment_id, dummy_context),
                        main_loop
                    )
                    purchase_finalized = False
                    try: purchase_finalized = finalize_future.result(timeout=60)
                    except Exception as e: logger.error(f"Error getting result from process_successful_crypto_purchase for {payment_id}: {e}. Purchase may not be fully finalized.", exc_info=True)

                    if purchase_finalized:
                        overpaid_eur = (paid_eur_equivalent - target_eur_decimal).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                        if overpaid_eur > Decimal('0.0'):
                            logger.info(f"{log_prefix} {payment_id}: Overpayment detected. Crediting {overpaid_eur:.2f} EUR to user {user_id} balance.")
                            credit_future = asyncio.run_coroutine_threadsafe(
                                credit_user_balance(user_id, overpaid_eur, f"Overpayment on purchase {payment_id}", dummy_context),
                                main_loop
                            )
                            try: credit_future.result(timeout=30)
                            except Exception as e:
                                logger.error(f"Error crediting overpayment for {payment_id}: {e}", exc_info=True)
                                if ADMIN_ID: asyncio.run_coroutine_threadsafe(send_message_with_retry(telegram_app.bot, ADMIN_ID, f"⚠️ CRITICAL: Failed to credit overpayment for purchase {payment_id} user {user_id}. Amount: {overpaid_eur:.2f} EUR. MANUAL CHECK NEEDED!"), main_loop)
                        asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="purchase_success"), main_loop)
                        logger.info(f"Successfully processed and removed pending record for {log_prefix} {payment_id}")
                    else:
                        logger.critical(f"CRITICAL: {log_prefix} {payment_id} paid (>= expected), but process_successful_crypto_purchase FAILED for user {user_id}. Pending deposit NOT removed. Manual intervention required.")
                        if ADMIN_ID: asyncio.run_coroutine_threadsafe(send_message_with_retry(telegram_app.bot, ADMIN_ID, f"⚠️ CRITICAL: Crypto purchase {payment_id} paid by user {user_id} but FAILED TO FINALIZE. Check logs!"), main_loop)
                else: # Underpayment
                    logger.warning(f"{log_prefix} {payment_id} UNDERPAID by user {user_id}. Crediting balance with received amount.")
                    credit_future = asyncio.run_coroutine_threadsafe(
                         credit_user_balance(user_id, paid_eur_equivalent, f"Underpayment on purchase {payment_id}", dummy_context),
                         main_loop
                    )
                    credit_success = False
                    try: credit_success = credit_future.result(timeout=30)
                    except Exception as e: logger.error(f"Error crediting underpayment for {payment_id}: {e}", exc_info=True)
                    if not credit_success:
                         logger.critical(f"CRITICAL: Failed to credit balance for underpayment {payment_id} user {user_id}. Amount: {paid_eur_equivalent:.2f} EUR. MANUAL CHECK NEEDED!")
                         if ADMIN_ID: asyncio.run_coroutine_threadsafe(send_message_with_retry(telegram_app.bot, ADMIN_ID, f"⚠️ CRITICAL: Failed to credit balance for UNDERPAYMENT {payment_id} user {user_id}. Amount: {paid_eur_equivalent:.2f} EUR. MANUAL CHECK NEEDED!"), main_loop)
                    lang_data_local = LANGUAGES.get(dummy_context.user_data.get("lang", "en"), LANGUAGES['en'])
                    fail_msg_template = lang_data_local.get("crypto_purchase_underpaid_credited", "⚠️ Purchase Failed: Underpayment detected. Amount needed was {needed_eur} EUR. Your balance has been credited with the received value ({paid_eur} EUR). Your items were not delivered.")
                    fail_msg = fail_msg_template.format(needed_eur=format_currency(target_eur_decimal), paid_eur=format_currency(paid_eur_equivalent))
                    asyncio.run_coroutine_threadsafe(send_message_with_retry(telegram_app.bot, user_id, fail_msg, parse_mode=None), main_loop)
                    asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="failure"), main_loop)
                    logger.info(f"Processed underpaid purchase {payment_id} for user {user_id}. Balance credited, items un-reserved.")
            else: # Refill
                 credited_eur_amount = paid_eur_equivalent
                 if credited_eur_amount > 0:
                     future = asyncio.run_coroutine_threadsafe(
                         payment.process_successful_refill(user_id, credited_eur_amount, payment_id, dummy_context),
                         main_loop
                     )
                     try:
                          db_update_success = future.result(timeout=30)
                          if db_update_success:
                               asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="refill_success"), main_loop)
                               logger.info(f"Successfully processed and removed pending deposit {payment_id} (Status: {status})")
                          else:
                               logger.critical(f"CRITICAL: {log_prefix} {payment_id} ({status}) processed, but process_successful_refill FAILED for user {user_id}. Pending deposit NOT removed. Manual intervention required.")
                     except asyncio.TimeoutError:
                          logger.error(f"Timeout waiting for process_successful_refill result for {payment_id}. Pending deposit NOT removed.")
                     except Exception as e:
                          logger.error(f"Error getting result from process_successful_refill for {payment_id}: {e}. Pending deposit NOT removed.", exc_info=True)
                 else:
                     logger.warning(f"{log_prefix} {payment_id} ({status}): Calculated credited EUR is zero for user {user_id}. Removing pending deposit without updating balance.")
                     asyncio.run_coroutine_threadsafe(asyncio.to_thread(remove_pending_deposit, payment_id, trigger="zero_credit"), main_loop)
        except (ValueError, TypeError) as e:
            logger.error(f"Webhook Error: Invalid number format in webhook data for {payment_id}. Error: {e}. Data: {data}")
        except Exception as e:
            logger.error(f"Webhook Error: Could not process payment update {payment_id}.", exc_info=True)
    elif status in ['failed', 'expired', 'refunded']:
        logger.warning(f"Payment {payment_id} has status '{status}'. Removing pending record.")
        pending_info_for_removal = None
        try:
            pending_info_for_removal = asyncio.run_coroutine_threadsafe(
                 asyncio.to_thread(get_pending_deposit, payment_id), main_loop
            ).result(timeout=5)
        except Exception as e:
            logger.error(f"Error checking pending deposit for {payment_id} before removal/notification: {e}")
        asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(remove_pending_deposit, payment_id, trigger="failure" if status == 'failed' else "expiry"),
            main_loop
        )
        if pending_info_for_removal and telegram_app:
            user_id = pending_info_for_removal['user_id']
            is_purchase_failure = pending_info_for_removal.get('is_purchase') == 1
            try:
                conn_lang = None; user_lang = 'en'
                try:
                    conn_lang = get_db_connection()
                    c_lang = conn_lang.cursor()
                    c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
                    lang_res = c_lang.fetchone()
                    if lang_res and lang_res['language'] in LANGUAGES: user_lang = lang_res['language']
                except Exception as lang_e: logger.error(f"Failed to get lang for user {user_id} notify: {lang_e}")
                finally:
                     if conn_lang: conn_lang.close()
                lang_data_local = LANGUAGES.get(user_lang, LANGUAGES['en'])
                if is_purchase_failure: fail_msg = lang_data_local.get("crypto_purchase_failed", "Payment Failed/Expired. Your items are no longer reserved.")
                else: fail_msg = lang_data_local.get("payment_cancelled_or_expired", "Payment Status: Your payment ({payment_id}) was cancelled or expired.").format(payment_id=payment_id)
                dummy_context = ContextTypes.DEFAULT_TYPE(application=telegram_app, chat_id=user_id, user_id=user_id)
                asyncio.run_coroutine_threadsafe(send_message_with_retry(telegram_app.bot, user_id, fail_msg, parse_mode=None), main_loop)
            except Exception as notify_e: logger.error(f"Error notifying user {user_id} about failed/expired payment {payment_id}: {notify_e}")
    else:
         logger.info(f"Webhook received for payment {payment_id} with status: {status} (ignored).")
    return Response(status=200)

def call_handler_synchronously(update: Update):
    """Call handlers using the main event loop without blocking"""
    global telegram_app, main_loop
    
    logger.info(f"SYNC_HANDLER: Processing update {update.update_id} synchronously")
    
    try:
        async def process_update_wrapper():
            try:
                logger.info(f"SYNC_HANDLER: Starting process_update for update {update.update_id}")
                # Use the application's built-in update processing
                await telegram_app.process_update(update)
                logger.info(f"SYNC_HANDLER: process_update completed for update {update.update_id}")
            except Exception as e:
                logger.error(f"SYNC_HANDLER: Exception in process_update for update {update.update_id}: {e}", exc_info=True)
                # Try to send error message to user
                try:
                    if update.effective_chat and telegram_app.bot:
                        await send_message_with_retry(telegram_app.bot, update.effective_chat.id, "An error occurred processing your request. Please try again.")
                except Exception as notify_e:
                    logger.error(f"SYNC_HANDLER: Failed to notify user of error: {notify_e}")
        
        # Add callback to log completion/errors
        def log_future_result(future):
            try:
                exc = future.exception()
                if exc:
                    logger.error(f"SYNC_HANDLER: Future completed with exception for update {update.update_id}: {exc}", exc_info=exc)
                else:
                    logger.info(f"SYNC_HANDLER: Future completed successfully for update {update.update_id}")
            except Exception as e:
                logger.error(f"SYNC_HANDLER: Error checking future result: {e}")
        
        # Schedule the coroutine on the main event loop
        logger.info(f"SYNC_HANDLER: Scheduling process_update on main event loop")
        future = asyncio.run_coroutine_threadsafe(process_update_wrapper(), main_loop)
        
        # Add callback to log when the future completes
        future.add_done_callback(log_future_result)
        
        # Don't wait for the result - just let it run in the background
        logger.info(f"SYNC_HANDLER: Update processing scheduled successfully")
        return True
            
    except Exception as e:
        logger.error(f"SYNC_HANDLER: Error in synchronous handler: {e}", exc_info=True)
        return False

@flask_app.route(f"/telegram/{TOKEN}", methods=['POST'])
def telegram_webhook():
    global telegram_app, main_loop
    if not telegram_app or not main_loop:
        logger.error("Telegram webhook received but app/loop not ready.")
        return Response(status=503)
    try:
        update_data = request.get_json(force=True)
        logger.info(f"Telegram webhook received update: {update_data}")
        update = Update.de_json(update_data, telegram_app.bot)
        
        # Debug logging for update content
        if update and update.message:
            logger.info(f"DEBUG: Update {update.update_id} contains message: '{update.message.text}' from user {update.effective_user.id}")
            if update.message.text and update.message.text.startswith('/'):
                logger.info(f"DEBUG: Command detected: '{update.message.text}'")
        elif update and update.callback_query:
            logger.info(f"DEBUG: Update {update.update_id} contains callback query: '{update.callback_query.data}' from user {update.effective_user.id}")
        
        # Call handler synchronously
        logger.info(f"SYNC_APPROACH: Attempting synchronous handler processing")
        try:
            success = call_handler_synchronously(update)
            if success:
                logger.info(f"SYNC_APPROACH: Successfully processed update {update.update_id}")
            else:
                logger.error(f"SYNC_APPROACH: Failed to process update {update.update_id}")
        except Exception as sync_e:
            logger.error(f"SYNC_APPROACH: Exception in synchronous processing: {sync_e}", exc_info=True)
        
        return Response(status=200)
    except json.JSONDecodeError:
        logger.error("Telegram webhook received invalid JSON.")
        return Response("Invalid JSON", status=400)
    except Exception as e:
        logger.error(f"Error processing Telegram webhook: {e}", exc_info=True)
        return Response("Internal Server Error", status=500)

# --- Worker Product Addition Handler ---
# NOTE: The old free-form message approach for workers has been removed.
# Workers now use the structured callback-based interface in worker_interface.py:
# City → District → Type → Size → Price → Media → Confirmation

async def handle_worker_single_product_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle worker single product input"""
    user_id = update.effective_user.id
    
    # Check if user is in single product input state
    if context.user_data.get("state") != "awaiting_worker_single_product":
        return
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        await update.message.reply_text("❌ Access denied. Worker permissions required.")
        return
    
    # Check for media-based input
    incoming_caption = update.message.caption.strip() if update.message.caption else ""
    if update.message.photo or update.message.video or update.message.animation:
        # Require caption with size+price information
        if not incoming_caption:
            await update.message.reply_text("⚠️ Please add a caption containing size and price.")
            return
        input_text = incoming_caption
    else:
        if not update.message.text:
            await update.message.reply_text("Please send the product details (size and price) as text or caption on a photo/video.")
            return
        input_text = update.message.text.strip()

    # Try to extract price (look for decimal numbers)
    price_matches = re.findall(r'\d+\.?\d*', input_text)
    
    if price_matches:
        # Use the last number found as price, rest as size
        price_text = price_matches[-1]
        # Remove the price from the input to get size
        size_text = input_text.replace(price_text, '').strip()
        if not size_text:
            size_text = "1g"  # Default size if only price given
    else:
        # No numbers found - ask for price
        await update.message.reply_text("Please include a price in your message (e.g., '2g 30.00' or 'small batch 25')")
        return
    
    try:
        price_value = Decimal(price_text)
        if price_value <= 0:
            await update.message.reply_text("❌ Price must be positive. Please try again.")
            return
    except ValueError:
        await update.message.reply_text("❌ Could not understand the price. Please include a valid price number.")
        return
    
    # Get stored context data
    product_type = context.user_data.get("worker_selected_category")
    city_name = context.user_data.get("worker_single_city")
    district_name = context.user_data.get("worker_single_district")
    
    if not all([product_type, city_name, district_name]):
        await update.message.reply_text("❌ Location data lost. Please start again.")
        return
    
    # Store product details for confirmation
    context.user_data["worker_single_product"] = {
        "city": city_name,
        "district": district_name,
        "type": product_type,
        "size": size_text,
        "price": price_value,
        "original_text": input_text
    }
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 **Confirm Product Details**\n\n"
    msg += f"• **Product:** {type_emoji} {product_type}\n"
    msg += f"• **Location:** {city_name} / {district_name}\n"
    msg += f"• **Size:** {size_text}\n"
    msg += f"• **Price:** {price_value:.2f} EUR\n\n"
    msg += "✅ **Ready to add product!**"
    
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Product", callback_data="worker_confirm_single_product")],
        [InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]
    ]
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_worker_bulk_forwarded_drops_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle worker bulk products input"""
    user_id = update.effective_user.id
    
    # Check if user is in bulk products input state
    if context.user_data.get("state") != "awaiting_worker_bulk_details":
        return
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        await update.message.reply_text("❌ Access denied. Worker permissions required.")
        return
    
    if not update.message or not update.message.text:
        return  # Skip non-text messages
    
    # Flexible parsing - accept any text format
    input_text = update.message.text.strip()
    
    # Try to extract price (look for decimal numbers)
    price_matches = re.findall(r'\d+\.?\d*', input_text)
    
    if price_matches:
        # Use the last number found as price, rest as size
        price_text = price_matches[-1]
        # Remove the price from the input to get size
        size_text = input_text.replace(price_text, '').strip()
        if not size_text:
            size_text = "1g"  # Default size if only price given
    else:
        # No numbers found - ask for price
        await update.message.reply_text("Please include a price in your message (e.g., '2g 30.00' or 'small batch 25')")
        return
    
    try:
        price_value = Decimal(price_text)
        if price_value <= 0:
            await update.message.reply_text("❌ Price must be positive. Please try again.")
            return
    except ValueError:
        await update.message.reply_text("❌ Could not understand the price. Please include a valid price number.")
        return
    
    # Get current bulk products list
    bulk_products = context.user_data.get("worker_bulk_products", [])
    
    # Check if max limit reached (10 products)
    if len(bulk_products) >= 10:
        await update.message.reply_text("❌ Maximum 10 products reached. Please finish bulk adding first.")
        return
    
    # Get stored context data
    product_type = context.user_data.get("worker_selected_category")
    city_name = context.user_data.get("worker_bulk_city")
    district_name = context.user_data.get("worker_bulk_district")
    
    if not all([product_type, city_name, district_name]):
        await update.message.reply_text("❌ Location data lost. Please start again.")
        return
    
    # Add product to bulk list
    bulk_products.append({
        "city": city_name,
        "district": district_name,
        "type": product_type,
        "size": size_text,
        "price": price_value
    })
    
    context.user_data["worker_bulk_products"] = bulk_products
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"✅ Product #{len(bulk_products)} added to bulk list!\n\n"
    msg += f"• **Product:** {type_emoji} {product_type} - {size_text}\n"
    msg += f"• **Price:** {price_value:.2f} EUR\n\n"
    msg += f"**Bulk Progress:** {len(bulk_products)}/10"
    
    if len(bulk_products) < 10:
        msg += f"\n\nSend another product or finish bulk adding."
    else:
        msg += f"\n\n⚠️ **Maximum reached!** Please finish bulk adding."
    
    keyboard = [
        [InlineKeyboardButton(f"✅ Finish Bulk Add ({len(bulk_products)}/10)", callback_data="worker_bulk_finish")],
        [InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]
    ]
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

def main() -> None:
    global telegram_app, main_loop
    logger.info("Starting bot...")
    init_db()
    
    # NEW: Initialize bulk stock tables
    try:
        BulkStockManager.init_bulk_stock_tables()
        logger.info("Bulk stock tables initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing bulk stock tables: {e}")
    
    load_all_data()
    defaults = Defaults(parse_mode=None, block=False)
    app_builder = ApplicationBuilder().token(TOKEN).defaults(defaults).job_queue(JobQueue())
    app_builder.post_init(post_init)
    app_builder.post_shutdown(post_shutdown)
    application = app_builder.build()

    logger.info("Registering handlers...")
    # Register handlers (but we'll handle them synchronously via webhook)
    application.add_handler(CommandHandler("start", user.start))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL,
        handle_message
    ))
    application.add_error_handler(error_handler)
    
    logger.info("All handlers registered successfully")
    telegram_app = application
    main_loop = asyncio.get_event_loop()
    if BASKET_TIMEOUT > 0:
        job_queue = application.job_queue
        if job_queue:
            logger.info(f"Setting up background job for expired baskets (interval: 60s)...")
            job_queue.run_repeating(clear_expired_baskets_job_wrapper, interval=timedelta(seconds=60), first=timedelta(seconds=10), name="clear_baskets")
            logger.info("Background job setup complete.")
        else: logger.warning("Job Queue is not available. Basket clearing job skipped.")
    else: logger.warning("BASKET_TIMEOUT is not positive. Skipping background job setup.")

    async def setup_webhooks_and_run():
        global telegram_app, main_loop
        main_loop = asyncio.get_running_loop()
        
        logger.info("Starting setup_webhooks_and_run...")
        logger.info(f"DEBUG: Event loop set to: {main_loop}")
        logger.info(f"DEBUG: telegram_app: {telegram_app}")
        
        # Schedule recurring jobs
        job_queue = telegram_app.job_queue
        job_queue.run_repeating(clear_expired_baskets_job_wrapper, interval=BASKET_TIMEOUT, first=30, name="clear_expired_baskets")
        logger.info(f"Scheduled basket cleanup job to run every {BASKET_TIMEOUT // 60} minutes")
        
        # NEW: Schedule bulk stock monitoring job (runs every 30 minutes)
        job_queue.run_repeating(bulk_stock_monitoring_job_wrapper, interval=1800, first=60, name="bulk_stock_monitoring")
        logger.info("Scheduled bulk stock monitoring job to run every 30 minutes")
        
        # NEW: Schedule worker achievements notification job (runs every hour)
        job_queue.run_repeating(worker_achievements_notification_job_wrapper, interval=3600, first=120, name="worker_achievements")
        logger.info("Scheduled worker achievements notification job to run every hour")
        
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/telegram/{TOKEN}", allowed_updates=["message", "callback_query"])
        
        logger.info(f"Webhook set to: {WEBHOOK_URL}/telegram/{TOKEN}")
        logger.info("Telegram bot initialized and webhook configured")
        logger.info(f"DEBUG: Bot info: {await telegram_app.bot.get_me()}")
        
        # Run Flask in a separate thread so it doesn't block the event loop
        flask_thread = threading.Thread(
            target=flask_app.run,
            kwargs={
                'host': '0.0.0.0',
                'port': int(os.environ.get('PORT', 5000)),
                'debug': False,
                'use_reloader': False,
                'threaded': True
            },
            daemon=True
        )
        flask_thread.start()
        logger.info("Flask server started in background thread")
        
        # Keep the main event loop running
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")

    async def shutdown(signal, loop, application):
        logger.info(f"Received exit signal {signal.name}...")
        logger.info("Shutting down application...")
        if application:
            await application.stop()
            await application.shutdown()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        logger.info(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Flushing metrics")
        loop.stop()

    try:
        main_loop.run_until_complete(setup_webhooks_and_run())
    except (KeyboardInterrupt, SystemExit) as e:
        logger.info(f"Shutdown initiated by {type(e).__name__}.")
    except Exception as e:
        logger.critical(f"Critical error in main execution loop: {e}", exc_info=True)
    finally:
        logger.info("Main loop finished or interrupted.")
        if main_loop.is_running():
            logger.info("Stopping event loop.")
            main_loop.stop()
        logger.info("Bot shutdown complete.")

if __name__ == '__main__':
    main()

# --- END OF FILE main.py ---
