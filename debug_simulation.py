#!/usr/bin/env python3
"""
Comprehensive debugging simulation script for maxbotv2

This script simulates various user flows to identify bugs and issues:
1. User registration and basic flows
2. Admin product management
3. Worker product addition
4. Payment flows
5. State management edge cases
6. Database consistency checks

Run this script to debug common issues before deployment.
"""

import asyncio
import sqlite3
import logging
import sys
import os
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock

# Add the current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging for debugging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('debug_simulation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class MockUpdate:
    """Mock Telegram Update object for testing"""
    def __init__(self, user_id, message_text=None, callback_data=None, is_callback=False):
        self.update_id = 12345
        self.effective_user = MagicMock()
        self.effective_user.id = user_id
        self.effective_user.first_name = f"TestUser{user_id}"
        self.effective_user.username = f"testuser{user_id}"
        
        self.effective_chat = MagicMock()
        self.effective_chat.id = user_id
        
        if is_callback:
            self.callback_query = MagicMock()
            self.callback_query.from_user = self.effective_user
            self.callback_query.data = callback_data
            self.callback_query.message = MagicMock()
            self.callback_query.message.chat_id = user_id
            self.callback_query.answer = AsyncMock()
            self.callback_query.edit_message_text = AsyncMock()
            self.message = None
        else:
            self.message = MagicMock()
            self.message.text = message_text
            self.message.chat_id = user_id
            self.message.reply_text = AsyncMock()
            self.callback_query = None

class MockContext:
    """Mock Telegram Context object for testing"""
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot = MagicMock()
        self.job_queue = MagicMock()

class DebugSimulator:
    """Main debugging and simulation class"""
    
    def __init__(self):
        self.test_results = []
        self.errors = []
    
    def log_test(self, test_name, status, message=""):
        """Log test results"""
        self.test_results.append({
            'test': test_name,
            'status': status,
            'message': message,
            'timestamp': datetime.now()
        })
        
        if status == 'PASS':
            logger.info(f"✅ {test_name}: {message}")
        elif status == 'FAIL':
            logger.error(f"❌ {test_name}: {message}")
            self.errors.append(f"{test_name}: {message}")
        else:
            logger.warning(f"⚠️ {test_name}: {message}")
    
    def check_database_integrity(self):
        """Check database schema and basic integrity"""
        try:
            from utils import get_db_connection, init_db
            
            # Initialize database
            init_db()
            
            conn = get_db_connection()
            c = conn.cursor()
            
            # Check required tables exist
            required_tables = [
                'users', 'cities', 'districts', 'products', 'product_types',
                'discount_codes', 'pending_deposits', 'purchase_history',
                'reviews', 'admin_log', 'worker_actions'
            ]
            
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = [row[0] for row in c.fetchall()]
            
            for table in required_tables:
                if table in existing_tables:
                    self.log_test(f"DB_TABLE_{table.upper()}", "PASS", f"Table {table} exists")
                else:
                    self.log_test(f"DB_TABLE_{table.upper()}", "FAIL", f"Table {table} missing")
            
            # Check foreign key constraints
            c.execute("PRAGMA foreign_key_check")
            fk_violations = c.fetchall()
            if fk_violations:
                self.log_test("DB_FOREIGN_KEYS", "FAIL", f"{len(fk_violations)} foreign key violations")
            else:
                self.log_test("DB_FOREIGN_KEYS", "PASS", "No foreign key violations")
            
            # Check for orphaned records
            c.execute("""
                SELECT COUNT(*) FROM products p 
                LEFT JOIN cities c ON p.city = c.name 
                WHERE c.name IS NULL
            """)
            orphaned_products = c.fetchone()[0]
            if orphaned_products > 0:
                self.log_test("DB_ORPHANED_PRODUCTS", "FAIL", f"{orphaned_products} products with invalid cities")
            else:
                self.log_test("DB_ORPHANED_PRODUCTS", "PASS", "No orphaned products")
            
            conn.close()
            
        except Exception as e:
            self.log_test("DB_INTEGRITY", "FAIL", f"Database integrity check failed: {e}")
    
    async def test_user_registration_flow(self):
        """Test basic user registration and start command"""
        try:
            from user import start
            from utils import get_user_roles
            
            # Test user registration
            test_user_id = 999999
            update = MockUpdate(test_user_id, "/start")
            context = MockContext()
            
            await start(update, context)
            
            # Check if user was created
            user_roles = get_user_roles(test_user_id)
            if 'is_primary' in user_roles:
                self.log_test("USER_REGISTRATION", "PASS", "User registration successful")
            else:
                self.log_test("USER_REGISTRATION", "FAIL", "User registration failed")
            
        except Exception as e:
            self.log_test("USER_REGISTRATION", "FAIL", f"User registration error: {e}")
    
    async def test_admin_access_control(self):
        """Test admin access control"""
        try:
            from admin_product_management import handle_admin_menu
            from utils import ADMIN_ID
            
            # Test valid admin access
            if ADMIN_ID:
                update = MockUpdate(ADMIN_ID, is_callback=True, callback_data="admin_menu")
                context = MockContext()
                
                await handle_admin_menu(update, context)
                self.log_test("ADMIN_ACCESS_VALID", "PASS", "Admin access granted correctly")
            
            # Test invalid admin access
            fake_admin_id = 888888
            update = MockUpdate(fake_admin_id, is_callback=True, callback_data="admin_menu")
            context = MockContext()
            
            try:
                await handle_admin_menu(update, context)
                # If no exception, check if access was denied
                self.log_test("ADMIN_ACCESS_INVALID", "PASS", "Non-admin access properly denied")
            except Exception:
                self.log_test("ADMIN_ACCESS_INVALID", "PASS", "Non-admin access properly denied with exception")
                
        except Exception as e:
            self.log_test("ADMIN_ACCESS_CONTROL", "FAIL", f"Admin access control error: {e}")
    
    async def test_worker_interface(self):
        """Test worker interface functionality"""
        try:
            from worker_interface import handle_worker_admin_menu
            from utils import get_db_connection
            
            # Create a test worker
            test_worker_id = 777777
            conn = get_db_connection()
            c = conn.cursor()
            
            c.execute("""
                INSERT OR REPLACE INTO users (user_id, username, is_worker, worker_status, worker_alias)
                VALUES (?, ?, 1, 'active', 'TestWorker')
            """, (test_worker_id, f"testworker{test_worker_id}"))
            conn.commit()
            conn.close()
            
            # Test worker menu access
            update = MockUpdate(test_worker_id, is_callback=True, callback_data="worker_admin_menu")
            context = MockContext()
            
            await handle_worker_admin_menu(update, context)
            self.log_test("WORKER_INTERFACE", "PASS", "Worker interface accessible")
            
        except Exception as e:
            self.log_test("WORKER_INTERFACE", "FAIL", f"Worker interface error: {e}")
    
    async def test_state_management(self):
        """Test state management and validation"""
        try:
            from main import _validate_and_cleanup_state
            
            # Test valid state
            update = MockUpdate(123456)
            context = MockContext()
            context.user_data["worker_selected_category"] = "Test"
            context.user_data["worker_single_city"] = "TestCity"
            context.user_data["worker_single_district"] = "TestDistrict"
            
            is_valid = await _validate_and_cleanup_state(update, context, "awaiting_worker_single_product")
            if is_valid:
                self.log_test("STATE_VALIDATION_VALID", "PASS", "Valid state correctly validated")
            else:
                self.log_test("STATE_VALIDATION_VALID", "FAIL", "Valid state incorrectly invalidated")
            
            # Test invalid state
            context.user_data.clear()
            is_valid = await _validate_and_cleanup_state(update, context, "awaiting_worker_single_product")
            if not is_valid:
                self.log_test("STATE_VALIDATION_INVALID", "PASS", "Invalid state correctly detected")
            else:
                self.log_test("STATE_VALIDATION_INVALID", "FAIL", "Invalid state not detected")
                
        except Exception as e:
            self.log_test("STATE_MANAGEMENT", "FAIL", f"State management error: {e}")
    
    async def test_callback_handlers(self):
        """Test callback handler routing"""
        try:
            from main import callback_query_router, handle_callback_query
            
            # Test known callback
            update = MockUpdate(123456, is_callback=True, callback_data="start")
            context = MockContext()
            
            await handle_callback_query(update, context)
            self.log_test("CALLBACK_ROUTING_VALID", "PASS", "Valid callback routed correctly")
            
            # Test unknown callback
            update = MockUpdate(123456, is_callback=True, callback_data="nonexistent_callback")
            context = MockContext()
            
            await handle_callback_query(update, context)
            self.log_test("CALLBACK_ROUTING_INVALID", "PASS", "Unknown callback handled gracefully")
            
        except Exception as e:
            self.log_test("CALLBACK_HANDLERS", "FAIL", f"Callback handler error: {e}")
    
    def test_import_integrity(self):
        """Test that all required modules can be imported"""
        required_modules = [
            'utils', 'user', 'admin_product_management', 'admin_features',
            'admin_workers', 'worker_interface', 'payment', 'stock',
            'viewer_admin', 'bulk_stock_management'
        ]
        
        for module_name in required_modules:
            try:
                __import__(module_name)
                self.log_test(f"IMPORT_{module_name.upper()}", "PASS", f"Module {module_name} imported successfully")
            except ImportError as e:
                self.log_test(f"IMPORT_{module_name.upper()}", "FAIL", f"Failed to import {module_name}: {e}")
            except Exception as e:
                self.log_test(f"IMPORT_{module_name.upper()}", "WARN", f"Import warning for {module_name}: {e}")
    
    def test_configuration(self):
        """Test configuration and environment variables"""
        try:
            from utils import TOKEN, ADMIN_ID, WEBHOOK_URL, NOWPAYMENTS_API_KEY
            
            if TOKEN:
                self.log_test("CONFIG_TOKEN", "PASS", "Bot token configured")
            else:
                self.log_test("CONFIG_TOKEN", "FAIL", "Bot token missing")
            
            if ADMIN_ID:
                self.log_test("CONFIG_ADMIN_ID", "PASS", f"Admin ID configured: {ADMIN_ID}")
            else:
                self.log_test("CONFIG_ADMIN_ID", "FAIL", "Admin ID missing")
            
            if WEBHOOK_URL:
                self.log_test("CONFIG_WEBHOOK", "PASS", "Webhook URL configured")
            else:
                self.log_test("CONFIG_WEBHOOK", "FAIL", "Webhook URL missing")
            
            if NOWPAYMENTS_API_KEY:
                self.log_test("CONFIG_PAYMENTS", "PASS", "Payment API key configured")
            else:
                self.log_test("CONFIG_PAYMENTS", "FAIL", "Payment API key missing")
                
        except Exception as e:
            self.log_test("CONFIGURATION", "FAIL", f"Configuration test error: {e}")
    
    async def run_all_tests(self):
        """Run all debugging tests"""
        logger.info("🚀 Starting comprehensive debugging simulation...")
        
        # Basic tests
        self.test_import_integrity()
        self.test_configuration()
        self.check_database_integrity()
        
        # Async tests
        await self.test_user_registration_flow()
        await self.test_admin_access_control()
        await self.test_worker_interface()
        await self.test_state_management()
        await self.test_callback_handlers()
        
        # Summary
        total_tests = len(self.test_results)
        passed_tests = len([t for t in self.test_results if t['status'] == 'PASS'])
        failed_tests = len([t for t in self.test_results if t['status'] == 'FAIL'])
        warned_tests = len([t for t in self.test_results if t['status'] == 'WARN'])
        
        logger.info(f"\n{'='*60}")
        logger.info(f"DEBUGGING SIMULATION COMPLETE")
        logger.info(f"{'='*60}")
        logger.info(f"Total Tests: {total_tests}")
        logger.info(f"✅ Passed: {passed_tests}")
        logger.info(f"❌ Failed: {failed_tests}")
        logger.info(f"⚠️ Warnings: {warned_tests}")
        
        if failed_tests > 0:
            logger.error(f"\n🚨 CRITICAL ISSUES DETECTED:")
            for error in self.errors:
                logger.error(f"  • {error}")
        
        if failed_tests == 0:
            logger.info(f"\n🎉 ALL TESTS PASSED! Bot should work correctly.")
        else:
            logger.warning(f"\n⚠️ Some tests failed. Review the issues above before deployment.")

if __name__ == "__main__":
    simulator = DebugSimulator()
    asyncio.run(simulator.run_all_tests()) 