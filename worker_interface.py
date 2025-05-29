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
    context.user_data["state"] = "awaiting_worker_bulk_details"
    context.user_data["worker_bulk_products"] = []  # Initialize bulk products list
    
    type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 Add Bulk {type_emoji} {product_type} (Max 10)\n"
    msg += f"📍 {city_name} / {district_name}\n\n"
    msg += f"Send one message per product:\n\n"
    msg += f"📝 Include size and price (any format)\n"
    msg += f"Examples:\n"
    msg += f"• '2g 30.00'\n"
    msg += f"• 'small batch 25'\n"
    msg += f"• 'premium quality 1g 35'\n\n"
    msg += f"💡 Send up to 10 products, then finish."
    
    keyboard = [
        [InlineKeyboardButton("✅ Finish Bulk Add (0/10)", callback_data="worker_bulk_finish")],
        [InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    await query.answer("Send product details in chat.")

async def handle_worker_bulk_finish(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Finish bulk product addition"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    bulk_products = context.user_data.get("worker_bulk_products", [])
    
    if not bulk_products:
        return await query.answer("No products added yet. Add some products first.", show_alert=True)
    
    product_type = context.user_data.get("worker_selected_category")
    city_name = context.user_data.get("worker_bulk_city")
    district_name = context.user_data.get("worker_bulk_district")
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find or create city
        c.execute("SELECT city_id FROM cities WHERE city_name = ?", (city_name,))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (city_name) VALUES (?)", (city_name,))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT district_id FROM districts WHERE city_id = ? AND district_name = ?", (city_id, district_name))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, district_name) VALUES (?, ?)", (city_id, district_name))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert all bulk products
        product_ids = []
        type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
        
        for product in bulk_products:
            c.execute("""
                INSERT INTO products (city_id, district_id, product_type, size, price, available, added_by)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (city_id, district_id, product["type"], product["size"], product["price"], user_id))
            product_ids.append(c.lastrowid)
        
        # Log worker action
        c.execute("""
            INSERT INTO worker_actions (worker_id, action_type, details, quantity, timestamp)
            VALUES (?, 'add_bulk', ?, ?, CURRENT_TIMESTAMP)
        """, (user_id, f"Added {len(bulk_products)}x {product_type} products in {city_name}/{district_name}", len(bulk_products)))
        
        conn.commit()
        conn.close()
        
        # Success message
        msg = f"✅ **Bulk Addition Complete!**\n\n"
        msg += f"• **Products Added:** {len(bulk_products)} {type_emoji} {product_type}\n"
        msg += f"• **Location:** {city_name} / {district_name}\n"
        msg += f"• **Product IDs:** #{min(product_ids)}-#{max(product_ids)}\n\n"
        msg += f"**Product Details:**\n"
        
        for i, product in enumerate(bulk_products[:5], 1):  # Show first 5 products
            msg += f"{i}. {product['size']} - {product['price']:.2f} EUR\n"
        
        if len(bulk_products) > 5:
            msg += f"... and {len(bulk_products) - 5} more\n"
        
        msg += f"\n🎉 **Great work!** All products are now available for customers."
        
        keyboard = [
            [InlineKeyboardButton("📦 Add More Products", callback_data="worker_select_category")],
            [InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]
        ]
        
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
        # Clear worker state
        for key in ["worker_bulk_products", "worker_selected_category", "worker_bulk_city", "worker_bulk_district", "worker_bulk_city_id", "worker_bulk_district_id", "state"]:
            context.user_data.pop(key, None)
        
    except Exception as e:
        logger.error(f"Error in worker bulk finish: {e}")
        await query.answer("Error processing bulk products.", show_alert=True)

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
        c.execute("SELECT city_id FROM cities WHERE city_name = ?", (product_data["city"],))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (city_name) VALUES (?)", (product_data["city"],))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT district_id FROM districts WHERE city_id = ? AND district_name = ?", (city_id, product_data["district"]))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, district_name) VALUES (?, ?)", (city_id, product_data["district"]))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert product
        type_emoji = PRODUCT_TYPES.get(product_data["type"], DEFAULT_PRODUCT_EMOJI)
        c.execute("""
            INSERT INTO products (city_id, district_id, product_type, size, price, available, added_by)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (city_id, district_id, product_data["type"], product_data["size"], product_data["price"], user_id))
        
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
        c.execute("SELECT city_id FROM cities WHERE city_name = ?", (product_data["city"],))
        city_result = c.fetchone()
        if not city_result:
            c.execute("INSERT INTO cities (city_name) VALUES (?)", (product_data["city"],))
            city_id = c.lastrowid
        else:
            city_id = city_result[0]
        
        # Find or create district
        c.execute("SELECT district_id FROM districts WHERE city_id = ? AND district_name = ?", (city_id, product_data["district"]))
        district_result = c.fetchone()
        if not district_result:
            c.execute("INSERT INTO districts (city_id, district_name) VALUES (?, ?)", (city_id, product_data["district"]))
            district_id = c.lastrowid
        else:
            district_id = district_result[0]
        
        # Insert multiple products (up to 10 as per cap)
        bulk_quantity = min(10, product_data.get("quantity", 5))  # Default 5, max 10
        type_emoji = PRODUCT_TYPES.get(product_data["type"], DEFAULT_PRODUCT_EMOJI)
        
        product_ids = []
        for i in range(bulk_quantity):
            c.execute("""
                INSERT INTO products (city_id, district_id, product_type, size, price, available, added_by)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (city_id, district_id, product_data["type"], product_data["size"], product_data["price"], user_id))
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

# --- END OF FILE worker_interface.py --- 