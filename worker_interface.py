# --- START OF FILE worker_interface.py ---

import logging
import sqlite3
import math
import os
import time
import tempfile
import shutil
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import telegram.error as telegram_error

# --- Local Imports ---
from utils import (
    ADMIN_ID, SECONDARY_ADMIN_IDS, LANGUAGES, CITIES, DISTRICTS, PRODUCT_TYPES,
    get_db_connection, send_message_with_retry, _get_lang_data,
    log_admin_action, get_user_roles, DEFAULT_PRODUCT_EMOJI, SIZES, MEDIA_DIR,
    load_all_data
)

logger = logging.getLogger(__name__)

# --- Worker Main Menu ---
async def handle_worker_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Main admin menu for workers - simplified without quotas/leaderboards"""
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

    username = update.effective_user.username or f"ID_{user_id}"
    alias = f" ({worker_info['worker_alias']})" if worker_info['worker_alias'] else ""
    
    msg = f"👷 Worker Panel: @{username}{alias}\n\n"
    msg += f"Select a product category to add products:\n\n"
    
    keyboard = [
        [InlineKeyboardButton("📦 Add Products", callback_data="worker_select_category")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_start")]
    ]

    if query:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer()
    else:
        await send_message_with_retry(context.bot, update.effective_chat.id, msg, reply_markup=InlineKeyboardMarkup(keyboard))

# --- NEW: Product Category Selection ---
async def handle_worker_select_category(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show existing product categories for workers to choose from"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not PRODUCT_TYPES:
        return await query.edit_message_text("No product types configured. Contact admin.", parse_mode=None)
    
    msg = "📦 Select Product Category:\n\n"
    
    keyboard = []
    for type_name, emoji in sorted(PRODUCT_TYPES.items()):
        callback_data = f"worker_category_chosen|{type_name}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name}", callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Worker Panel", callback_data="worker_admin_menu")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

# --- NEW: Add Type Selection (Single vs Bulk) ---
async def handle_worker_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show options to add single or bulk products for chosen category"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Error: Product type missing.", show_alert=True)
    
    product_type = params[0]
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    # Store selected category
    context.user_data["worker_selected_category"] = product_type
    
    msg = f"📦 {type_emoji} {product_type}\n\n"
    msg += f"Choose how many products to add:\n\n"
    
    keyboard = [
        [InlineKeyboardButton("1️⃣ Add Single Product", callback_data="worker_add_single")],
        [InlineKeyboardButton("📦 Add Bulk Products (Max 10)", callback_data="worker_add_bulk")],
        [InlineKeyboardButton("⬅️ Back to Categories", callback_data="worker_select_category")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

# --- NEW: Single Product Addition ---
async def handle_worker_add_single(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle single product addition for workers"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    product_type = context.user_data.get("worker_selected_category")
    if not product_type:
        return await query.edit_message_text("Error: Product category not selected. Please start again.", parse_mode=None)
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    msg = f"📦 Add Single {type_emoji} {product_type}\n\n"
    msg += f"Now select location:\n\n"
    
    # Show cities
    if not CITIES:
        return await query.edit_message_text("No cities configured. Contact admin.", parse_mode=None)
    
    keyboard = []
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    for city_id in sorted_city_ids:
        city_name = CITIES.get(city_id, 'N/A')
        callback_data = f"worker_single_city|{city_id}"
        keyboard.append([InlineKeyboardButton(f"🏙️ {city_name}", callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"worker_category_chosen|{product_type}")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

# --- NEW: Bulk Product Addition ---
async def handle_worker_add_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle bulk product addition for workers (max 10)"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    product_type = context.user_data.get("worker_selected_category")
    if not product_type:
        return await query.edit_message_text("Error: Product category not selected. Please start again.", parse_mode=None)
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    msg = f"📦 Add Bulk {type_emoji} {product_type}\n\n"
    msg += f"Now select location:\n\n"
    
    # Show cities
    if not CITIES:
        return await query.edit_message_text("No cities configured. Contact admin.", parse_mode=None)
    
    keyboard = []
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    for city_id in sorted_city_ids:
        city_name = CITIES.get(city_id, 'N/A')
        callback_data = f"worker_bulk_city|{city_id}"
        keyboard.append([InlineKeyboardButton(f"🏙️ {city_name}", callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"worker_category_chosen|{product_type}")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

# --- Single Product Flow ---
async def handle_worker_single_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle city selection for single product"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Error: City ID missing.", show_alert=True)
    
    city_id = params[0]
    city_name = CITIES.get(city_id)
    product_type = context.user_data.get("worker_selected_category")
    
    if not city_name or not product_type:
        return await query.edit_message_text("Error: City or product type not found.", parse_mode=None)
    
    context.user_data["worker_single_city_id"] = city_id
    context.user_data["worker_single_city"] = city_name
    
    # Show districts for this city
    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="worker_add_single")]]
        return await query.edit_message_text(f"No districts found for {city_name}. Contact admin.", 
                                           reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 Add Single {type_emoji} {product_type}\n"
    msg += f"📍 {city_name}\n\n"
    msg += f"Select district:\n\n"
    
    keyboard = []
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id, ''))
    for dist_id in sorted_district_ids:
        dist_name = districts_in_city.get(dist_id)
        if dist_name:
            callback_data = f"worker_single_district|{dist_id}"
            keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="worker_add_single")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_worker_single_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle district selection for single product"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Error: District ID missing.", show_alert=True)
    
    dist_id = params[0]
    city_id = context.user_data.get("worker_single_city_id")
    city_name = context.user_data.get("worker_single_city")
    product_type = context.user_data.get("worker_selected_category")
    
    if not city_id or not product_type:
        return await query.edit_message_text("Error: Missing location data.", parse_mode=None)
    
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not district_name:
        return await query.edit_message_text("Error: District not found.", parse_mode=None)
    
    context.user_data["worker_single_district_id"] = dist_id
    context.user_data["worker_single_district"] = district_name
    context.user_data["state"] = "awaiting_worker_single_product"
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 Add Single {type_emoji} {product_type}\n"
    msg += f"📍 {city_name} / {district_name}\n\n"
    msg += f"Send a message with product details:\n\n"
    msg += f"📝 Include size and price (any format)\n"
    msg += f"Examples:\n"
    msg += f"• '2g 30.00'\n"
    msg += f"• 'small batch 25'\n"
    msg += f"• 'premium quality 1g 35'\n\n"
    msg += f"💡 Just make sure to include a price number!"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send product details in chat.")

# --- Bulk Product Flow ---
async def handle_worker_bulk_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle city selection for bulk products"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Error: City ID missing.", show_alert=True)
    
    city_id = params[0]
    city_name = CITIES.get(city_id)
    product_type = context.user_data.get("worker_selected_category")
    
    if not city_name or not product_type:
        return await query.edit_message_text("Error: City or product type not found.", parse_mode=None)
    
    context.user_data["worker_bulk_city_id"] = city_id
    context.user_data["worker_bulk_city"] = city_name
    
    # Show districts for this city
    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="worker_add_bulk")]]
        return await query.edit_message_text(f"No districts found for {city_name}. Contact admin.", 
                                           reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 Add Bulk {type_emoji} {product_type}\n"
    msg += f"📍 {city_name}\n\n"
    msg += f"Select district:\n\n"
    
    keyboard = []
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id, ''))
    for dist_id in sorted_district_ids:
        dist_name = districts_in_city.get(dist_id)
        if dist_name:
            callback_data = f"worker_bulk_district|{dist_id}"
            keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="worker_add_bulk")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer()

async def handle_worker_bulk_district(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle district selection for bulk products"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Error: District ID missing.", show_alert=True)
    
    dist_id = params[0]
    city_id = context.user_data.get("worker_bulk_city_id")
    city_name = context.user_data.get("worker_bulk_city")
    product_type = context.user_data.get("worker_selected_category")
    
    if not city_id or not product_type:
        return await query.edit_message_text("Error: Missing location data.", parse_mode=None)
    
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    if not district_name:
        return await query.edit_message_text("Error: District not found.", parse_mode=None)
    
    context.user_data["worker_bulk_district_id"] = dist_id
    context.user_data["worker_bulk_district"] = district_name
    context.user_data["state"] = "awaiting_worker_bulk_forwarded_drops"  # Changed state
    context.user_data["worker_bulk_items_added_count"] = 0  # Track successful adds
    context.user_data["worker_bulk_items_failed"] = []  # Track failed adds
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 **Bulk Add {type_emoji} {product_type}** (Max 10)\n"
    msg += f"📍 **{city_name} / {district_name}**\n\n"
    msg += f"🔄 **Now forward your product messages:**\n\n"
    msg += f"📝 Each message should have:\n"
    msg += f"• **Media** (photo/video/GIF)\n"
    msg += f"• **Caption** with product details\n\n"
    msg += f"💡 Forward up to 10 messages, then click finish.\n"
    msg += f"📊 **Progress:** 0/10 added"
    
    keyboard = [
        [InlineKeyboardButton("✅ Finish Bulk Add (0/10)", callback_data="worker_bulk_finish")],
        [InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer("Forward product messages with media + captions")
    
    # Store message info for progress updates
    if query.message:
        context.user_data["worker_bulk_setup_message_id"] = query.message.message_id
        context.user_data["worker_bulk_setup_chat_id"] = query.message.chat_id

async def handle_worker_bulk_finish(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handle finishing the bulk product addition"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    state = context.user_data.get("state")
    if state != "awaiting_worker_bulk_forwarded_drops":
        return await query.answer("No active bulk session found.", show_alert=True)
    
    await query.answer("Finishing bulk add session...")
    await _finish_worker_bulk_session(update, context, "Worker manually finished bulk add session.")

async def handle_worker_confirm_single_product(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Confirm and add single product to database"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    # Get product details from context
    product_data = context.user_data.get("worker_single_product")
    if not product_data:
        await query.edit_message_text("❌ Product data lost. Please start again.")
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find or create city
        c.execute("SELECT id FROM cities WHERE name = ?", (product_data["city"],))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (name) VALUES (?)", (product_data["city"],))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT id FROM districts WHERE city_id = ? AND name = ?", (city_id, product_data["district"]))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, name) VALUES (?, ?)", (city_id, product_data["district"]))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert product
        type_emoji = PRODUCT_TYPES.get(product_data["type"], DEFAULT_PRODUCT_EMOJI)
        c.execute("""
            INSERT INTO products (city, district, product_type, size, name, price, available, added_by, original_text, added_date)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
        """, (product_data["city"], product_data["district"], product_data["type"], product_data["size"], "Worker Product", product_data["price"], user_id, product_data.get("original_text", f"{product_data['size']} {product_data['price']} EUR")))
        
        product_id = c.lastrowid
        
        # Log worker action
        c.execute("""
            INSERT INTO worker_actions (worker_id, action_type, product_id, details, quantity, timestamp)
            VALUES (?, 'add_single', ?, ?, 1, CURRENT_TIMESTAMP)
        """, (user_id, product_id, f"Added {product_data['type']} - {product_data['size']} in {product_data['city']}/{product_data['district']}"))
        
        conn.commit()
        conn.close()
        
        # Success message
        msg = f"✅ **Product Added Successfully!**\n\n"
        msg += f"• **Product:** {type_emoji} {product_data['type']} - {product_data['size']}\n"
        msg += f"• **Location:** {product_data['city']} / {product_data['district']}\n"
        msg += f"• **Price:** {product_data['price']:.2f} EUR\n"
        msg += f"• **Product ID:** #{product_id}\n\n"
        msg += "🎉 **Great work!** Product is now available for customers."
        
        keyboard = [
            [InlineKeyboardButton("➕ Add Another Product", callback_data="worker_select_category")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="worker_admin_menu")]
        ]
        
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
        # Clear user data
        context.user_data.pop("worker_single_product", None)
        context.user_data.pop("state", None)
        
    except Exception as e:
        logger.error(f"Error confirming single product: {e}")
        await query.answer("Error adding product to database.", show_alert=True)

async def handle_worker_confirm_bulk_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Confirm and add bulk products to database"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    # Get product details from context
    product_data = context.user_data.get("worker_bulk_products")
    if not product_data:
        await query.edit_message_text("❌ Product data lost. Please start again.")
        return

    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find or create city
        c.execute("SELECT id FROM cities WHERE name = ?", (product_data["city"],))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (name) VALUES (?)", (product_data["city"],))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT id FROM districts WHERE city_id = ? AND name = ?", (city_id, product_data["district"]))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, name) VALUES (?, ?)", (city_id, product_data["district"]))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert multiple products (up to 10 as per cap)
        bulk_quantity = min(10, product_data.get("quantity", 5))  # Default 5, max 10
        type_emoji = PRODUCT_TYPES.get(product_data["type"], DEFAULT_PRODUCT_EMOJI)
        
        product_ids = []
        for i in range(bulk_quantity):
            c.execute("""
                INSERT INTO products (city, district, product_type, size, name, price, available, added_by, original_text, added_date)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
            """, (product_data["city"], product_data["district"], product_data["type"], product_data["size"], "Worker Bulk Product", product_data["price"], user_id, product_data.get("original_text", f"{product_data['size']} {product_data['price']} EUR - Bulk #{i+1}")))
            product_ids.append(c.lastrowid)
        
        # Log worker action
        c.execute("""
            INSERT INTO worker_actions (worker_id, action_type, details, quantity, timestamp)
            VALUES (?, 'add_bulk', ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, f"Added {bulk_quantity}x {product_data['type']} - {product_data['size']} in {product_data['city']}/{product_data['district']}", bulk_quantity))
        
        conn.commit()
        conn.close()
        
        # Success message
        msg = f"✅ **Bulk Products Added Successfully!**\n\n"
        msg += f"• **Product:** {type_emoji} {product_data['type']} - {product_data['size']}\n"
        msg += f"• **Location:** {product_data['city']} / {product_data['district']}\n"
        msg += f"• **Price:** {product_data['price']:.2f} EUR each\n"
        msg += f"• **Quantity Added:** {bulk_quantity} units\n"
        msg += f"• **Product IDs:** #{min(product_ids)}-#{max(product_ids)}\n\n"
        msg += "🎉 **Excellent work!** All products are now available for customers."
        
        keyboard = [
            [InlineKeyboardButton("➕ Add More Products", callback_data="worker_select_category")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="worker_admin_menu")]
        ]
        
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
        # Clear user data
        context.user_data.pop("worker_bulk_products", None)
        context.user_data.pop("state", None)
        
    except Exception as e:
        logger.error(f"Error confirming bulk products: {e}")
        await query.answer("Error adding products to database.", show_alert=True)

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

async def _update_worker_bulk_progress_display(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update the bulk add progress display with current counts"""
    current_count = context.user_data.get("worker_bulk_items_added_count", 0)
    failed_items = context.user_data.get("worker_bulk_items_failed", [])
    product_type = context.user_data.get("worker_selected_category", "Products")
    city_name = context.user_data.get("worker_bulk_city", "Unknown")
    district_name = context.user_data.get("worker_bulk_district", "Unknown")
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    # Create updated message
    msg = f"📦 **Bulk Add {type_emoji} {product_type}** (Max 10)\n"
    msg += f"📍 **{city_name} / {district_name}**\n\n"
    msg += f"🔄 **Now forward your product messages:**\n\n"
    msg += f"📝 Each message should have:\n"
    msg += f"• **Media** (photo/video/GIF)\n"
    msg += f"• **Caption** with product details\n\n"
    msg += f"💡 Forward up to 10 messages, then click finish.\n"
    msg += f"📊 **Progress:** {current_count}/10 added"
    
    if failed_items:
        msg += f" ({len(failed_items)} failed)"
    
    # Update keyboard with current progress
    finish_text = f"✅ Finish Bulk Add ({current_count}/10)"
    if current_count >= 10:
        finish_text = "✅ Complete! (10/10)"
    elif current_count == 0:
        finish_text = "❌ Cancel (0/10)"
    
    keyboard = [
        [InlineKeyboardButton(finish_text, callback_data="worker_bulk_finish")],
        [InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]
    ]
    
    # Try to update the original message
    try:
        if update.message and update.message.chat_id:
            # Get the bulk setup message chat_id from context if available  
            setup_message_id = context.user_data.get("worker_bulk_setup_message_id")
            setup_chat_id = context.user_data.get("worker_bulk_setup_chat_id")
            
            if setup_message_id and setup_chat_id:
                await context.bot.edit_message_text(
                    chat_id=setup_chat_id,
                    message_id=setup_message_id,
                    text=msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            else:
                # Fallback: send a new message
                await send_message_with_retry(context.bot, update.message.chat_id, 
                                            f"📊 **Progress Update:** {current_count}/10 added" + 
                                            (f" ({len(failed_items)} failed)" if failed_items else ""), 
                                            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error updating bulk progress display: {e}")

async def handle_worker_bulk_forwarded_drops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process forwarded messages from workers for bulk product addition"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return
    
    # Check if in correct state
    if context.user_data.get("state") != "awaiting_worker_bulk_forwarded_drops":
        return
    
    if not update.message:
        return

    # Get bulk setup details
    product_type = context.user_data.get("worker_selected_category")
    city_name = context.user_data.get("worker_bulk_city")
    district_name = context.user_data.get("worker_bulk_district")
    
    if not all([product_type, city_name, district_name]):
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Bulk setup details lost. Please start again.", parse_mode=None)
        await _finish_worker_bulk_session(update, context, "Bulk session aborted due to missing setup data.")
        return

    current_count = context.user_data.get("worker_bulk_items_added_count", 0)
    if current_count >= 10:  # Worker limit
        logger.info(f"Worker bulk add limit already reached, but another message received.")
        await _finish_worker_bulk_session(update, context, "Worker bulk add limit of 10 reached.")
        return

    # Extract media and caption
    original_text = (update.message.caption or "").strip()
    media_info_list = []
    
    if update.message.photo:
        media_info_list.append({'type': 'photo', 'file_id': update.message.photo[-1].file_id})
    elif update.message.video:
        media_info_list.append({'type': 'video', 'file_id': update.message.video.file_id})
    elif update.message.animation:
        media_info_list.append({'type': 'gif', 'file_id': update.message.animation.file_id})
    
    if not media_info_list:
        await send_message_with_retry(context.bot, chat_id, "⚠️ **Message skipped:** No media found. Please forward messages with photos/videos.", parse_mode='Markdown')
        return
    
    if not original_text:
        await send_message_with_retry(context.bot, chat_id, "⚠️ **Message skipped:** No caption found. Caption is needed for product details.", parse_mode='Markdown')
        return

    # Try to add the product to database
    add_success = await _add_single_worker_bulk_item_to_db(context, product_type, city_name, district_name, media_info_list, original_text, user_id)

    if add_success:
        context.user_data['worker_bulk_items_added_count'] += 1
        count_now = context.user_data['worker_bulk_items_added_count']
        
        type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
        success_msg = f"✅ **Drop #{count_now} saved successfully!**\n\n"
        success_msg += f"📦 {type_emoji} {product_type}\n"
        success_msg += f"📝 {original_text[:50]}{'...' if len(original_text) > 50 else ''}\n"
        success_msg += f"📊 **Progress:** {count_now}/10"
        
        if count_now < 10:
            success_msg += f"\n\n💡 Forward next message or finish bulk adding."
        else:
            success_msg += f"\n\n🎉 **Maximum reached!** Finishing bulk add..."
        
        await send_message_with_retry(context.bot, chat_id, success_msg, parse_mode='Markdown')
        
        # Update progress display
        await _update_worker_bulk_progress_display(update, context)
        
        if count_now >= 10:
            await _finish_worker_bulk_session(update, context, "Worker bulk add limit of 10 reached.")
    else:
        # Track failed item
        failed_items = context.user_data.get("worker_bulk_items_failed", [])
        failed_items.append({
            "caption": original_text[:30] + "..." if len(original_text) > 30 else original_text,
            "reason": "Database error"
        })
        context.user_data["worker_bulk_items_failed"] = failed_items
        
        await send_message_with_retry(context.bot, chat_id, 
                                    f"❌ **Drop failed to save!**\n\n"
                                    f"📝 {original_text[:50]}{'...' if len(original_text) > 50 else ''}\n"
                                    f"🔧 **Reason:** Database error\n\n"
                                    f"💡 Try forwarding again or finish bulk adding.", 
                                    parse_mode='Markdown')
        
        # Update progress display to show failed count
        await _update_worker_bulk_progress_display(update, context)

async def _add_single_worker_bulk_item_to_db(context: ContextTypes.DEFAULT_TYPE, product_type: str, city_name: str, district_name: str, media_info_list: list, original_text: str, worker_id: int) -> bool:
    """Helper function to add a single worker bulk item to the database"""
    import asyncio
    import tempfile
    import shutil
    import os
    import time
    
    temp_dir = None
    conn = None
    product_id = None
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find or create city
        c.execute("SELECT id FROM cities WHERE name = ?", (city_name,))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (name) VALUES (?)", (city_name,))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT id FROM districts WHERE city_id = ? AND name = ?", (city_id, district_name))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, name) VALUES (?, ?)", (city_id, district_name))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert product using city/district names (not IDs) as per products table schema
        c.execute("""
            INSERT INTO products (city, district, product_type, size, name, price, available, added_by, original_text, added_date)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)
        """, (city_name, district_name, product_type, "1g", "Worker Product", 25.0, worker_id, original_text))
        
        product_id = c.lastrowid
        
        # Handle media if present
        if media_info_list and product_id:
            temp_dir_base = await asyncio.to_thread(tempfile.mkdtemp, prefix="worker_bulk_")
            temp_dir = os.path.join(temp_dir_base, str(int(time.time()*1000)))
            await asyncio.to_thread(os.makedirs, temp_dir, exist_ok=True)

            media_inserts = []
            for i, media_info in enumerate(media_info_list):
                m_type = media_info['type']
                file_id = media_info['file_id']
                file_extension = ".jpg" if m_type == "photo" else ".mp4" if m_type in ["video", "gif"] else ".dat"
                temp_file_name = f"media_{i}_{file_id}{file_extension}"
                temp_file_path = os.path.join(temp_dir, temp_file_name)
                
                try:
                    file_obj = await context.bot.get_file(file_id)
                    await file_obj.download_to_drive(custom_path=temp_file_path)
                    
                    if await asyncio.to_thread(os.path.exists, temp_file_path) and await asyncio.to_thread(os.path.getsize, temp_file_path) > 0:
                        # Move to final location
                        final_media_dir = os.path.join(MEDIA_DIR, str(product_id))
                        await asyncio.to_thread(os.makedirs, final_media_dir, exist_ok=True)
                        
                        final_path = os.path.join(final_media_dir, temp_file_name)
                        await asyncio.to_thread(shutil.move, temp_file_path, final_path)
                        media_inserts.append((product_id, m_type, final_path, file_id))
                except Exception as e:
                    logger.error(f"Error processing worker bulk media {file_id}: {e}")
                    pass
            
            # Insert media records
            if media_inserts:
                c.executemany("INSERT INTO product_media (product_id, media_type, file_path, telegram_file_id) VALUES (?, ?, ?, ?)", media_inserts)
        
        # Log worker action in worker_actions table
        c.execute("""
            INSERT INTO worker_actions (worker_id, action_type, product_id, details, quantity, timestamp)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (worker_id, 'add_bulk_forwarded', product_id, f"Added {product_type} via forwarded message in {city_name}/{district_name}", 1))
        
        conn.commit()
        logger.info(f"Worker Bulk Added: Product {product_id} by worker {worker_id} via forwarded message.")
        return True
        
    except Exception as e:
        if conn and conn.in_transaction:
            conn.rollback()
        logger.error(f"Error adding worker bulk item to DB: {e}", exc_info=True)
        return False
    finally:
        if conn:
            conn.close()
        if temp_dir and await asyncio.to_thread(os.path.exists, temp_dir):
            await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)

async def _finish_worker_bulk_session(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str = "Worker bulk add session ended."):
    """Cleans up worker bulk add context and shows summary"""
    chat_id = None
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    elif update.message:
        chat_id = update.message.chat_id
    
    if not chat_id:
        logger.error("_finish_worker_bulk_session: could not determine chat_id.")
        return

    success_count = context.user_data.get('worker_bulk_items_added_count', 0)
    failed_items = context.user_data.get('worker_bulk_items_failed', [])
    product_type = context.user_data.get('worker_selected_category', 'Products')
    
    # Create summary message
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    final_message = f"📊 **Bulk Add Complete!**\n\n"
    final_message += f"🎯 **{message}**\n\n"
    final_message += f"✅ **Successfully added:** {success_count} {type_emoji} {product_type}\n"
    
    if failed_items:
        final_message += f"❌ **Failed:** {len(failed_items)} items\n\n"
        final_message += f"**Failed items:**\n"
        for i, failed in enumerate(failed_items[:3], 1):  # Show first 3 failures
            final_message += f"{i}. {failed['caption']} - {failed['reason']}\n"
        if len(failed_items) > 3:
            final_message += f"... and {len(failed_items) - 3} more\n"
    else:
        final_message += f"🎉 **All items processed successfully!**"
    
    # Clear worker bulk context
    keys_to_pop = ['state', 'worker_selected_category', 'worker_bulk_city', 'worker_bulk_district', 
                   'worker_bulk_city_id', 'worker_bulk_district_id', 'worker_bulk_items_added_count', 'worker_bulk_items_failed']
    for key in keys_to_pop:
        context.user_data.pop(key, None)
    
    await send_message_with_retry(context.bot, chat_id, final_message, parse_mode='Markdown')
    
    kb = [[InlineKeyboardButton("📦 Add More Products", callback_data="worker_select_category"),
           InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]]
    await send_message_with_retry(context.bot, chat_id, "What would you like to do next?", reply_markup=InlineKeyboardMarkup(kb), parse_mode=None)

# --- END OF FILE worker_interface.py --- 