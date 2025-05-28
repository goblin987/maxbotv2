"""
Advanced Bulk Stock & Worker Replenishment Notification System

This module manages bulk stock items and automatically notifies workers when 
related sellable products run low. It provides admin interfaces for managing
bulk stock and replenishment rules, plus automated monitoring and notifications.

Key Components:
1. Database operations for bulk stock and replenishment rules
2. Admin interfaces for managing bulk stock items and rules
3. Background monitoring system for stock levels
4. Worker notification system with pickup instructions
"""

import sqlite3
import logging
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from decimal import Decimal

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

# Local imports
from utils import (
    get_db_connection, send_message_with_retry, log_admin_action,
    LANGUAGES, MEDIA_DIR, BOT_MEDIA_JSON_PATH
)

# Configure logging
logger = logging.getLogger(__name__)

# Constants
NOTIFICATION_COOLDOWN_HOURS = 2  # Cooldown period between notifications
BULK_STOCK_ITEMS_PER_PAGE = 10
REPLENISHMENT_RULES_PER_PAGE = 8


class BulkStockManager:
    """Main class for managing bulk stock operations"""
    
    @staticmethod
    def init_bulk_stock_tables():
        """Initialize database tables for bulk stock management"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Create bulk_stock_items table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bulk_stock_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    current_quantity REAL NOT NULL DEFAULT 0,
                    unit TEXT NOT NULL,
                    pickup_instructions TEXT NOT NULL,
                    assigned_worker_id INTEGER,
                    is_active BOOLEAN DEFAULT 1,
                    is_processed BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(assigned_worker_id) REFERENCES users(user_id) ON DELETE SET NULL
                )
            """)
            
            # Create bulk_stock_media table for pickup instruction media
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bulk_stock_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bulk_stock_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL CHECK(media_type IN ('photo', 'video', 'animation')),
                    telegram_file_id TEXT NOT NULL,
                    file_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(bulk_stock_id) REFERENCES bulk_stock_items(id) ON DELETE CASCADE
                )
            """)
            
            # Create replenishment_rules table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS replenishment_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bulk_stock_id INTEGER NOT NULL,
                    sellable_product_type_name TEXT NOT NULL,
                    low_stock_threshold INTEGER NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(bulk_stock_id) REFERENCES bulk_stock_items(id) ON DELETE CASCADE,
                    UNIQUE(bulk_stock_id, sellable_product_type_name)
                )
            """)
            
            # Create notification_logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bulk_stock_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    replenishment_rule_id INTEGER NOT NULL,
                    worker_id INTEGER NOT NULL,
                    current_stock_level INTEGER NOT NULL,
                    threshold INTEGER NOT NULL,
                    notification_sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(replenishment_rule_id) REFERENCES replenishment_rules(id) ON DELETE CASCADE,
                    FOREIGN KEY(worker_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            """)
            
            conn.commit()
            logger.info("Bulk stock tables initialized successfully")
            
        except sqlite3.Error as e:
            logger.error(f"Error initializing bulk stock tables: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def add_bulk_stock_item(name: str, initial_quantity: float, unit: str, 
                           pickup_instructions: str, assigned_worker_id: int) -> bool:
        """Add a new bulk stock item"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO bulk_stock_items 
                (name, current_quantity, unit, pickup_instructions, assigned_worker_id)
                VALUES (?, ?, ?, ?, ?)
            """, (name, initial_quantity, unit, pickup_instructions, assigned_worker_id))
            
            bulk_stock_id = cursor.lastrowid
            conn.commit()
            
            logger.info(f"Added bulk stock item: {name} (ID: {bulk_stock_id})")
            return bulk_stock_id
            
        except sqlite3.IntegrityError:
            logger.warning(f"Bulk stock item with name '{name}' already exists")
            return False
        except sqlite3.Error as e:
            logger.error(f"Error adding bulk stock item: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def add_bulk_stock_media(bulk_stock_id: int, media_type: str, 
                            telegram_file_id: str, file_path: str = None) -> bool:
        """Add media for bulk stock pickup instructions"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO bulk_stock_media 
                (bulk_stock_id, media_type, telegram_file_id, file_path)
                VALUES (?, ?, ?, ?)
            """, (bulk_stock_id, media_type, telegram_file_id, file_path))
            
            conn.commit()
            logger.info(f"Added media for bulk stock item {bulk_stock_id}: {media_type}")
            return True
            
        except sqlite3.Error as e:
            logger.error(f"Error adding bulk stock media: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_bulk_stock_items(offset: int = 0, limit: int = BULK_STOCK_ITEMS_PER_PAGE, 
                            include_inactive: bool = False) -> List[Dict]:
        """Get paginated list of bulk stock items"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            where_clause = "" if include_inactive else "WHERE bsi.is_active = 1"
            
            cursor.execute(f"""
                SELECT 
                    bsi.id, bsi.name, bsi.current_quantity, bsi.unit,
                    bsi.pickup_instructions, bsi.assigned_worker_id, bsi.is_active,
                    bsi.is_processed, bsi.created_at, bsi.updated_at,
                    u.username as worker_username
                FROM bulk_stock_items bsi
                LEFT JOIN users u ON bsi.assigned_worker_id = u.user_id
                {where_clause}
                ORDER BY bsi.updated_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            
            items = []
            for row in cursor.fetchall():
                items.append({
                    'id': row[0],
                    'name': row[1],
                    'current_quantity': row[2],
                    'unit': row[3],
                    'pickup_instructions': row[4],
                    'assigned_worker_id': row[5],
                    'is_active': row[6],
                    'is_processed': row[7],
                    'created_at': row[8],
                    'updated_at': row[9],
                    'worker_username': row[10] or 'Unassigned'
                })
            
            return items
            
        except sqlite3.Error as e:
            logger.error(f"Error fetching bulk stock items: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_bulk_stock_item_count(include_inactive: bool = False) -> int:
        """Get total count of bulk stock items"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            where_clause = "" if include_inactive else "WHERE is_active = 1"
            cursor.execute(f"SELECT COUNT(*) FROM bulk_stock_items {where_clause}")
            
            return cursor.fetchone()[0]
            
        except sqlite3.Error as e:
            logger.error(f"Error counting bulk stock items: {e}")
            return 0
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def update_bulk_stock_quantity(bulk_stock_id: int, new_quantity: float, 
                                  mark_processed: bool = False) -> bool:
        """Update bulk stock quantity and processing status"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            update_fields = ["current_quantity = ?", "updated_at = CURRENT_TIMESTAMP"]
            params = [new_quantity]
            
            if mark_processed:
                update_fields.append("is_processed = 1")
            
            cursor.execute(f"""
                UPDATE bulk_stock_items 
                SET {', '.join(update_fields)}
                WHERE id = ?
            """, params + [bulk_stock_id])
            
            if cursor.rowcount > 0:
                conn.commit()
                logger.info(f"Updated bulk stock item {bulk_stock_id}: quantity={new_quantity}, processed={mark_processed}")
                return True
            else:
                logger.warning(f"Bulk stock item {bulk_stock_id} not found for update")
                return False
                
        except sqlite3.Error as e:
            logger.error(f"Error updating bulk stock item: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def add_replenishment_rule(bulk_stock_id: int, sellable_product_type_name: str, 
                              low_stock_threshold: int) -> bool:
        """Add a replenishment rule linking bulk stock to sellable product type"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO replenishment_rules 
                (bulk_stock_id, sellable_product_type_name, low_stock_threshold)
                VALUES (?, ?, ?)
            """, (bulk_stock_id, sellable_product_type_name, low_stock_threshold))
            
            rule_id = cursor.lastrowid
            conn.commit()
            
            logger.info(f"Added replenishment rule (ID: {rule_id}) for bulk stock {bulk_stock_id}")
            return rule_id
            
        except sqlite3.IntegrityError:
            logger.warning(f"Replenishment rule already exists for bulk stock {bulk_stock_id} and product type {sellable_product_type_name}")
            return False
        except sqlite3.Error as e:
            logger.error(f"Error adding replenishment rule: {e}")
            if conn:
                conn.rollback()
            return False
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def get_replenishment_rules(offset: int = 0, limit: int = REPLENISHMENT_RULES_PER_PAGE) -> List[Dict]:
        """Get paginated list of replenishment rules"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    rr.id, rr.bulk_stock_id, rr.sellable_product_type_name,
                    rr.low_stock_threshold, rr.is_active, rr.created_at,
                    bsi.name as bulk_stock_name, bsi.assigned_worker_id,
                    u.username as worker_username
                FROM replenishment_rules rr
                JOIN bulk_stock_items bsi ON rr.bulk_stock_id = bsi.id
                LEFT JOIN users u ON bsi.assigned_worker_id = u.user_id
                WHERE rr.is_active = 1 AND bsi.is_active = 1
                ORDER BY rr.created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            
            rules = []
            for row in cursor.fetchall():
                rules.append({
                    'id': row[0],
                    'bulk_stock_id': row[1],
                    'sellable_product_type_name': row[2],
                    'low_stock_threshold': row[3],
                    'is_active': row[4],
                    'created_at': row[5],
                    'bulk_stock_name': row[6],
                    'assigned_worker_id': row[7],
                    'worker_username': row[8] or 'Unassigned'
                })
            
            return rules
            
        except sqlite3.Error as e:
            logger.error(f"Error fetching replenishment rules: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def check_stock_levels_and_notify() -> int:
        """
        Background job: Check stock levels for all active rules and send notifications
        Returns: Number of notifications sent
        """
        notifications_sent = 0
        
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Get all active replenishment rules
            cursor.execute("""
                SELECT 
                    rr.id, rr.bulk_stock_id, rr.sellable_product_type_name,
                    rr.low_stock_threshold, bsi.name as bulk_stock_name,
                    bsi.pickup_instructions, bsi.assigned_worker_id,
                    u.username as worker_username
                FROM replenishment_rules rr
                JOIN bulk_stock_items bsi ON rr.bulk_stock_id = bsi.id
                LEFT JOIN users u ON bsi.assigned_worker_id = u.user_id
                WHERE rr.is_active = 1 AND bsi.is_active = 1 AND bsi.assigned_worker_id IS NOT NULL
            """)
            
            rules = cursor.fetchall()
            
            for rule in rules:
                rule_id, bulk_stock_id, product_type, threshold, bulk_stock_name, pickup_instructions, worker_id, worker_username = rule
                
                # Check if we've notified recently for this rule
                if BulkStockManager._was_recently_notified(rule_id):
                    continue
                
                # Calculate current stock level for this product type
                current_stock = BulkStockManager._get_current_sellable_stock(product_type)
                
                # Check if stock is below threshold
                if current_stock <= threshold:
                    # Send notification to worker
                    if BulkStockManager._send_worker_notification(
                        worker_id, bulk_stock_id, bulk_stock_name, pickup_instructions, current_stock, threshold
                    ):
                        # Log the notification
                        BulkStockManager._log_notification(rule_id, worker_id, current_stock, threshold)
                        notifications_sent += 1
                        
                        logger.info(f"Sent low stock notification for {product_type} (stock: {current_stock}, threshold: {threshold}) to worker {worker_username}")
            
            return notifications_sent
            
        except sqlite3.Error as e:
            logger.error(f"Error in stock level check: {e}")
            return 0
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def _was_recently_notified(rule_id: int) -> bool:
        """Check if a notification was sent recently for this rule"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cutoff_time = datetime.now() - timedelta(hours=NOTIFICATION_COOLDOWN_HOURS)
            
            cursor.execute("""
                SELECT COUNT(*) FROM bulk_stock_notifications
                WHERE replenishment_rule_id = ? AND notification_sent_at > ?
            """, (rule_id, cutoff_time.isoformat()))
            
            count = cursor.fetchone()[0]
            return count > 0
            
        except sqlite3.Error as e:
            logger.error(f"Error checking recent notifications: {e}")
            return True  # Assume recently notified to prevent spam
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def _get_current_sellable_stock(product_type_name: str) -> int:
        """Get current available stock for a product type"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT SUM(available - reserved) as total_stock
                FROM products
                WHERE product_type = ? AND (available - reserved) > 0
            """, (product_type_name,))
            
            result = cursor.fetchone()[0]
            return int(result) if result else 0
            
        except sqlite3.Error as e:
            logger.error(f"Error calculating sellable stock for {product_type_name}: {e}")
            return 0
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def _send_worker_notification(worker_id: int, bulk_stock_id: int, bulk_stock_name: str,
                                 pickup_instructions: str, current_stock: int, threshold: int) -> bool:
        """Send notification with pickup instructions to worker"""
        try:
            # Import here to avoid circular imports
            from main import telegram_app
            
            if not telegram_app or not telegram_app.bot:
                logger.error("Telegram app not available for sending worker notification")
                return False
            
            # Prepare notification message
            message = (
                f"🚨 *LOW STOCK ALERT* 🚨\n\n"
                f"📦 Bulk Stock: *{bulk_stock_name}*\n"
                f"📊 Current Stock: *{current_stock}*\n"
                f"⚠️ Threshold: *{threshold}*\n\n"
                f"📋 *Pickup Instructions:*\n{pickup_instructions}\n\n"
                f"Please process this bulk stock item as soon as possible."
            )
            
            # Get media for this bulk stock item
            media_files = BulkStockManager._get_bulk_stock_media(bulk_stock_id)
            
            # Send message with media if available
            if media_files:
                # Send media group first, then text
                media_group = []
                for media in media_files[:10]:  # Telegram limit
                    if media['media_type'] == 'photo':
                        media_group.append(InputMediaPhoto(media['telegram_file_id']))
                    elif media['media_type'] == 'video':
                        media_group.append(InputMediaVideo(media['telegram_file_id']))
                
                if media_group:
                    import asyncio
                    # Send media group
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(telegram_app.bot.send_media_group(worker_id, media_group))
                    loop.close()
            
            # Send text message
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                send_message_with_retry(
                    telegram_app.bot, worker_id, message, parse_mode=ParseMode.MARKDOWN
                )
            )
            loop.close()
            
            return True
            
        except Exception as e:
            logger.error(f"Error sending worker notification: {e}")
            return False
    
    @staticmethod
    def _get_bulk_stock_media(bulk_stock_id: int) -> List[Dict]:
        """Get media files for bulk stock item"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT media_type, telegram_file_id, file_path
                FROM bulk_stock_media
                WHERE bulk_stock_id = ?
                ORDER BY created_at
            """, (bulk_stock_id,))
            
            media = []
            for row in cursor.fetchall():
                media.append({
                    'media_type': row[0],
                    'telegram_file_id': row[1],
                    'file_path': row[2]
                })
            
            return media
            
        except sqlite3.Error as e:
            logger.error(f"Error fetching bulk stock media: {e}")
            return []
        finally:
            if conn:
                conn.close()
    
    @staticmethod
    def _log_notification(rule_id: int, worker_id: int, current_stock: int, threshold: int):
        """Log that a notification was sent"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO bulk_stock_notifications
                (replenishment_rule_id, worker_id, current_stock_level, threshold)
                VALUES (?, ?, ?, ?)
            """, (rule_id, worker_id, current_stock, threshold))
            
            conn.commit()
            
        except sqlite3.Error as e:
            logger.error(f"Error logging notification: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()


# Initialize tables when module is imported
BulkStockManager.init_bulk_stock_tables() 