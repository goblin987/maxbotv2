# Advanced Bulk Stock & Worker Replenishment Notification Feature

## Overview

This feature provides an automated system for managing bulk stock items and notifying workers when related sellable products run low. It's designed to streamline the restocking process by automatically alerting assigned workers when inventory thresholds are breached.

## Core Components

### 1. Database Schema

The feature adds four new database tables:

#### `bulk_stock_items`
- Stores information about bulk stock items (raw materials)
- Fields: id, name, current_quantity, unit, pickup_instructions, assigned_worker_id, is_active, is_processed, timestamps

#### `bulk_stock_media`
- Stores media files (photos/videos) for pickup instructions
- Fields: id, bulk_stock_id, media_type, telegram_file_id, file_path, created_at

#### `replenishment_rules`
- Defines relationships between bulk stock and sellable product types
- Fields: id, bulk_stock_id, sellable_product_type_name, low_stock_threshold, is_active, timestamps

#### `bulk_stock_notifications`
- Logs notification history to prevent spam
- Fields: id, replenishment_rule_id, worker_id, current_stock_level, threshold, notification_sent_at

### 2. Core Classes

#### `BulkStockManager` (`bulk_stock_management.py`)
Main class containing all database operations and business logic:
- `init_bulk_stock_tables()` - Initialize database tables
- `add_bulk_stock_item()` - Create new bulk stock items
- `get_bulk_stock_items()` - Retrieve bulk stock items with pagination
- `add_replenishment_rule()` - Create monitoring rules
- `check_stock_levels_and_notify()` - Background monitoring job
- Various helper methods for stock calculations and notifications

#### `AdminBulkStockHandlers` (`admin_bulk_stock.py`)
Telegram bot handlers for admin interface:
- Main menu navigation
- Bulk stock item listing and viewing
- Creation workflow initiation

#### `CompleteBulkStockHandlers` (`admin_bulk_stock_complete.py`)
Additional handlers for complex workflows:
- Worker assignment
- Replenishment rule creation
- Quantity updates
- Unit selection

## Feature Workflow

### 1. Admin: Add Bulk Stock Item

**Path**: Admin Menu → Bulk Stock Management → Add New Bulk Stock Item

**Steps**:
1. Enter item name (e.g., "Raw Bananas - 200kg Crate")
2. Enter initial quantity (e.g., 200)
3. Select unit (kg, L, pieces, boxes, or custom)
4. Enter pickup instructions (location, access codes, contact info)
5. Assign to a worker

**Admin Interface Navigation**:
```
/admin → Bulk Stock Management → Add New Bulk Stock Item
```

### 2. Admin: Define Replenishment Rule

**Path**: Admin Menu → Bulk Stock Management → Manage Replenishment Rules → Add New Rule

**Steps**:
1. Select bulk stock item to monitor
2. Choose sellable product type to track
3. Set low stock threshold

**Example Rule**:
- Bulk Stock: "Raw Bananas - 200kg Crate"
- Product Type: "Banana"
- Threshold: 10 items

### 3. System: Automated Monitoring

The system runs a background job every 30 minutes that:

1. **Checks Active Rules**: Queries all active replenishment rules
2. **Calculates Stock**: Sums available stock for each product type: `SUM(available - reserved)`
3. **Compares Thresholds**: Checks if stock ≤ threshold
4. **Prevents Spam**: Ensures no notification sent in last 2 hours
5. **Sends Notifications**: Delivers pickup instructions to assigned workers

**Background Job Configuration**:
```python
# Runs every 30 minutes, starts 60 seconds after app launch
job_queue.run_repeating(bulk_stock_monitoring_job_wrapper, interval=1800, first=60)
```

### 4. Worker Notification Format

When stock falls below threshold, workers receive:

```
🚨 LOW STOCK ALERT 🚨

📦 Bulk Stock: Raw Bananas - 200kg Crate
📊 Current Stock: 8
⚠️ Threshold: 10

📋 Pickup Instructions:
Location: Warehouse A, Section 3
Access Code: 1234
Contact: John Doe (+1234567890)
Hours: 8 AM - 6 PM

Please process this bulk stock item as soon as possible.
```

Plus any attached media (photos/videos of location, access instructions, etc.)

## Admin Interface Structure

### Main Menu
```
📦 Bulk Stock Management
├── 📦 Manage Bulk Stock Items
├── 📋 Manage Replenishment Rules  
├── 👷 View Worker Notifications
└── ➕ Add New Bulk Stock Item
```

### Bulk Stock Item Management
```
📦 Bulk Stock Items
├── ✅📦 Raw Bananas (200kg) → View Details
├── ✅🔄 Raw Apples (150kg) → View Details
└── ❌📦 Old Stock (0kg) → View Details

Item Details:
├── ✏️ Update Quantity
├── 🔄 Mark as Processed  
├── 📋 View Rules
└── ❌ Deactivate
```

### Replenishment Rules Management
```
📋 Replenishment Rules
├── 📦 Raw Bananas → Banana (≤10)
├── 📦 Raw Apples → Apple (≤15)
└── 📦 Raw Oranges → Orange (≤8)

Rule Details:
├── ✏️ Edit Threshold
├── 🔄 Toggle Active/Inactive
└── 🗑️ Delete Rule
```

## Integration Points

### 1. Main Application Integration

**File**: `main.py`
- Import bulk stock modules
- Register callback handlers in `KNOWN_HANDLERS`
- Add message handlers for text input
- Schedule background monitoring job

### 2. Admin Menu Integration

**File**: `admin_product_management.py`
- Add "Bulk Stock Management" button to admin menu
- Button callback: `admin_bulk_stock_menu`

### 3. Database Integration

**File**: `utils.py`
- Tables automatically created on module import
- Uses existing `get_db_connection()` function
- Follows existing database patterns

## Configuration Options

### Constants (Configurable)
```python
NOTIFICATION_COOLDOWN_HOURS = 2  # Prevent notification spam
BULK_STOCK_ITEMS_PER_PAGE = 10   # Pagination limit
REPLENISHMENT_RULES_PER_PAGE = 8 # Pagination limit
```

### Background Job Timing
```python
# Monitor every 30 minutes (1800 seconds)
# Start 60 seconds after application launch
interval=1800, first=60
```

## Error Handling & Logging

### Database Errors
- All database operations wrapped in try/catch
- Transactions rolled back on error
- Detailed error logging with context

### Notification Failures
- Failed notifications logged as errors
- System continues monitoring other rules
- Admin alerted of critical failures

### Worker Assignment
- Validates worker exists before assignment
- Handles unassigned workers gracefully
- Fallback to admin notification if worker unreachable

## Security & Permissions

### Admin Only Access
- All bulk stock operations require admin privileges
- Checked against `ADMIN_ID` and `SECONDARY_ADMIN_IDS`
- Unauthorized access attempts logged

### Input Validation
- Quantity values validated (positive numbers)
- Text inputs sanitized and length-limited
- SQL injection prevention through parameterized queries

## Performance Considerations

### Background Job Optimization
- Efficient SQL queries with proper indexing
- Cooldown period prevents excessive notifications
- Batch processing of multiple rules

### Pagination
- Large lists paginated to prevent memory issues
- Configurable page sizes for different views
- Efficient database queries with LIMIT/OFFSET

### Media Handling
- Media files stored locally when possible
- Telegram file IDs cached for reuse
- Failed media downloads don't block notifications

## Troubleshooting

### Common Issues

1. **Notifications Not Sending**
   - Check worker assignment (not null)
   - Verify rule is active
   - Check cooldown period hasn't expired
   - Ensure Telegram app is running

2. **Background Job Not Running**
   - Check job queue initialization
   - Verify `telegram_app` is properly set
   - Look for job scheduling errors in logs

3. **Database Issues**
   - Check database path and permissions
   - Verify table creation during import
   - Look for SQL errors in logs

### Log Monitoring

Key log messages to monitor:
```
INFO: Bulk stock tables initialized successfully
INFO: Scheduled bulk stock monitoring job to run every 30 minutes
INFO: Sent low stock notification for [type] to worker [username]
ERROR: Error in background job bulk_stock_monitoring_job
```

## Future Enhancements

### Potential Improvements

1. **Media Management**
   - Bulk media upload interface
   - Media preview in admin interface
   - Media compression and optimization

2. **Advanced Rules**
   - Multiple threshold levels (warning, critical)
   - Time-based rules (weekday vs weekend)
   - Location-specific rules

3. **Reporting & Analytics**
   - Notification history dashboard
   - Stock level trends
   - Worker response time metrics

4. **Mobile Integration**
   - Push notifications for workers
   - Mobile-optimized interfaces
   - Location-based notifications

## File Structure

```
├── bulk_stock_management.py          # Core business logic & database operations
├── admin_bulk_stock.py               # Basic admin interface handlers
├── admin_bulk_stock_complete.py      # Advanced admin interface handlers
├── main.py                           # Integration & background job scheduling
├── admin_product_management.py       # Menu integration
└── BULK_STOCK_README.md             # This documentation
```

## Database Migration

The feature automatically creates required tables on import. For existing installations:

1. Tables are created with `CREATE TABLE IF NOT EXISTS`
2. No data migration required
3. Existing functionality unaffected
4. Feature can be safely enabled/disabled

## Testing Scenarios

### Manual Testing Checklist

1. **Bulk Stock Creation**
   - [ ] Create item with all fields
   - [ ] Handle duplicate names
   - [ ] Validate quantity inputs
   - [ ] Test worker assignment

2. **Replenishment Rules**
   - [ ] Create rule for existing product type
   - [ ] Test threshold validation
   - [ ] Handle duplicate rules

3. **Background Monitoring**
   - [ ] Create low stock scenario
   - [ ] Verify notification sent
   - [ ] Test cooldown period
   - [ ] Check notification content

4. **Admin Interface**
   - [ ] Navigate all menu levels
   - [ ] Test pagination
   - [ ] Verify permissions
   - [ ] Test error handling

This comprehensive feature provides a robust solution for automated stock management while maintaining the modular, well-documented structure of your existing codebase. 