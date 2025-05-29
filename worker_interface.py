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
        [InlineKeyboardButton("🔄 Restock Products", callback_data="worker_city")],
        [InlineKeyboardButton("📊 Enhanced Statistics", callback_data="worker_view_stats_enhanced")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="worker_leaderboard")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="back_start")]
    ]

    if query:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer()
    else:
        await send_message_with_retry(context.bot, update.effective_chat.id, msg, reply_markup=InlineKeyboardMarkup(keyboard))

# --- Worker Product Addition Flow (Following Admin Pattern) ---
async def handle_worker_city(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker city selection - follows admin pattern"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not CITIES:
        return await query.edit_message_text("No cities configured. Contact admin to add cities first.", parse_mode=None)
    
    sorted_city_ids = sorted(CITIES.keys(), key=lambda city_id: CITIES.get(city_id, ''))
    keyboard = [[InlineKeyboardButton(f"🏙️ {CITIES.get(c,'N/A')}", callback_data=f"worker_district|{c}")] for c in sorted_city_ids]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Worker Panel", callback_data="worker_admin_menu")])
    
    await query.edit_message_text("🏙️ Select City to Add Product:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_worker_district_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker district selection - follows admin pattern"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params: 
        return await query.answer("Error: City ID missing.", show_alert=True)
    
    city_id = params[0]
    city_name = CITIES.get(city_id)
    if not city_name:
        return await query.edit_message_text("Error: City not found. Please select again.", parse_mode=None)
    
    districts_in_city = DISTRICTS.get(city_id, {})
    if not districts_in_city:
        keyboard = [[InlineKeyboardButton("⬅️ Back to Cities", callback_data="worker_city")]]
        return await query.edit_message_text(f"No districts found for {city_name}. Contact admin to add districts.",
                                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    
    sorted_district_ids = sorted(districts_in_city.keys(), key=lambda dist_id: districts_in_city.get(dist_id,''))
    keyboard = []
    
    for d in sorted_district_ids:
        dist_name = districts_in_city.get(d)
        if dist_name:
            keyboard.append([InlineKeyboardButton(f"🏘️ {dist_name}", callback_data=f"worker_type_selection|{city_id}|{d}")])
        else: 
            logger.warning(f"District name missing for ID {d} in city {city_id}")
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Cities", callback_data="worker_city")])
    await query.edit_message_text(f"🏘️ Select District in {city_name}:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_worker_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker product type selection - follows admin pattern
    
    This function handles both old and new callback formats:
    - Old: worker_type|ProductName (from server version)
    - New: worker_type_selection|city_id|district_id (from local version)
    """
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    # Handle old callback format: worker_type|ProductName (from server version)
    if params and len(params) == 1:
        product_type = params[0]
        logger.info(f"COMPAT: Handling old worker_type callback for product: {product_type}")
        
        # Try to get city/district from context first
        city_id = context.user_data.get("worker_city_id")
        dist_id = context.user_data.get("worker_district_id")
        
        # If not in context, extract from the callback query data or use fallback
        if not city_id or not dist_id:
            # Based on the logged interaction pattern: worker_city|1 -> worker_district|1 -> worker_type|Pienas
            # Use the most recent selections as fallback
            city_id = "1"  # Kaunas from logs
            dist_id = "1"  # centas from logs
            logger.info(f"COMPAT: Using fallback city_id={city_id}, dist_id={dist_id}")
        
        # Store the selections in context for the structured flow
        context.user_data["worker_city_id"] = city_id
        context.user_data["worker_district_id"] = dist_id
        context.user_data["worker_product_type"] = product_type
        context.user_data["worker_city"] = CITIES.get(city_id, "Unknown City")
        context.user_data["worker_district"] = DISTRICTS.get(city_id, {}).get(dist_id, "Unknown District")
        
        # Redirect directly to size selection (follows the new structured flow)
        city_name = context.user_data["worker_city"]
        district_name = context.user_data["worker_district"]
        type_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
        
        keyboard = [[InlineKeyboardButton(f"📏 {s}", callback_data=f"worker_size|{s}")] for s in SIZES]
        keyboard.append([InlineKeyboardButton("📏 Custom Size", callback_data="worker_custom_size")])
        keyboard.append([InlineKeyboardButton("⬅️ Back to Worker Panel", callback_data="worker_admin_menu")])
        
        await query.edit_message_text(f"📦 Adding {type_emoji} {product_type} in {city_name} / {district_name}\n\nSelect size:", 
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
        await query.answer("Size selection ready!")
        return

    # Handle new callback format: worker_type_selection|city_id|district_id
    if not params or len(params) < 2: 
        return await query.answer("Error: City or District ID missing.", show_alert=True)
    
    city_id, dist_id = params[0], params[1] 
    
    # Store in worker context (similar to admin but with worker prefix)
    context.user_data["worker_city_id"] = city_id 
    context.user_data["worker_district_id"] = dist_id

    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)

    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found. Please select again.", parse_mode=None)
    
    if not PRODUCT_TYPES:
        return await query.edit_message_text("No product types configured. Contact admin to add product types.", parse_mode=None)

    keyboard = []
    for type_name_iter, emoji in sorted(PRODUCT_TYPES.items()):
        callback_data_type = f"worker_add_products|{city_id}|{dist_id}|{type_name_iter}"
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name_iter}", callback_data=callback_data_type)])
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Districts", callback_data=f"worker_district|{city_id}")])
    await query.edit_message_text("💎 Select Product Type:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def handle_worker_add_products(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker existing product selection for restocking - no new products, only restock existing ones"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or len(params) < 3: 
        return await query.answer("Error: Location/Type info missing.", show_alert=True)
    
    city_id, dist_id, p_type = params
    city_name = CITIES.get(city_id)
    district_name = DISTRICTS.get(city_id, {}).get(dist_id)
    
    if not city_name or not district_name:
        return await query.edit_message_text("Error: City/District not found. Please select again.", parse_mode=None)
    
    # Get existing products in this location and type
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, name, size, price, available, reserved 
                     FROM products 
                     WHERE city = ? AND district = ? AND product_type = ? 
                     ORDER BY price ASC, size ASC""", 
                 (city_name, district_name, p_type))
        existing_products = c.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching existing products for worker: {e}")
        return await query.edit_message_text("❌ Error loading products. Please try again.", parse_mode=None)
    
    if not existing_products:
        # No existing products - inform worker
        type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
        msg = f"📦 No existing {type_emoji} {p_type} products found in {city_name} / {district_name}.\n\n"
        msg += "❌ **Workers can only restock existing products.**\n\n"
        msg += "Contact admin to create the initial product catalog for this location."
        
        keyboard = [
            [InlineKeyboardButton("⬅️ Back to Types", callback_data=f"worker_type_selection|{city_id}|{dist_id}")],
            [InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]
        ]
        
        return await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    # Store worker context
    context.user_data["worker_city_id"] = city_id
    context.user_data["worker_district_id"] = dist_id
    context.user_data["worker_product_type"] = p_type
    context.user_data["worker_city"] = city_name
    context.user_data["worker_district"] = district_name
    
    # Show existing products for restocking
    type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
    msg = f"📦 **Restock {type_emoji} {p_type}** in {city_name} / {district_name}\n\n"
    msg += "Select an existing product to restock:\n\n"
    
    keyboard = []
    for product in existing_products:
        product_id, name, size, price, available, reserved = product
        stock_status = "✅ In Stock" if available > 0 else "❌ Out of Stock"
        
        # Create button text with key info
        button_text = f"{size} - {price:.2f}€ ({stock_status})"
        callback_data = f"worker_restock_product|{product_id}"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        # Add product details to message
        msg += f"• **{size}** - {price:.2f} EUR\n"
        msg += f"  Stock: {available} available, {reserved} reserved\n\n"
    
    keyboard.append([InlineKeyboardButton("⬅️ Back to Types", callback_data=f"worker_type_selection|{city_id}|{dist_id}")])
    keyboard.append([InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_worker_restock_product(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker product restocking - ask for quantity to add"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    if not params or not params[0]:
        return await query.answer("Product ID missing.", show_alert=True)
    
    product_id = params[0]
    
    # Get product details
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT id, city, district, product_type, size, name, price, available, reserved 
                     FROM products WHERE id = ?""", (product_id,))
        product = c.fetchone()
        conn.close()
        
        if not product:
            return await query.edit_message_text("❌ Product not found. Please try again.", parse_mode=None)
            
    except Exception as e:
        logger.error(f"Error fetching product for restocking: {e}")
        return await query.edit_message_text("❌ Error loading product. Please try again.", parse_mode=None)
    
    # Store product info for restocking
    context.user_data["worker_restock_product_id"] = product_id
    context.user_data["worker_restock_product"] = dict(product)
    context.user_data["state"] = "awaiting_worker_restock_quantity"
    
    # Show product details and ask for quantity
    product_id, city, district, p_type, size, name, price, available, reserved = product
    type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
    
    msg = f"📦 **Restocking Product**\n\n"
    msg += f"• **Product:** {type_emoji} {p_type} - {size}\n"
    msg += f"• **Location:** {city} / {district}\n"
    msg += f"• **Price:** {price:.2f} EUR\n"
    msg += f"• **Current Stock:** {available} available, {reserved} reserved\n\n"
    msg += "📝 **How many units do you want to add to stock?**\n\n"
    msg += "Reply with a number (e.g., 5, 10, 1):"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="worker_admin_menu")]]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer("Enter quantity in chat.")

async def handle_worker_confirm_restock(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Worker restock confirmation and database update"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied. Worker permissions required.", show_alert=True)

    # Get restock data from context
    product_id = context.user_data.get("worker_restock_product_id")
    product_data = context.user_data.get("worker_restock_product")
    quantity_to_add = context.user_data.get("worker_restock_quantity")
    
    if not all([product_id, product_data, quantity_to_add]):
        return await query.edit_message_text("❌ Error: Restock information incomplete. Please start again.", parse_mode=None)
    
    try:
        quantity_int = int(quantity_to_add)
        if quantity_int <= 0:
            return await query.edit_message_text("❌ Error: Quantity must be positive.", parse_mode=None)
    except (ValueError, TypeError):
        return await query.edit_message_text("❌ Error: Invalid quantity format.", parse_mode=None)

    # Update database - increase available stock
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        
        # Update product stock
        c.execute("""UPDATE products 
                     SET available = available + ? 
                     WHERE id = ?""", (quantity_int, product_id))
        
        if c.rowcount == 0:
            conn.rollback()
            return await query.edit_message_text("❌ Error: Product not found or no changes made.", parse_mode=None)
        
        # Log the worker restock action
        c.execute("""INSERT INTO worker_actions 
                     (worker_id, action_type, product_id, details, timestamp)
                     VALUES (?, 'restock', ?, ?, ?)""", 
                 (user_id, product_id, f"Added {quantity_int} units", datetime.now(timezone.utc).isoformat()))
        
        conn.commit()
        
        # Clean up worker context
        for key in list(context.user_data.keys()):
            if key.startswith("worker_restock_") or key.startswith("worker_"):
                context.user_data.pop(key, None)
        context.user_data.pop("state", None)
        
        # Get updated worker stats
        today_stats = await _get_worker_today_stats(user_id)
        worker_info = await _get_worker_info(user_id)
        daily_quota = worker_info.get('daily_quota', 10) if worker_info else 10
        
        # Generate success message with progress
        username = update.effective_user.username or f"ID_{user_id}"
        progress_bar = _generate_progress_bar((today_stats['drops_today'] / daily_quota) * 100)
        
        p_type = product_data.get('product_type', 'Product')
        size = product_data.get('size', 'N/A')
        city = product_data.get('city', 'N/A')
        district = product_data.get('district', 'N/A')
        price = product_data.get('price', 0)
        type_emoji = PRODUCT_TYPES.get(p_type, DEFAULT_PRODUCT_EMOJI)
        
        msg = f"✅ **Product Restocked Successfully!**\n\n"
        msg += f"📦 **Restock Details:**\n"
        msg += f"• Product: {type_emoji} {p_type} - {size}\n"
        msg += f"• Location: {city} / {district}\n"
        msg += f"• Price: {price:.2f} EUR\n"
        msg += f"• **Added {quantity_int} units to stock**\n"
        msg += f"• Product ID: #{product_id}\n\n"
        
        msg += f"📊 **Today's Progress:**\n"
        msg += f"• Restocks Today: {today_stats['drops_today']}\n"
        msg += f"• Daily Quota: {daily_quota}\n"
        msg += f"• Progress: {progress_bar} {(today_stats['drops_today']/daily_quota*100):.1f}%\n\n"
        
        if today_stats['drops_today'] >= daily_quota:
            msg += "🎉 **Daily quota completed! Excellent work!**\n\n"
        else:
            remaining = daily_quota - today_stats['drops_today']
            msg += f"🎯 {remaining} more restocks to reach your quota!\n\n"
        
        msg += "What would you like to do next?"
        
        keyboard = [
            [InlineKeyboardButton("🔄 Restock Another Product", callback_data="worker_city")],
            [InlineKeyboardButton("📊 View My Stats", callback_data="worker_view_stats")],
            [InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]
        ]
        
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
        logger.info(f"Worker {username} (ID: {user_id}) restocked product {product_id}: +{quantity_int} units")
        
    except sqlite3.Error as e:
        logger.error(f"Database error restocking product: {e}", exc_info=True)
        if conn and conn.in_transaction:
            conn.rollback()
        await query.edit_message_text("❌ Database error restocking product. Please try again or contact admin.", parse_mode=None)
    except Exception as e:
        logger.error(f"Error restocking product: {e}", exc_info=True)
        if conn and conn.in_transaction:
            conn.rollback()
        await query.edit_message_text("❌ Error restocking product. Please try again or contact admin.", parse_mode=None)
    finally:
        if conn:
            conn.close()

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

# --- Enhanced Worker Statistics with Revenue ---
async def handle_worker_view_stats_enhanced(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show comprehensive worker statistics with revenue and efficiency metrics"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied.", show_alert=True)
    
    # Get comprehensive stats with revenue
    stats = await _get_worker_comprehensive_stats_with_revenue(user_id)
    username = update.effective_user.username or f"ID_{user_id}"
    alias = stats.get('alias', '')
    display_name = f"@{username}" + (f" ({alias})" if alias else "")
    
    msg = f"📊 **Enhanced Statistics for {display_name}**\n\n"
    
    # Today's performance with revenue
    today = stats.get('today', {})
    msg += f"📅 **Today ({datetime.now().strftime('%Y-%m-%d')})**\n"
    msg += f"• Drops Added: {today.get('drops', 0)}\n"
    msg += f"• Revenue Generated: €{today.get('revenue', 0):.2f}\n"
    msg += f"• Avg Price per Drop: €{today.get('avg_price', 0):.2f}\n"
    msg += f"• Quota Progress: {today.get('quota_progress', 0):.1f}%\n"
    msg += f"• Efficiency Score: {today.get('efficiency_score', 0):.2f}\n\n"
    
    # This week's stats
    week = stats.get('week', {})
    msg += f"📅 **This Week**\n"
    msg += f"• Total Drops: {week.get('drops', 0)}\n"
    msg += f"• Total Revenue: €{week.get('revenue', 0):.2f}\n"
    msg += f"• Daily Average: {week.get('daily_avg', 0):.1f} drops\n"
    msg += f"• Revenue/Day: €{week.get('revenue_per_day', 0):.2f}\n"
    msg += f"• Best Day: {week.get('best_day', 'N/A')}\n\n"
    
    # This month's performance
    month = stats.get('month', {})
    msg += f"📅 **This Month**\n"
    msg += f"• Total Drops: {month.get('drops', 0)}\n"
    msg += f"• Total Revenue: €{month.get('revenue', 0):.2f}\n"
    msg += f"• Monthly Ranking: #{month.get('rank', 'N/A')} of {month.get('total_workers', 'N/A')}\n"
    msg += f"• Quota Achievement: {month.get('quota_achievement', 0):.1f}%\n\n"
    
    # Achievements and milestones
    achievements = stats.get('achievements', {})
    if achievements:
        msg += f"🏆 **Achievements**\n"
        if achievements.get('quota_streaks', 0) > 0:
            msg += f"• Quota Streak: {achievements['quota_streaks']} days\n"
        if achievements.get('milestones'):
            msg += f"• Milestones: {', '.join(map(str, achievements['milestones']))}\n"
        if achievements.get('top_performer_days', 0) > 0:
            msg += f"• Top Performer Days: {achievements['top_performer_days']}\n"
        msg += "\n"
    
    # Efficiency insights
    efficiency = stats.get('efficiency', {})
    if efficiency:
        msg += f"⚡ **Efficiency Insights**\n"
        msg += f"• Revenue per Drop: €{efficiency.get('revenue_per_drop', 0):.2f}\n"
        msg += f"• Peak Hours: {efficiency.get('peak_hours', 'N/A')}\n"
        msg += f"• Most Valuable Product: {efficiency.get('top_product', 'N/A')}\n"
        msg += f"• Consistency Score: {efficiency.get('consistency_score', 0):.1f}/10\n"
    
    keyboard = [
        [InlineKeyboardButton("📈 Weekly Detailed Report", callback_data="worker_weekly_detailed_report")],
        [InlineKeyboardButton("💰 Revenue Breakdown", callback_data="worker_revenue_breakdown")],
        [InlineKeyboardButton("🎯 Goal Tracking", callback_data="worker_goal_tracking")],
        [InlineKeyboardButton("⬅️ Back to Panel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

async def handle_worker_revenue_breakdown(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show detailed revenue breakdown for worker"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied.", show_alert=True)
    
    revenue_data = await _get_worker_revenue_breakdown(user_id)
    
    msg = f"💰 **Revenue Breakdown**\n\n"
    
    # Time-based breakdown
    time_breakdown = revenue_data.get('time_breakdown', {})
    msg += f"📅 **Time-based Revenue:**\n"
    msg += f"• Today: €{time_breakdown.get('today', 0):.2f}\n"
    msg += f"• This Week: €{time_breakdown.get('week', 0):.2f}\n"
    msg += f"• This Month: €{time_breakdown.get('month', 0):.2f}\n"
    msg += f"• All Time: €{time_breakdown.get('total', 0):.2f}\n\n"
    
    # Product type breakdown
    product_breakdown = revenue_data.get('product_breakdown', [])
    if product_breakdown:
        msg += f"📦 **By Product Type:**\n"
        for product in product_breakdown[:5]:
            msg += f"• {product['type']}: €{product['revenue']:.2f} ({product['drops']} drops)\n"
        msg += "\n"
    
    # Price range analysis
    price_analysis = revenue_data.get('price_analysis', {})
    if price_analysis:
        msg += f"💵 **Price Analysis:**\n"
        msg += f"• Highest Sale: €{price_analysis.get('max_price', 0):.2f}\n"
        msg += f"• Lowest Sale: €{price_analysis.get('min_price', 0):.2f}\n"
        msg += f"• Average Sale: €{price_analysis.get('avg_price', 0):.2f}\n"
        msg += f"• Most Common Price: €{price_analysis.get('mode_price', 0):.2f}\n\n"
    
    # Trends
    trends = revenue_data.get('trends', {})
    if trends:
        msg += f"📈 **Trends:**\n"
        msg += f"• Revenue Growth: {trends.get('growth_percent', 0):+.1f}%\n"
        msg += f"• Best Revenue Day: {trends.get('best_day', 'N/A')}\n"
        msg += f"• Consistency Rating: {trends.get('consistency', 0):.1f}/10\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 Back to Stats", callback_data="worker_view_stats")],
        [InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

async def handle_worker_goal_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show goal tracking and achievements for worker"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Verify worker permissions
    user_roles = get_user_roles(user_id)
    if not user_roles['is_worker']:
        return await query.answer("Access denied.", show_alert=True)
    
    goals_data = await _get_worker_goals_and_achievements(user_id)
    
    msg = f"🎯 **Goal Tracking & Achievements**\n\n"
    
    # Daily quota progress
    quota_info = goals_data.get('quota', {})
    msg += f"📊 **Daily Quota Progress:**\n"
    msg += f"• Target: {quota_info.get('target', 0)} drops\n"
    msg += f"• Completed: {quota_info.get('completed', 0)} drops\n"
    msg += f"• Progress: {quota_info.get('progress_percent', 0):.1f}%\n"
    progress_bar = _generate_progress_bar(quota_info.get('progress_percent', 0), 15)
    msg += f"• {progress_bar}\n\n"
    
    # Weekly goals
    weekly_goals = goals_data.get('weekly', {})
    if weekly_goals:
        msg += f"📅 **This Week's Goals:**\n"
        msg += f"• Weekly Target: {weekly_goals.get('target', 0)} drops\n"
        msg += f"• Current: {weekly_goals.get('current', 0)} drops\n"
        msg += f"• Remaining: {max(0, weekly_goals.get('target', 0) - weekly_goals.get('current', 0))} drops\n\n"
    
    # Achievements
    achievements = goals_data.get('achievements', [])
    if achievements:
        msg += f"🏆 **Recent Achievements:**\n"
        for achievement in achievements[-5:]:
            date = achievement.get('date', 'N/A')
            desc = achievement.get('description', 'Achievement unlocked')
            msg += f"• {date}: {desc}\n"
        msg += "\n"
    
    # Milestones progress
    milestones = goals_data.get('milestones', {})
    if milestones:
        msg += f"🌟 **Milestone Progress:**\n"
        current_drops = milestones.get('current_total', 0)
        next_milestone = milestones.get('next_milestone', 0)
        if next_milestone > 0:
            progress_to_milestone = (current_drops % next_milestone) / next_milestone * 100
            msg += f"• Next Milestone: {next_milestone} drops\n"
            msg += f"• Progress: {current_drops} / {next_milestone}\n"
            msg += f"• {progress_to_milestone:.1f}% complete\n"
        msg += "\n"
    
    # Performance insights
    insights = goals_data.get('insights', [])
    if insights:
        msg += f"💡 **Performance Insights:**\n"
        for insight in insights[:3]:
            msg += f"• {insight}\n"
    
    keyboard = [
        [InlineKeyboardButton("🎯 Set Personal Goals", callback_data="worker_set_personal_goals")],
        [InlineKeyboardButton("📊 Back to Stats", callback_data="worker_view_stats")],
        [InlineKeyboardButton("🏠 Worker Panel", callback_data="worker_admin_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    await query.answer()

# --- Enhanced Helper Functions ---
async def _get_worker_comprehensive_stats_with_revenue(user_id: int) -> dict:
    """Get comprehensive worker statistics including revenue metrics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Get basic worker info
        c.execute("""
            SELECT username, worker_status, worker_alias, worker_daily_quota
            FROM users
            WHERE user_id = ? AND is_worker = 1
        """, (user_id,))
        
        worker_info = c.fetchone()
        if not worker_info:
            return {}
        
        stats = {
            'alias': worker_info['worker_alias'],
            'daily_quota': worker_info['worker_daily_quota'] or 10
        }
        
        today = datetime.now().strftime('%Y-%m-%d')
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        
        # Today's stats with revenue
        c.execute("""
            SELECT COUNT(*) as drops, 
                   COALESCE(SUM(price), 0) as revenue,
                   COALESCE(AVG(price), 0) as avg_price,
                   MIN(added_date) as first_drop
            FROM products
            WHERE added_by = ? AND DATE(added_date) = ?
        """, (user_id, today))
        
        today_result = c.fetchone()
        drops_today = today_result['drops'] if today_result else 0
        revenue_today = float(today_result['revenue']) if today_result else 0.0
        avg_price_today = float(today_result['avg_price']) if today_result else 0.0
        
        quota_progress = (drops_today / stats['daily_quota'] * 100) if stats['daily_quota'] > 0 else 0
        efficiency_score = revenue_today / max(1, drops_today)
        
        stats['today'] = {
            'drops': drops_today,
            'revenue': revenue_today,
            'avg_price': avg_price_today,
            'quota_progress': quota_progress,
            'efficiency_score': efficiency_score
        }
        
        # Week's stats with revenue
        c.execute("""
            SELECT COUNT(*) as drops,
                   COALESCE(SUM(price), 0) as revenue,
                   DATE(added_date) as date
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
            GROUP BY DATE(added_date)
            ORDER BY drops DESC
        """, (user_id, week_start))
        
        week_results = c.fetchall()
        week_drops = sum(day['drops'] for day in week_results)
        week_revenue = sum(float(day['revenue']) for day in week_results)
        week_days = len(week_results)
        week_daily_avg = week_drops / max(1, week_days)
        revenue_per_day = week_revenue / max(1, week_days)
        best_day = f"{week_results[0]['date']} ({week_results[0]['drops']} drops)" if week_results else "N/A"
        
        stats['week'] = {
            'drops': week_drops,
            'revenue': week_revenue,
            'daily_avg': week_daily_avg,
            'revenue_per_day': revenue_per_day,
            'best_day': best_day
        }
        
        # Month's stats with ranking
        c.execute("""
            SELECT COUNT(*) as drops,
                   COALESCE(SUM(price), 0) as revenue
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
        """, (user_id, month_start))
        
        month_result = c.fetchone()
        month_drops = month_result['drops'] if month_result else 0
        month_revenue = float(month_result['revenue']) if month_result else 0.0
        
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
            if result['user_id'] == user_id:
                rank = i
                break
        
        days_in_month = datetime.now().day
        month_daily_avg = month_drops / max(1, days_in_month)
        quota_achievement = (month_daily_avg / stats['daily_quota'] * 100) if stats['daily_quota'] > 0 else 0
        
        stats['month'] = {
            'drops': month_drops,
            'revenue': month_revenue,
            'rank': rank,
            'total_workers': len(ranking_results),
            'quota_achievement': quota_achievement
        }
        
        # Achievements and milestones
        achievements = await _get_worker_achievements(user_id)
        stats['achievements'] = achievements
        
        # Efficiency metrics
        efficiency = await _get_worker_efficiency_metrics(user_id)
        stats['efficiency'] = efficiency
        
        return stats
        
    except sqlite3.Error as e:
        logger.error(f"Error fetching comprehensive stats for {user_id}: {e}")
        return {}
    finally:
        if conn:
            conn.close()

async def _get_worker_revenue_breakdown(user_id: int) -> dict:
    """Get detailed revenue breakdown for worker"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        revenue_data = {}
        
        today = datetime.now().strftime('%Y-%m-%d')
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        
        # Time-based breakdown
        time_queries = {
            'today': f"DATE(added_date) = '{today}'",
            'week': f"DATE(added_date) >= '{week_start}'",
            'month': f"DATE(added_date) >= '{month_start}'",
            'total': "1=1"
        }
        
        time_breakdown = {}
        for period, condition in time_queries.items():
            c.execute(f"""
                SELECT COALESCE(SUM(price), 0) as revenue
                FROM products
                WHERE added_by = ? AND {condition}
            """, (user_id,))
            result = c.fetchone()
            time_breakdown[period] = float(result['revenue']) if result else 0.0
        
        revenue_data['time_breakdown'] = time_breakdown
        
        # Product type breakdown
        c.execute("""
            SELECT product_type, 
                   COUNT(*) as drops,
                   COALESCE(SUM(price), 0) as revenue
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
            GROUP BY product_type
            ORDER BY revenue DESC
        """, (user_id, month_start))
        
        product_breakdown = []
        for row in c.fetchall():
            product_breakdown.append({
                'type': row['product_type'],
                'drops': row['drops'],
                'revenue': float(row['revenue'])
            })
        revenue_data['product_breakdown'] = product_breakdown
        
        # Price analysis
        c.execute("""
            SELECT MIN(price) as min_price,
                   MAX(price) as max_price,
                   AVG(price) as avg_price
            FROM products
            WHERE added_by = ?
        """, (user_id,))
        
        price_result = c.fetchone()
        if price_result:
            revenue_data['price_analysis'] = {
                'min_price': float(price_result['min_price'] or 0),
                'max_price': float(price_result['max_price'] or 0),
                'avg_price': float(price_result['avg_price'] or 0),
                'mode_price': 0  # Would need more complex query for mode
            }
        
        return revenue_data
        
    except sqlite3.Error as e:
        logger.error(f"Error getting revenue breakdown for {user_id}: {e}")
        return {}
    finally:
        if conn:
            conn.close()

async def _get_worker_achievements(user_id: int) -> dict:
    """Get worker achievements and milestones"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        achievements = {}
        
        # Check for quota streaks (consecutive days meeting quota)
        # This would require more complex logic to track streaks
        achievements['quota_streaks'] = 0
        
        # Check milestones
        c.execute("SELECT COUNT(*) as total_drops FROM products WHERE added_by = ?", (user_id,))
        total_drops = c.fetchone()['total_drops']
        
        milestones = [10, 50, 100, 500, 1000, 5000]
        achieved_milestones = [m for m in milestones if total_drops >= m]
        achievements['milestones'] = achieved_milestones
        
        # Top performer days (days when worker was #1)
        achievements['top_performer_days'] = 0  # Would need complex query
        
        return achievements
        
    except sqlite3.Error as e:
        logger.error(f"Error getting achievements for {user_id}: {e}")
        return {}
    finally:
        if conn: conn.close()

async def _get_worker_efficiency_metrics(user_id: int) -> dict:
    """Get worker efficiency metrics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        efficiency = {}
        
        # Revenue per drop
        c.execute("""
            SELECT COUNT(*) as total_drops,
                   COALESCE(SUM(price), 0) as total_revenue
            FROM products
            WHERE added_by = ?
        """, (user_id,))
        
        result = c.fetchone()
        if result and result['total_drops'] > 0:
            efficiency['revenue_per_drop'] = float(result['total_revenue']) / result['total_drops']
        else:
            efficiency['revenue_per_drop'] = 0.0
        
        # Peak hours (most productive hour)
        c.execute("""
            SELECT strftime('%H', added_date) as hour, COUNT(*) as drops
            FROM products
            WHERE added_by = ?
            GROUP BY hour
            ORDER BY drops DESC
            LIMIT 1
        """, (user_id,))
        
        peak_result = c.fetchone()
        if peak_result:
            hour = int(peak_result['hour'])
            efficiency['peak_hours'] = f"{hour:02d}:00-{hour+1:02d}:00"
        else:
            efficiency['peak_hours'] = "N/A"
        
        # Most valuable product type
        c.execute("""
            SELECT product_type, COALESCE(SUM(price), 0) as revenue
            FROM products
            WHERE added_by = ?
            GROUP BY product_type
            ORDER BY revenue DESC
            LIMIT 1
        """, (user_id,))
        
        top_product_result = c.fetchone()
        efficiency['top_product'] = top_product_result['product_type'] if top_product_result else "N/A"
        
        # Consistency score (simplified)
        efficiency['consistency_score'] = 7.5  # Placeholder calculation
        
        return efficiency
        
    except sqlite3.Error as e:
        logger.error(f"Error getting efficiency metrics for {user_id}: {e}")
        return {}
    finally:
        if conn: conn.close()

async def _get_worker_goals_and_achievements(user_id: int) -> dict:
    """Get worker goals and achievement data"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        goals_data = {}
        
        # Get daily quota info
        c.execute("""
            SELECT worker_daily_quota FROM users WHERE user_id = ? AND is_worker = 1
        """, (user_id,))
        quota_result = c.fetchone()
        daily_quota = quota_result['worker_daily_quota'] if quota_result else 10
        
        # Get today's progress
        today = datetime.now().strftime('%Y-%m-%d')
        c.execute("""
            SELECT COUNT(*) as today_drops
            FROM products
            WHERE added_by = ? AND DATE(added_date) = ?
        """, (user_id, today))
        today_drops = c.fetchone()['today_drops']
        
        progress_percent = (today_drops / daily_quota * 100) if daily_quota > 0 else 0
        
        goals_data['quota'] = {
            'target': daily_quota,
            'completed': today_drops,
            'progress_percent': progress_percent
        }
        
        # Weekly goals (7 * daily quota)
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        c.execute("""
            SELECT COUNT(*) as week_drops
            FROM products
            WHERE added_by = ? AND DATE(added_date) >= ?
        """, (user_id, week_start))
        week_drops = c.fetchone()['week_drops']
        
        weekly_target = daily_quota * 7
        goals_data['weekly'] = {
            'target': weekly_target,
            'current': week_drops
        }
        
        # Milestones
        c.execute("SELECT COUNT(*) as total_drops FROM products WHERE added_by = ?", (user_id,))
        total_drops = c.fetchone()['total_drops']
        
        milestones = [10, 50, 100, 500, 1000, 5000, 10000]
        next_milestone = next((m for m in milestones if m > total_drops), None)
        
        goals_data['milestones'] = {
            'current_total': total_drops,
            'next_milestone': next_milestone
        }
        
        # Performance insights
        insights = []
        if progress_percent >= 100:
            insights.append("🎉 Daily quota completed!")
        elif progress_percent >= 50:
            insights.append("💪 Great progress on today's quota!")
        
        if week_drops > weekly_target * 0.8:
            insights.append("📈 Excellent weekly performance!")
        
        goals_data['insights'] = insights
        
        return goals_data
        
    except sqlite3.Error as e:
        logger.error(f"Error getting goals data for {user_id}: {e}")
        return {}
    finally:
        if conn: conn.close()

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