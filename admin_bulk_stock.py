"""
Admin Interface for Bulk Stock Management

This module provides Telegram bot handlers for admin users to manage bulk stock items,
replenishment rules, and view notification history. It integrates with the main admin
interface and provides a comprehensive UI for bulk stock operations.

Admin Functions:
1. Manage Bulk Stock Items (add, view, edit, deactivate)
2. Manage Replenishment Rules (add, view, edit, deactivate) 
3. View Worker Notifications History
4. Manual Stock Updates and Processing
"""

import logging
import json
import os
from typing import List, Dict, Optional
from datetime import datetime

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

# Local imports
from utils import (
    get_db_connection, send_message_with_retry, log_admin_action,
    LANGUAGES, ADMIN_ID, SECONDARY_ADMIN_IDS, load_product_types
)
from bulk_stock_management import BulkStockManager

# Configure logging
logger = logging.getLogger(__name__)

# Constants for pagination
BULK_STOCK_ITEMS_PER_PAGE = 8
WORKERS_PER_PAGE = 8


class AdminBulkStockHandlers:
    """Admin interface handlers for bulk stock management"""
    
    @staticmethod
    async def handle_bulk_stock_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Main bulk stock management menu"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        # Check admin permissions
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            # Get counts for display
            bulk_items_count = BulkStockManager.get_bulk_stock_item_count()
            rules_count = len(BulkStockManager.get_replenishment_rules(limit=1000))  # Get total count
            
            message = (
                f"📦 *Bulk Stock Management*\n\n"
                f"📊 Active Bulk Items: {bulk_items_count}\n"
                f"📋 Active Rules: {rules_count}\n\n"
                f"Choose an option below:"
            )
            
            keyboard = [
                [InlineKeyboardButton("📦 Manage Bulk Stock Items", callback_data="admin_bulk_items|0")],
                [InlineKeyboardButton("📋 Manage Replenishment Rules", callback_data="admin_replenishment_rules|0")],
                [InlineKeyboardButton("👷 View Worker Notifications", callback_data="admin_bulk_notifications|0")],
                [InlineKeyboardButton("➕ Add New Bulk Stock Item", callback_data="admin_add_bulk_stock")],
                [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
            ]
            
            await query.edit_message_text(
                message, 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error in bulk stock menu: {e}")
            await query.edit_message_text("❌ Error loading bulk stock menu", parse_mode=None)
    
    @staticmethod
    async def handle_bulk_items_list(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Display paginated list of bulk stock items"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            offset = int(params[0]) if params and params[0].isdigit() else 0
            
            items = BulkStockManager.get_bulk_stock_items(offset=offset, limit=BULK_STOCK_ITEMS_PER_PAGE)
            total_count = BulkStockManager.get_bulk_stock_item_count()
            
            if not items:
                message = "📦 No bulk stock items found.\n\nClick 'Add New' to create your first bulk stock item."
                keyboard = [
                    [InlineKeyboardButton("➕ Add New Bulk Stock Item", callback_data="admin_add_bulk_stock")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ]
            else:
                current_page = (offset // BULK_STOCK_ITEMS_PER_PAGE) + 1
                total_pages = (total_count + BULK_STOCK_ITEMS_PER_PAGE - 1) // BULK_STOCK_ITEMS_PER_PAGE
                
                message = f"📦 *Bulk Stock Items* (Page {current_page}/{total_pages})\n\n"
                
                keyboard = []
                for item in items:
                    status_icon = "✅" if item['is_active'] else "❌"
                    processed_icon = "🔄" if item['is_processed'] else "📦"
                    
                    button_text = f"{status_icon}{processed_icon} {item['name']} ({item['current_quantity']}{item['unit']})"
                    callback_data = f"admin_view_bulk_item|{item['id']}|{offset}"
                    
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
                
                # Navigation buttons
                nav_buttons = []
                if offset > 0:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_bulk_items|{max(0, offset - BULK_STOCK_ITEMS_PER_PAGE)}"))
                if offset + BULK_STOCK_ITEMS_PER_PAGE < total_count:
                    nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_bulk_items|{offset + BULK_STOCK_ITEMS_PER_PAGE}"))
                
                if nav_buttons:
                    keyboard.append(nav_buttons)
                
                keyboard.extend([
                    [InlineKeyboardButton("➕ Add New Item", callback_data="admin_add_bulk_stock")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error displaying bulk items list: {e}")
            await query.edit_message_text("❌ Error loading bulk stock items", parse_mode=None)
    
    @staticmethod
    async def handle_view_bulk_item(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Display detailed view of a bulk stock item"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            if not params or len(params) < 2:
                await query.edit_message_text("❌ Invalid item reference", parse_mode=None)
                return
            
            item_id = int(params[0])
            return_offset = int(params[1]) if params[1].isdigit() else 0
            
            # Get item details
            items = BulkStockManager.get_bulk_stock_items(limit=1000)  # Get all to find specific item
            item = next((i for i in items if i['id'] == item_id), None)
            
            if not item:
                await query.edit_message_text("❌ Bulk stock item not found", parse_mode=None)
                return
            
            # Format item details
            status = "Active" if item['is_active'] else "Inactive"
            processed = "Yes" if item['is_processed'] else "No"
            created = datetime.fromisoformat(item['created_at']).strftime("%Y-%m-%d %H:%M")
            updated = datetime.fromisoformat(item['updated_at']).strftime("%Y-%m-%d %H:%M")
            
            message = (
                f"📦 *Bulk Stock Item Details*\n\n"
                f"🏷️ *Name:* {item['name']}\n"
                f"📊 *Current Quantity:* {item['current_quantity']} {item['unit']}\n"
                f"👷 *Assigned Worker:* @{item['worker_username']}\n"
                f"📋 *Status:* {status}\n"
                f"🔄 *Processed:* {processed}\n"
                f"📅 *Created:* {created}\n"
                f"🔄 *Updated:* {updated}\n\n"
                f"📝 *Pickup Instructions:*\n{item['pickup_instructions']}"
            )
            
            keyboard = [
                [InlineKeyboardButton("✏️ Update Quantity", callback_data=f"admin_update_bulk_quantity|{item_id}|{return_offset}")],
                [InlineKeyboardButton("🔄 Mark as Processed", callback_data=f"admin_mark_bulk_processed|{item_id}|{return_offset}")],
                [InlineKeyboardButton("📋 View Rules", callback_data=f"admin_bulk_item_rules|{item_id}|{return_offset}")],
                [InlineKeyboardButton("❌ Deactivate", callback_data=f"admin_deactivate_bulk_item|{item_id}|{return_offset}")],
                [InlineKeyboardButton("⬅️ Back to List", callback_data=f"admin_bulk_items|{return_offset}")]
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error viewing bulk item: {e}")
            await query.edit_message_text("❌ Error loading item details", parse_mode=None)
    
    @staticmethod
    async def handle_add_bulk_stock_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Start the process of adding a new bulk stock item"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            # Initialize the adding process
            context.user_data['adding_bulk_stock'] = {
                'step': 'name',
                'data': {}
            }
            
            message = (
                "➕ *Add New Bulk Stock Item*\n\n"
                "Step 1/5: Enter the name for this bulk stock item\n"
                "(e.g., 'Raw Bananas - 200kg Crate')\n\n"
                "💡 Use descriptive names that workers will easily understand."
            )
            
            keyboard = [
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")]
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error starting add bulk stock: {e}")
            await query.edit_message_text("❌ Error starting bulk stock creation", parse_mode=None)
    
    @staticmethod
    async def handle_replenishment_rules_list(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Display paginated list of replenishment rules"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            offset = int(params[0]) if params and params[0].isdigit() else 0
            
            rules = BulkStockManager.get_replenishment_rules(offset=offset)
            total_count = len(BulkStockManager.get_replenishment_rules(limit=1000))  # Get total count
            
            if not rules:
                message = "📋 No replenishment rules found.\n\nRules automatically notify workers when sellable products run low."
                keyboard = [
                    [InlineKeyboardButton("➕ Add New Rule", callback_data="admin_add_replenishment_rule")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ]
            else:
                current_page = (offset // 8) + 1  # REPLENISHMENT_RULES_PER_PAGE
                total_pages = (total_count + 7) // 8
                
                message = f"📋 *Replenishment Rules* (Page {current_page}/{total_pages})\n\n"
                
                keyboard = []
                for rule in rules:
                    button_text = f"📦 {rule['bulk_stock_name']} → {rule['sellable_product_type_name']} (≤{rule['low_stock_threshold']})"
                    callback_data = f"admin_view_rule|{rule['id']}|{offset}"
                    
                    keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
                
                # Navigation buttons
                nav_buttons = []
                if offset > 0:
                    nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_replenishment_rules|{max(0, offset - 8)}"))
                if offset + 8 < total_count:
                    nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_replenishment_rules|{offset + 8}"))
                
                if nav_buttons:
                    keyboard.append(nav_buttons)
                
                keyboard.extend([
                    [InlineKeyboardButton("➕ Add New Rule", callback_data="admin_add_replenishment_rule")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error displaying replenishment rules: {e}")
            await query.edit_message_text("❌ Error loading replenishment rules", parse_mode=None)


# Message handlers for text input during bulk stock creation
class AdminBulkStockMessageHandlers:
    """Handle text message inputs during bulk stock item creation"""
    
    @staticmethod
    async def handle_bulk_stock_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for bulk stock creation process"""
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            return
        
        # Check if user is in bulk stock adding process
        if 'adding_bulk_stock' not in context.user_data:
            return
        
        adding_data = context.user_data['adding_bulk_stock']
        step = adding_data['step']
        text = update.message.text.strip()
        
        try:
            if step == 'name':
                await AdminBulkStockMessageHandlers._handle_name_input(update, context, text)
            elif step == 'quantity':
                await AdminBulkStockMessageHandlers._handle_quantity_input(update, context, text)
            elif step == 'unit':
                await AdminBulkStockMessageHandlers._handle_unit_input(update, context, text)
            elif step == 'instructions':
                await AdminBulkStockMessageHandlers._handle_instructions_input(update, context, text)
            elif step == 'worker':
                await AdminBulkStockMessageHandlers._handle_worker_input(update, context, text)
            
        except Exception as e:
            logger.error(f"Error handling bulk stock text input: {e}")
            await update.message.reply_text("❌ Error processing input. Please try again.")
    
    @staticmethod
    async def _handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Handle bulk stock item name input"""
        if len(text) < 3:
            await update.message.reply_text("❌ Name must be at least 3 characters long. Please try again.")
            return
        
        if len(text) > 100:
            await update.message.reply_text("❌ Name must be less than 100 characters. Please try again.")
            return
        
        # Store name and move to next step
        context.user_data['adding_bulk_stock']['data']['name'] = text
        context.user_data['adding_bulk_stock']['step'] = 'quantity'
        
        message = (
            f"✅ Name set: *{text}*\n\n"
            f"Step 2/5: Enter the initial quantity\n"
            f"(e.g., 200, 50.5, 1000)\n\n"
            f"💡 This will be the starting amount before any processing."
        )
        
        keyboard = [
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")]
        ]
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def _handle_quantity_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Handle bulk stock item quantity input"""
        try:
            quantity = float(text)
            if quantity <= 0:
                await update.message.reply_text("❌ Quantity must be greater than 0. Please try again.")
                return
        except ValueError:
            await update.message.reply_text("❌ Invalid quantity format. Please enter a number (e.g., 200, 50.5).")
            return
        
        # Store quantity and move to next step
        context.user_data['adding_bulk_stock']['data']['quantity'] = quantity
        context.user_data['adding_bulk_stock']['step'] = 'unit'
        
        message = (
            f"✅ Quantity set: *{quantity}*\n\n"
            f"Step 3/5: Enter the unit of measurement\n"
            f"(e.g., kg, L, pieces, boxes, tons)\n\n"
            f"💡 Use units that workers will understand."
        )
        
        keyboard = [
            [InlineKeyboardButton("kg", callback_data="admin_bulk_unit|kg")],
            [InlineKeyboardButton("L", callback_data="admin_bulk_unit|L")],
            [InlineKeyboardButton("pieces", callback_data="admin_bulk_unit|pieces")],
            [InlineKeyboardButton("boxes", callback_data="admin_bulk_unit|boxes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")]
        ]
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def _handle_unit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Handle bulk stock item unit input"""
        if len(text) < 1:
            await update.message.reply_text("❌ Unit cannot be empty. Please try again.")
            return
        
        if len(text) > 20:
            await update.message.reply_text("❌ Unit must be less than 20 characters. Please try again.")
            return
        
        # Store unit and move to next step
        context.user_data['adding_bulk_stock']['data']['unit'] = text
        context.user_data['adding_bulk_stock']['step'] = 'instructions'
        
        message = (
            f"✅ Unit set: *{text}*\n\n"
            f"Step 4/5: Enter pickup instructions for workers\n\n"
            f"💡 Be specific about location, access codes, contact info, etc.\n"
            f"📸 After this step, you can optionally add photos/videos."
        )
        
        keyboard = [
            [InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")]
        ]
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    async def _handle_instructions_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Handle bulk stock item pickup instructions input"""
        if len(text) < 10:
            await update.message.reply_text("❌ Instructions must be at least 10 characters long. Please try again.")
            return
        
        if len(text) > 1000:
            await update.message.reply_text("❌ Instructions must be less than 1000 characters. Please try again.")
            return
        
        # Store instructions and move to worker selection
        context.user_data['adding_bulk_stock']['data']['instructions'] = text
        context.user_data['adding_bulk_stock']['step'] = 'worker'
        
        # Get list of workers (users with worker role)
        workers = AdminBulkStockMessageHandlers._get_available_workers()
        
        if not workers:
            await update.message.reply_text(
                "❌ No workers available. Please add workers first in the Worker Management section.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")]])
            )
            return
        
        message = (
            f"✅ Instructions set\n\n"
            f"Step 5/5: Select a worker to assign this bulk stock item\n\n"
            f"Available workers:"
        )
        
        keyboard = []
        for worker in workers[:10]:  # Limit to first 10 workers
            keyboard.append([InlineKeyboardButton(f"👷 @{worker['username']}", callback_data=f"admin_assign_worker|{worker['user_id']}")])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_bulk_stock_menu")])
        
        await update.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    @staticmethod
    def _get_available_workers():
        """Get list of users with worker role"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Get users who are workers (assuming there's a worker role system)
            # For now, get all users who aren't admins
            admin_ids = [ADMIN_ID] + SECONDARY_ADMIN_IDS
            admin_placeholders = ','.join(['?' for _ in admin_ids])
            
            cursor.execute(f"""
                SELECT user_id, username
                FROM users
                WHERE user_id NOT IN ({admin_placeholders})
                AND username IS NOT NULL
                ORDER BY username
            """, admin_ids)
            
            workers = []
            for row in cursor.fetchall():
                workers.append({
                    'user_id': row[0],
                    'username': row[1]
                })
            
            return workers
            
        except Exception as e:
            logger.error(f"Error fetching workers: {e}")
            return []
        finally:
            if 'conn' in locals():
                conn.close()


# Export handler functions for integration with main bot
BULK_STOCK_HANDLERS = {
    "admin_bulk_stock_menu": AdminBulkStockHandlers.handle_bulk_stock_menu,
    "admin_bulk_items": AdminBulkStockHandlers.handle_bulk_items_list,
    "admin_view_bulk_item": AdminBulkStockHandlers.handle_view_bulk_item,
    "admin_add_bulk_stock": AdminBulkStockHandlers.handle_add_bulk_stock_start,
    "admin_replenishment_rules": AdminBulkStockHandlers.handle_replenishment_rules_list,
} 