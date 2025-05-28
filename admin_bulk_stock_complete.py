"""
Complete Bulk Stock Management Handlers

This module contains the remaining handlers for bulk stock management including:
- Completing bulk stock item creation (worker assignment, media handling)
- Replenishment rule creation and management
- Bulk stock item updates and status changes
- Integration callbacks for inline buttons
"""

import logging
import sqlite3
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


class CompleteBulkStockHandlers:
    """Complete set of bulk stock management handlers"""
    
    @staticmethod
    async def handle_assign_worker(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Handle worker assignment for bulk stock item"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params or 'adding_bulk_stock' not in context.user_data:
            await query.edit_message_text("❌ Creation process lost. Please start again.", parse_mode=None)
            return
        
        try:
            worker_id = int(params[0])
            adding_data = context.user_data['adding_bulk_stock']['data']
            
            # Create bulk stock item in database
            bulk_stock_id = BulkStockManager.add_bulk_stock_item(
                name=adding_data['name'],
                initial_quantity=adding_data['quantity'],
                unit=adding_data['unit'],
                pickup_instructions=adding_data['instructions'],
                assigned_worker_id=worker_id
            )
            
            if bulk_stock_id:
                # Log admin action
                log_admin_action(
                    admin_id=user_id,
                    action="BULK_STOCK_CREATED",
                    target_user_id=worker_id,
                    reason=f"Created bulk stock item: {adding_data['name']}"
                )
                
                # Clear creation state
                context.user_data.pop('adding_bulk_stock', None)
                
                # Get worker info
                worker_username = await CompleteBulkStockHandlers._get_worker_username(worker_id)
                
                message = (
                    f"✅ *Bulk Stock Item Created Successfully!*\n\n"
                    f"📦 Name: {adding_data['name']}\n"
                    f"📊 Quantity: {adding_data['quantity']} {adding_data['unit']}\n"
                    f"👷 Assigned Worker: @{worker_username}\n\n"
                    f"You can now:\n"
                    f"• Add media for pickup instructions\n"
                    f"• Create replenishment rules\n"
                    f"• View and manage the item"
                )
                
                keyboard = [
                    [InlineKeyboardButton("📸 Add Media", callback_data=f"admin_add_bulk_media|{bulk_stock_id}")],
                    [InlineKeyboardButton("📋 Create Rule", callback_data=f"admin_add_rule_for_bulk|{bulk_stock_id}")],
                    [InlineKeyboardButton("📦 View Item", callback_data=f"admin_view_bulk_item|{bulk_stock_id}|0")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ]
                
                await query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text(
                    "❌ Failed to create bulk stock item. Name might already exist.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_bulk_stock_menu")]])
                )
            
        except Exception as e:
            logger.error(f"Error assigning worker to bulk stock: {e}")
            await query.edit_message_text("❌ Error creating bulk stock item", parse_mode=None)
    
    @staticmethod
    async def handle_bulk_unit_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Handle unit selection via inline button"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params or 'adding_bulk_stock' not in context.user_data:
            await query.edit_message_text("❌ Creation process lost. Please start again.", parse_mode=None)
            return
        
        try:
            unit = params[0]
            
            # Store unit and move to instructions step
            context.user_data['adding_bulk_stock']['data']['unit'] = unit
            context.user_data['adding_bulk_stock']['step'] = 'instructions'
            
            message = (
                f"✅ Unit set: *{unit}*\n\n"
                f"Step 4/5: Enter pickup instructions for workers\n\n"
                f"💡 Be specific about location, access codes, contact info, etc.\n"
                f"📸 After this step, you can optionally add photos/videos."
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
            logger.error(f"Error handling bulk unit selection: {e}")
            await query.edit_message_text("❌ Error processing unit selection", parse_mode=None)
    
    @staticmethod
    async def handle_add_replenishment_rule_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Start creating a new replenishment rule"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        try:
            # Get available bulk stock items
            bulk_items = BulkStockManager.get_bulk_stock_items(limit=100)
            
            if not bulk_items:
                message = "❌ No bulk stock items available.\n\nCreate bulk stock items first before adding replenishment rules."
                keyboard = [
                    [InlineKeyboardButton("➕ Add Bulk Stock Item", callback_data="admin_add_bulk_stock")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="admin_bulk_stock_menu")]
                ]
                await query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=None
                )
                return
            
            message = (
                "📋 *Create New Replenishment Rule*\n\n"
                "Step 1/3: Select the bulk stock item that should trigger notifications\n\n"
                "Available bulk stock items:"
            )
            
            keyboard = []
            for item in bulk_items[:15]:  # Limit display
                button_text = f"📦 {item['name']} ({item['current_quantity']}{item['unit']})"
                callback_data = f"admin_rule_select_bulk|{item['id']}"
                keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
            
            keyboard.append([InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error starting replenishment rule creation: {e}")
            await query.edit_message_text("❌ Error starting rule creation", parse_mode=None)
    
    @staticmethod
    async def handle_rule_select_bulk_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Handle bulk stock selection for replenishment rule"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params:
            await query.edit_message_text("❌ Invalid bulk stock selection", parse_mode=None)
            return
        
        try:
            bulk_stock_id = int(params[0])
            
            # Get available product types
            product_types = load_product_types()
            
            if not product_types:
                message = "❌ No product types available.\n\nCreate product types first in 'Manage Product Types'."
                keyboard = [
                    [InlineKeyboardButton("⬅️ Back", callback_data="admin_add_replenishment_rule")]
                ]
                await query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=None
                )
                return
            
            # Store selection and move to product type selection
            context.user_data['creating_rule'] = {
                'bulk_stock_id': bulk_stock_id,
                'step': 'product_type'
            }
            
            # Get bulk stock item name
            items = BulkStockManager.get_bulk_stock_items(limit=1000)
            selected_item = next((i for i in items if i['id'] == bulk_stock_id), None)
            item_name = selected_item['name'] if selected_item else f"ID {bulk_stock_id}"
            
            message = (
                f"📋 *Create Replenishment Rule*\n\n"
                f"✅ Bulk Stock: {item_name}\n\n"
                f"Step 2/3: Select the sellable product type to monitor\n\n"
                f"When this product type's total stock falls below the threshold, "
                f"the assigned worker will be notified to process the bulk stock."
            )
            
            keyboard = []
            for type_name, emoji in sorted(product_types.items()):
                callback_data = f"admin_rule_select_type|{type_name}"
                keyboard.append([InlineKeyboardButton(f"{emoji} {type_name}", callback_data=callback_data)])
            
            # Add navigation
            if len(keyboard) > 10:
                keyboard = keyboard[:10]
                keyboard.append([InlineKeyboardButton("➡️ More Types", callback_data="admin_rule_more_types|0")])
            
            keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_add_replenishment_rule")])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error selecting bulk stock for rule: {e}")
            await query.edit_message_text("❌ Error processing selection", parse_mode=None)
    
    @staticmethod
    async def handle_rule_select_product_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Handle product type selection for replenishment rule"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params or 'creating_rule' not in context.user_data:
            await query.edit_message_text("❌ Rule creation process lost", parse_mode=None)
            return
        
        try:
            product_type = params[0]
            
            # Store product type selection
            context.user_data['creating_rule']['product_type'] = product_type
            context.user_data['creating_rule']['step'] = 'threshold'
            
            # Get current stock level for this product type
            current_stock = BulkStockManager._get_current_sellable_stock(product_type)
            
            message = (
                f"📋 *Create Replenishment Rule*\n\n"
                f"✅ Product Type: {product_type}\n"
                f"📊 Current Stock: {current_stock} items\n\n"
                f"Step 3/3: Enter the low stock threshold\n\n"
                f"When the total available stock of '{product_type}' products "
                f"falls to this number or below, a notification will be sent.\n\n"
                f"💡 Recommended: Set threshold higher than your typical daily sales."
            )
            
            # Suggest some thresholds based on current stock
            keyboard = []
            suggested_thresholds = []
            
            if current_stock > 50:
                suggested_thresholds = [10, 20, 30, 50]
            elif current_stock > 20:
                suggested_thresholds = [5, 10, 15]
            elif current_stock > 5:
                suggested_thresholds = [2, 5]
            else:
                suggested_thresholds = [1, 2, 3]
            
            for threshold in suggested_thresholds:
                if threshold <= current_stock:
                    keyboard.append([InlineKeyboardButton(f"{threshold} items", callback_data=f"admin_rule_set_threshold|{threshold}")])
            
            keyboard.extend([
                [InlineKeyboardButton("✏️ Custom Threshold", callback_data="admin_rule_custom_threshold")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"admin_rule_select_bulk|{context.user_data['creating_rule']['bulk_stock_id']}")]
            ])
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error selecting product type for rule: {e}")
            await query.edit_message_text("❌ Error processing selection", parse_mode=None)
    
    @staticmethod
    async def handle_rule_set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Handle threshold setting for replenishment rule"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params or 'creating_rule' not in context.user_data:
            await query.edit_message_text("❌ Rule creation process lost", parse_mode=None)
            return
        
        try:
            threshold = int(params[0])
            rule_data = context.user_data['creating_rule']
            
            # Create the replenishment rule
            rule_id = BulkStockManager.add_replenishment_rule(
                bulk_stock_id=rule_data['bulk_stock_id'],
                sellable_product_type_name=rule_data['product_type'],
                low_stock_threshold=threshold
            )
            
            if rule_id:
                # Log admin action
                log_admin_action(
                    admin_id=user_id,
                    action="REPLENISHMENT_RULE_CREATED",
                    reason=f"Rule: {rule_data['product_type']} → threshold {threshold}"
                )
                
                # Clear creation state
                context.user_data.pop('creating_rule', None)
                
                # Get details for confirmation
                items = BulkStockManager.get_bulk_stock_items(limit=1000)
                bulk_item = next((i for i in items if i['id'] == rule_data['bulk_stock_id']), None)
                bulk_name = bulk_item['name'] if bulk_item else f"ID {rule_data['bulk_stock_id']}"
                worker_username = bulk_item['worker_username'] if bulk_item else 'Unknown'
                
                message = (
                    f"✅ *Replenishment Rule Created Successfully!*\n\n"
                    f"📦 Bulk Stock: {bulk_name}\n"
                    f"🧩 Product Type: {rule_data['product_type']}\n"
                    f"⚠️ Threshold: {threshold} items\n"
                    f"👷 Worker: @{worker_username}\n\n"
                    f"The system will now monitor stock levels and automatically "
                    f"notify the worker when replenishment is needed."
                )
                
                keyboard = [
                    [InlineKeyboardButton("📋 View All Rules", callback_data="admin_replenishment_rules|0")],
                    [InlineKeyboardButton("➕ Create Another Rule", callback_data="admin_add_replenishment_rule")],
                    [InlineKeyboardButton("⬅️ Back to Bulk Stock Menu", callback_data="admin_bulk_stock_menu")]
                ]
                
                await query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text(
                    "❌ Failed to create replenishment rule. Rule might already exist for this combination.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_bulk_stock_menu")]])
                )
            
        except Exception as e:
            logger.error(f"Error setting threshold for rule: {e}")
            await query.edit_message_text("❌ Error creating rule", parse_mode=None)
    
    @staticmethod
    async def handle_update_bulk_quantity_start(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
        """Start the process of updating bulk stock quantity"""
        query = update.callback_query
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            await query.edit_message_text("❌ Access denied. Admin only.", parse_mode=None)
            return
        
        if not params or len(params) < 2:
            await query.edit_message_text("❌ Invalid item reference", parse_mode=None)
            return
        
        try:
            item_id = int(params[0])
            return_offset = int(params[1]) if params[1].isdigit() else 0
            
            # Store update context
            context.user_data['updating_bulk_quantity'] = {
                'item_id': item_id,
                'return_offset': return_offset
            }
            
            # Get current item details
            items = BulkStockManager.get_bulk_stock_items(limit=1000)
            item = next((i for i in items if i['id'] == item_id), None)
            
            if not item:
                await query.edit_message_text("❌ Bulk stock item not found", parse_mode=None)
                return
            
            message = (
                f"✏️ *Update Quantity*\n\n"
                f"📦 Item: {item['name']}\n"
                f"📊 Current Quantity: {item['current_quantity']} {item['unit']}\n\n"
                f"Enter the new quantity value:"
            )
            
            keyboard = [
                [InlineKeyboardButton("❌ Cancel", callback_data=f"admin_view_bulk_item|{item_id}|{return_offset}")]
            ]
            
            await query.edit_message_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            logger.error(f"Error starting bulk quantity update: {e}")
            await query.edit_message_text("❌ Error starting quantity update", parse_mode=None)
    
    @staticmethod
    async def handle_bulk_quantity_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for bulk stock quantity update"""
        user_id = update.effective_user.id
        
        if user_id not in [ADMIN_ID] + SECONDARY_ADMIN_IDS:
            return
        
        if 'updating_bulk_quantity' not in context.user_data:
            return
        
        try:
            text = update.message.text.strip()
            update_data = context.user_data['updating_bulk_quantity']
            
            # Validate quantity
            try:
                new_quantity = float(text)
                if new_quantity < 0:
                    await update.message.reply_text("❌ Quantity cannot be negative. Please try again.")
                    return
            except ValueError:
                await update.message.reply_text("❌ Invalid quantity format. Please enter a number (e.g., 100, 50.5).")
                return
            
            # Update the quantity
            success = BulkStockManager.update_bulk_stock_quantity(
                bulk_stock_id=update_data['item_id'],
                new_quantity=new_quantity
            )
            
            if success:
                # Log admin action
                log_admin_action(
                    admin_id=user_id,
                    action="BULK_STOCK_QUANTITY_UPDATED",
                    reason=f"Updated quantity to {new_quantity}"
                )
                
                # Clear update state
                context.user_data.pop('updating_bulk_quantity', None)
                
                message = f"✅ Quantity updated to {new_quantity} successfully!"
                keyboard = [
                    [InlineKeyboardButton("📦 View Item", callback_data=f"admin_view_bulk_item|{update_data['item_id']}|{update_data['return_offset']}")]
                ]
                
                await update.message.reply_text(
                    message,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.message.reply_text("❌ Failed to update quantity. Please try again.")
            
        except Exception as e:
            logger.error(f"Error handling bulk quantity update: {e}")
            await update.message.reply_text("❌ Error processing quantity update")
    
    @staticmethod
    async def _get_worker_username(worker_id: int) -> str:
        """Get username for a worker ID"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT username FROM users WHERE user_id = ?", (worker_id,))
            result = cursor.fetchone()
            
            return result['username'] if result and result['username'] else f"ID{worker_id}"
            
        except Exception as e:
            logger.error(f"Error fetching worker username: {e}")
            return f"ID{worker_id}"
        finally:
            if 'conn' in locals():
                conn.close()


# Additional handler exports for integration
COMPLETE_BULK_STOCK_HANDLERS = {
    "admin_assign_worker": CompleteBulkStockHandlers.handle_assign_worker,
    "admin_bulk_unit": CompleteBulkStockHandlers.handle_bulk_unit_selection,
    "admin_add_replenishment_rule": CompleteBulkStockHandlers.handle_add_replenishment_rule_start,
    "admin_rule_select_bulk": CompleteBulkStockHandlers.handle_rule_select_bulk_stock,
    "admin_rule_select_type": CompleteBulkStockHandlers.handle_rule_select_product_type,
    "admin_rule_set_threshold": CompleteBulkStockHandlers.handle_rule_set_threshold,
    "admin_update_bulk_quantity": CompleteBulkStockHandlers.handle_update_bulk_quantity_start,
}

# Message handler for quantity updates
async def handle_bulk_stock_message_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all text message inputs for bulk stock management"""
    await CompleteBulkStockHandlers.handle_bulk_quantity_message(update, context) 