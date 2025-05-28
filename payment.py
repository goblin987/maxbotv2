# --- START OF FILE payment.py ---

import logging
import sqlite3
import time
import os # Added import
import shutil # Added import
import asyncio
import uuid # For generating unique order IDs
import requests # For making API calls to NOWPayments
from decimal import Decimal, ROUND_UP, ROUND_DOWN # Use Decimal for precision
import json # For parsing potential error messages
from datetime import datetime, timezone # Added import
from collections import Counter, defaultdict # Added import

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram import helpers
import telegram.error as telegram_error
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation # Import InputMedia types
# -------------------------

# Import necessary items from utils and user
from utils import ( # Ensure utils imports are correct
    send_message_with_retry, format_currency, ADMIN_ID,
    LANGUAGES, load_all_data, BASKET_TIMEOUT, MIN_DEPOSIT_EUR,
    NOWPAYMENTS_API_KEY, NOWPAYMENTS_API_URL, WEBHOOK_URL,
    format_expiration_time, FEE_ADJUSTMENT,
    add_pending_deposit, remove_pending_deposit, # Make sure add_pending_deposit is imported
    get_nowpayments_min_amount,
    get_db_connection, MEDIA_DIR, PRODUCT_TYPES, DEFAULT_PRODUCT_EMOJI, # Added PRODUCT_TYPES/Emoji
    clear_expired_basket, # Added import
    _get_lang_data, # <--- *** ADDED IMPORT HERE ***
    log_admin_action # <<< IMPORT log_admin_action >>>
)
# <<< IMPORT USER MODULE >>>
import user

# --- Import Reseller Helper ---
try:
    from reseller_management import get_reseller_discount
except ImportError:
    logger_dummy_reseller_payment = logging.getLogger(__name__ + "_dummy_reseller_payment")
    logger_dummy_reseller_payment.error("Could not import get_reseller_discount from reseller_management.py. Reseller discounts will not work in payment processing.")
    # Define a dummy function that always returns zero discount
    def get_reseller_discount(user_id: int, product_type: str) -> Decimal:
        return Decimal('0.0')
# -----------------------------

# --- Import Unreserve Helper ---
# Assume _unreserve_basket_items is defined elsewhere (e.g., user.py or utils.py)
try:
    from user import _unreserve_basket_items
except ImportError:
    # Fallback: Try importing from utils
    try:
        from utils import _unreserve_basket_items
    except ImportError:
        logger_unreserve_import_error = logging.getLogger(__name__)
        logger_unreserve_import_error.error("Could not import _unreserve_basket_items helper function from user.py or utils.py! Un-reserving on failure might not work.")
        # Define a dummy function to avoid crashes, but log loudly
        def _unreserve_basket_items(basket_snapshot: list | None):
            logger_unreserve_import_error.critical("CRITICAL: _unreserve_basket_items function is missing! Cannot un-reserve items on payment failure.")
# ----------------------------------

logger = logging.getLogger(__name__)

# --- NEW: Helper to get NOWPayments Estimate ---
async def _get_nowpayments_estimate(target_eur_amount: Decimal, pay_currency_code: str) -> dict:
    """Gets the estimated crypto amount from NOWPayments API."""
    if not NOWPAYMENTS_API_KEY:
        return {'error': 'payment_api_misconfigured'}

    estimate_url = f"{NOWPAYMENTS_API_URL}/v1/estimate"
    params = {
        'amount': float(target_eur_amount),
        'currency_from': 'eur',
        'currency_to': pay_currency_code.lower()
    }
    headers = {'x-api-key': NOWPAYMENTS_API_KEY}

    try:
        def make_estimate_request():
            try:
                response = requests.get(estimate_url, params=params, headers=headers, timeout=15)
                logger.debug(f"NOWPayments estimate response status: {response.status_code}, content: {response.text[:200]}")
                response.raise_for_status()
                return response.json()
            except requests.exceptions.Timeout:
                logger.error(f"NOWPayments estimate request timed out for {target_eur_amount} EUR to {pay_currency_code}.")
                return {'error': 'estimate_api_timeout'}
            except requests.exceptions.RequestException as e:
                logger.error(f"NOWPayments estimate request error for {target_eur_amount} EUR to {pay_currency_code}: {e}")
                # Try to parse error message if available
                error_detail = str(e)
                if e.response is not None:
                     error_detail = f"Status {e.response.status_code}: {e.response.text[:200]}"
                     if "currencies not found" in e.response.text.lower():
                         return {'error': 'estimate_currency_not_found', 'currency': pay_currency_code.upper()}
                return {'error': 'estimate_api_request_failed', 'details': error_detail}
            except Exception as e:
                 logger.error(f"Unexpected error during NOWPayments estimate call: {e}", exc_info=True)
                 return {'error': 'estimate_api_unexpected_error', 'details': str(e)}

        estimate_data = await asyncio.to_thread(make_estimate_request)

        # Validate response structure
        if 'error' not in estimate_data and 'estimated_amount' not in estimate_data:
             logger.error(f"Invalid estimate response structure: {estimate_data}")
             return {'error': 'invalid_estimate_response'}

        return estimate_data

    except Exception as e:
        logger.error(f"Unexpected error in _get_nowpayments_estimate: {e}", exc_info=True)
        return {'error': 'internal_estimate_error', 'details': str(e)}


# --- Refactored NOWPayments Deposit Creation ---
async def create_nowpayments_payment(
    user_id: int,
    target_eur_amount: Decimal, # This should be the FINAL amount after ALL discounts
    pay_currency_code: str,
    is_purchase: bool = False,
    basket_snapshot: list | None = None, # Snapshot used for recording pending deposit
    discount_code: str | None = None # General discount code used
) -> dict:
    """
    Creates a payment invoice using the NOWPayments API.
    Checks minimum amount. Stores extra info if it's a purchase.
    The target_eur_amount should already account for all discounts.
    """
    if not NOWPAYMENTS_API_KEY:
        logger.error("NOWPayments API key is not configured.")
        return {'error': 'payment_api_misconfigured'}

    log_type = "direct purchase" if is_purchase else "refill"
    logger.info(f"Attempting to create NOWPayments {log_type} invoice for user {user_id}, {target_eur_amount} EUR via {pay_currency_code}")

    # 1. Get Estimate from NOWPayments
    estimate_result = await _get_nowpayments_estimate(target_eur_amount, pay_currency_code)

    if 'error' in estimate_result:
        logger.error(f"Failed to get estimate for {target_eur_amount} EUR to {pay_currency_code}: {estimate_result}")
        if estimate_result['error'] == 'estimate_currency_not_found':
             return {'error': 'estimate_currency_not_found', 'currency': estimate_result.get('currency', pay_currency_code.upper())}
        return {'error': 'estimate_failed'} # Generic estimate failure

    estimated_crypto_amount = Decimal(str(estimate_result['estimated_amount']))
    logger.info(f"NOWPayments estimated {estimated_crypto_amount} {pay_currency_code} needed for {target_eur_amount} EUR")

    # 2. Check Minimum Payment Amount from NOWPayments
    min_amount_api = get_nowpayments_min_amount(pay_currency_code)
    if min_amount_api is None:
        logger.error(f"Could not fetch minimum payment amount for {pay_currency_code} from NOWPayments API.")
        return {'error': 'min_amount_fetch_error', 'currency': pay_currency_code.upper()}

    # Check if purchase value in crypto is below the minimum required by API
    if is_purchase and estimated_crypto_amount < min_amount_api:
         logger.warning(f"Basket purchase for user {user_id} ({target_eur_amount} EUR -> {estimated_crypto_amount} {pay_currency_code}) is below the API minimum {min_amount_api} {pay_currency_code}.")
         return {
             'error': 'basket_pay_too_low',
             'currency': pay_currency_code.upper(),
             'min_amount': f"{min_amount_api:.8f}".rstrip('0').rstrip('.'), # Format min amount nicely
             'basket_total': format_currency(target_eur_amount)
         }

    # Determine the amount for the invoice: Use estimate OR minimum, whichever is higher
    invoice_crypto_amount = max(estimated_crypto_amount, min_amount_api)
    if invoice_crypto_amount > estimated_crypto_amount:
        logger.warning(f"Estimated amount {estimated_crypto_amount} was below NOWPayments minimum {min_amount_api}. Using minimum for invoice: {invoice_crypto_amount} {pay_currency_code}")


    # 3. Prepare API Request Data
    order_id_prefix = "PURCHASE" if is_purchase else "REFILL"
    order_id = f"USER{user_id}_{order_id_prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    ipn_callback_url = f"{WEBHOOK_URL}/webhook"
    order_desc = f"Basket purchase for user {user_id}" if is_purchase else f"Balance top-up for user {user_id}"

    payload = {
        "price_amount": float(invoice_crypto_amount), # Use the potentially adjusted amount
        "price_currency": pay_currency_code.lower(),
        "pay_currency": pay_currency_code.lower(),
        "ipn_callback_url": ipn_callback_url,
        "order_id": order_id,
        "order_description": f"{order_desc} (~{target_eur_amount:.2f} EUR)",
        "is_fixed_rate": False, # Use variable rate for less chance of underpayment issues
    }
    headers = {'x-api-key': NOWPAYMENTS_API_KEY, 'Content-Type': 'application/json'}
    payment_url = f"{NOWPAYMENTS_API_URL}/v1/payment"

    # 4. Make Payment Creation API Call
    try:
        def make_payment_request():
            try:
                response = requests.post(payment_url, headers=headers, json=payload, timeout=20)
                logger.debug(f"NOWPayments create payment response status: {response.status_code}, content: {response.text[:200]}")
                response.raise_for_status()
                return response.json()
            except requests.exceptions.Timeout:
                 logger.error(f"NOWPayments payment API request timed out for order {order_id}.")
                 return {'error': 'api_timeout', 'internal': True}
            except requests.exceptions.RequestException as e:
                 logger.error(f"NOWPayments payment API request error for order {order_id}: {e}", exc_info=True)
                 status_code = e.response.status_code if e.response is not None else None
                 error_content = e.response.text if e.response is not None else "No response content"
                 if status_code == 401: return {'error': 'api_key_invalid'}
                 if status_code == 400 and "AMOUNT_MINIMAL_ERROR" in error_content:
                     logger.warning(f"NOWPayments rejected payment for {order_id} due to amount minimal error (API check).")
                     min_amount_fallback = f"{min_amount_api:.8f}".rstrip('0').rstrip('.')
                     # Return specific error information
                     return {
                         'error': 'amount_too_low_api',
                         'currency': pay_currency_code.upper(),
                         'min_amount': min_amount_fallback,
                         'crypto_amount': f"{invoice_crypto_amount:.8f}".rstrip('0').rstrip('.'),
                         'target_eur_amount': target_eur_amount # Pass original EUR target
                     }
                 return {'error': 'api_request_failed', 'details': str(e), 'status': status_code, 'content': error_content[:200]}
            except Exception as e:
                 logger.error(f"Unexpected error during NOWPayments payment API call for order {order_id}: {e}", exc_info=True)
                 return {'error': 'api_unexpected_error', 'details': str(e)}

        payment_data = await asyncio.to_thread(make_payment_request)
        if 'error' in payment_data:
             if payment_data['error'] == 'api_key_invalid': logger.critical("NOWPayments API Key seems invalid!")
             elif payment_data.get('internal'): logger.error("Internal error during API request (e.g., timeout).")
             elif payment_data['error'] == 'amount_too_low_api': return payment_data # Return specific error
             else: logger.error(f"NOWPayments API returned error during payment creation: {payment_data}")
             return payment_data # Return other errors as well

        # 5. Validate Payment Response
        required_keys = ['payment_id', 'pay_address', 'pay_amount', 'pay_currency', 'expiration_estimate_date']
        if not all(k in payment_data for k in required_keys):
             logger.error(f"Invalid response from NOWPayments payment API for order {order_id}: Missing keys. Response: {payment_data}")
             return {'error': 'invalid_api_response'}

        # Store the *actual* crypto amount required by the invoice
        expected_crypto_amount_from_invoice = Decimal(str(payment_data['pay_amount']))
        payment_data['target_eur_amount_orig'] = float(target_eur_amount) # Store the FINAL EUR amount requested
        payment_data['pay_amount'] = f"{expected_crypto_amount_from_invoice:.8f}".rstrip('0').rstrip('.') # Store formatted crypto amount
        payment_data['is_purchase'] = is_purchase # Pass flag through response for display logic

        # 6. Store Pending Deposit Info
        add_success = await asyncio.to_thread(
            add_pending_deposit,
            payment_data['payment_id'], user_id, payment_data['pay_currency'],
            float(target_eur_amount), float(expected_crypto_amount_from_invoice), # Store the actual invoice amount
            is_purchase=is_purchase,
            basket_snapshot=basket_snapshot, # Store the snapshot
            discount_code=discount_code      # Store general discount code used
        )
        if not add_success:
             logger.error(f"Failed to add pending deposit to DB for payment_id {payment_data['payment_id']} (user {user_id}).")
             # Attempt to cancel invoice? NOWPayments doesn't have a standard cancel API. Manual intervention needed if DB fails.
             return {'error': 'pending_db_error'}

        logger.info(f"Successfully created NOWPayments {log_type} invoice {payment_data['payment_id']} for user {user_id}.")
        return payment_data

    except Exception as e:
        logger.error(f"Unexpected error in create_nowpayments_payment for user {user_id}: {e}", exc_info=True)
        return {'error': 'internal_server_error', 'details': str(e)}


# --- Callback Handler for Crypto Selection during Refill ---
async def handle_select_refill_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting the crypto asset for refill, creates NOWPayments invoice."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en") # Get language
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_refill_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    selected_asset_code = params[0].lower()
    logger.info(f"User {user_id} selected {selected_asset_code} for refill.")

    refill_eur_amount_float = context.user_data.get('refill_eur_amount')
    if not refill_eur_amount_float or refill_eur_amount_float <= 0:
        logger.error(f"Refill amount context lost before asset selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Refill amount context lost. Please start the top up again.", parse_mode=None)
        context.user_data.pop('state', None)
        return

    refill_eur_amount_decimal = Decimal(str(refill_eur_amount_float))

    preparing_invoice_msg = lang_data.get("preparing_invoice", "⏳ Preparing your payment invoice...")
    failed_invoice_creation_msg = lang_data.get("failed_invoice_creation", "❌ Failed to create payment invoice. Please try again later or contact support.")
    error_nowpayments_api_msg = lang_data.get("error_nowpayments_api", "❌ Payment API Error: Could not create payment. Please try again later or contact support.")
    error_invalid_response_msg = lang_data.get("error_invalid_nowpayments_response", "❌ Payment API Error: Invalid response received. Please contact support.")
    error_api_key_msg = lang_data.get("error_nowpayments_api_key", "❌ Payment API Error: Invalid API key. Please contact support.")
    error_pending_db_msg = lang_data.get("payment_pending_db_error", "❌ Database Error: Could not record pending payment. Please contact support.")
    error_amount_too_low_api_msg = lang_data.get("payment_amount_too_low_api", "❌ Payment Amount Too Low: The equivalent of {target_eur_amount} EUR in {currency} ({crypto_amount}) is below the minimum required by the payment provider ({min_amount} {currency}). Please try a higher EUR amount.")
    error_min_amount_fetch_msg = lang_data.get("error_min_amount_fetch", "❌ Error: Could not retrieve minimum payment amount for {currency}. Please try again later or select a different currency.")
    error_estimate_failed_msg = lang_data.get("error_estimate_failed", "❌ Error: Could not estimate crypto amount. Please try again or select a different currency.")
    error_estimate_currency_not_found_msg = lang_data.get("error_estimate_currency_not_found", "❌ Error: Currency {currency} not supported for estimation. Please select a different currency.")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]])

    try:
        await query.edit_message_text(preparing_invoice_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Couldn't edit message in handle_select_refill_crypto: {e}")
        await query.answer("Preparing...")

    # Call payment creation - specify it's NOT a purchase
    payment_result = await create_nowpayments_payment(
        user_id, refill_eur_amount_decimal, selected_asset_code,
        is_purchase=False # Explicitly False for refill
    )

    if 'error' in payment_result:
        error_code = payment_result['error']
        logger.error(f"Failed to create NOWPayments refill invoice for user {user_id}: {error_code} - Details: {payment_result}")

        error_message_to_user = failed_invoice_creation_msg # Default error
        if error_code == 'estimate_failed': error_message_to_user = error_estimate_failed_msg
        elif error_code == 'estimate_currency_not_found': error_message_to_user = error_estimate_currency_not_found_msg.format(currency=payment_result.get('currency', selected_asset_code.upper()))
        elif error_code == 'min_amount_fetch_error': error_message_to_user = error_min_amount_fetch_msg.format(currency=payment_result.get('currency', selected_asset_code.upper()))
        elif error_code == 'api_key_invalid': error_message_to_user = error_api_key_msg
        elif error_code == 'invalid_api_response': error_message_to_user = error_invalid_response_msg
        elif error_code == 'pending_db_error': error_message_to_user = error_pending_db_msg
        elif error_code == 'amount_too_low_api': # Handle specific error with details
             min_amount_val = payment_result.get('min_amount', 'N/A'); crypto_amount_val = payment_result.get('crypto_amount', 'N/A')
             target_eur_val = payment_result.get('target_eur_amount', refill_eur_amount_decimal)
             error_message_to_user = error_amount_too_low_api_msg.format(target_eur_amount=format_currency(target_eur_val), currency=payment_result.get('currency', selected_asset_code.upper()), crypto_amount=crypto_amount_val, min_amount=min_amount_val)
        elif error_code in ['api_timeout', 'api_request_failed', 'api_unexpected_error', 'internal_server_error', 'internal_estimate_error']:
            error_message_to_user = error_nowpayments_api_msg

        try: await query.edit_message_text(error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        except Exception as edit_e: logger.error(f"Failed to edit message with invoice creation error: {edit_e}"); await send_message_with_retry(context.bot, chat_id, error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None) # Reset state on error
    else:
        logger.info(f"NOWPayments refill invoice created successfully for user {user_id}. Payment ID: {payment_result.get('payment_id')}")
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None)
        await display_nowpayments_invoice(update, context, payment_result)


# --- UPDATED: Callback Handler for Crypto Selection during Basket Payment ---
async def handle_select_basket_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting crypto asset for direct basket payment."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_basket_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    selected_asset_code = params[0].lower()
    logger.info(f"User {user_id} selected {selected_asset_code} for basket payment.")

    # Retrieve stored basket context
    basket_snapshot = context.user_data.get('basket_pay_snapshot')
    final_total_eur_float = context.user_data.get('basket_pay_total_eur') # This should be the FINAL total after ALL discounts
    discount_code_used = context.user_data.get('basket_pay_discount_code') # General discount code used

    if basket_snapshot is None or final_total_eur_float is None:
        logger.error(f"Basket payment context lost before crypto selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Payment context lost. Please go back to your basket.",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ View Basket", callback_data="view_basket")]]) ,parse_mode=None)
        context.user_data.pop('state', None)
        context.user_data.pop('basket_pay_snapshot', None); context.user_data.pop('basket_pay_total_eur', None); context.user_data.pop('basket_pay_discount_code', None)
        return

    final_total_eur_decimal = Decimal(str(final_total_eur_float))

    # Get language strings
    preparing_invoice_msg = lang_data.get("preparing_invoice", "⏳ Preparing your payment invoice...")
    failed_invoice_creation_msg = lang_data.get("failed_invoice_creation", "❌ Failed to create payment invoice. Please try again later or contact support.")
    error_nowpayments_api_msg = lang_data.get("error_nowpayments_api", "❌ Payment API Error: Could not create payment. Please try again later or contact support.")
    error_invalid_response_msg = lang_data.get("error_invalid_nowpayments_response", "❌ Payment API Error: Invalid response received. Please contact support.")
    error_api_key_msg = lang_data.get("error_nowpayments_api_key", "❌ Payment API Error: Invalid API key. Please contact support.")
    error_pending_db_msg = lang_data.get("payment_pending_db_error", "❌ Database Error: Could not record pending payment. Please contact support.")
    error_amount_too_low_api_msg = lang_data.get("payment_amount_too_low_api", "❌ Payment Amount Too Low: The equivalent of {target_eur_amount} EUR in {currency} ({crypto_amount}) is below the minimum required by the payment provider ({min_amount} {currency}). Please try a higher EUR amount.")
    error_min_amount_fetch_msg = lang_data.get("error_min_amount_fetch", "❌ Error: Could not retrieve minimum payment amount for {currency}. Please try again later or select a different currency.")
    error_estimate_failed_msg = lang_data.get("error_estimate_failed", "❌ Error: Could not estimate crypto amount. Please try again or select a different currency.")
    error_estimate_currency_not_found_msg = lang_data.get("error_estimate_currency_not_found", "❌ Error: Currency {currency} not supported for estimation. Please select a different currency.")
    error_basket_pay_too_low_msg = lang_data.get("basket_pay_too_low", "❌ Basket total {basket_total} EUR is below the minimum required for {currency}.")
    back_to_basket_button = lang_data.get("back_basket_button", "Back to Basket")
    back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_to_basket_button}", callback_data="view_basket")]])

    try:
        await query.edit_message_text(preparing_invoice_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.warning(f"Couldn't edit message in handle_select_basket_crypto: {e}")
        await query.answer("Preparing...")

    # Call payment creation - specify it IS a purchase, pass FINAL total
    payment_result = await create_nowpayments_payment(
        user_id, final_total_eur_decimal, selected_asset_code, # Pass final total
        is_purchase=True,
        basket_snapshot=basket_snapshot,
        discount_code=discount_code_used
    )

    # Store snapshot temporarily BEFORE clearing context, in case we need it for un-reserving
    snapshot_before_clear = context.user_data.get('basket_pay_snapshot')

    # Clear context *after* attempting payment creation
    context.user_data.pop('basket_pay_snapshot', None)
    context.user_data.pop('basket_pay_total_eur', None)
    context.user_data.pop('basket_pay_discount_code', None)
    context.user_data.pop('state', None) # Ensure state is cleared

    if 'error' in payment_result:
        error_code = payment_result['error']
        logger.error(f"Failed to create NOWPayments basket payment invoice for user {user_id}: {error_code} - Details: {payment_result}")

        # --- Un-reserve items if invoice creation failed early ---
        if error_code in ['basket_pay_too_low', 'amount_too_low_api', 'min_amount_fetch_error', 'estimate_failed', 'estimate_currency_not_found', 'payment_api_misconfigured']:
            logger.info(f"Invoice creation failed ({error_code}) before pending record. Un-reserving items from snapshot.")
            try:
                # Use asyncio.to_thread for the synchronous helper
                await asyncio.to_thread(_unreserve_basket_items, snapshot_before_clear)
            except NameError:
                 logger.critical("CRITICAL: _unreserve_basket_items function call failed due to NameError!")
            except Exception as unreserve_e:
                 logger.error(f"Error occurred during item un-reservation: {unreserve_e}")
        # --- End Un-reserve Fix ---

        error_message_to_user = failed_invoice_creation_msg # Default error
        # Handle specific errors for user message
        if error_code == 'basket_pay_too_low':
            error_message_to_user = error_basket_pay_too_low_msg.format(
                basket_total=payment_result.get('basket_total', 'N/A'),
                currency=payment_result.get('currency', selected_asset_code.upper())
            )
        elif error_code == 'estimate_failed': error_message_to_user = error_estimate_failed_msg
        elif error_code == 'estimate_currency_not_found': error_message_to_user = error_estimate_currency_not_found_msg.format(currency=payment_result.get('currency', selected_asset_code.upper()))
        elif error_code == 'min_amount_fetch_error': error_message_to_user = error_min_amount_fetch_msg.format(currency=payment_result.get('currency', selected_asset_code.upper()))
        elif error_code == 'api_key_invalid': error_message_to_user = error_api_key_msg
        elif error_code == 'invalid_api_response': error_message_to_user = error_invalid_response_msg
        elif error_code == 'pending_db_error': error_message_to_user = error_pending_db_msg
        elif error_code == 'amount_too_low_api':
             min_amount_val = payment_result.get('min_amount', 'N/A'); crypto_amount_val = payment_result.get('crypto_amount', 'N/A')
             target_eur_val = payment_result.get('target_eur_amount', final_total_eur_decimal)
             error_message_to_user = error_amount_too_low_api_msg.format(target_eur_amount=format_currency(target_eur_val), currency=payment_result.get('currency', selected_asset_code.upper()), crypto_amount=crypto_amount_val, min_amount=min_amount_val)
        elif error_code in ['api_timeout', 'api_request_failed', 'api_unexpected_error', 'internal_server_error', 'internal_estimate_error']:
            error_message_to_user = error_nowpayments_api_msg

        try: await query.edit_message_text(error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        except Exception as edit_e: logger.error(f"Failed to edit message with basket payment creation error: {edit_e}"); await send_message_with_retry(context.bot, chat_id, error_message_to_user, reply_markup=back_button_markup, parse_mode=None)

        # The user needs to click the "Back to Basket" button.

    else:
        logger.info(f"NOWPayments basket payment invoice created successfully for user {user_id}. Payment ID: {payment_result.get('payment_id')}")
        # Display the invoice (same function as refill)
        await display_nowpayments_invoice(update, context, payment_result)
        # Important: DO NOT clear the user's actual basket here.


# --- Display NOWPayments Invoice (with Cancel Button fix) ---
async def display_nowpayments_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_data: dict):
    """Displays the NOWPayments invoice details with improved formatting and a specific cancel button."""
    query = update.callback_query
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    final_msg = "Error displaying invoice."
    is_purchase_invoice = payment_data.get('is_purchase', False)

    try:
        pay_address = payment_data.get('pay_address')
        pay_amount_str = payment_data.get('pay_amount')
        pay_currency = payment_data.get('pay_currency', 'N/A').upper()
        payment_id = payment_data.get('payment_id') # <<< Get payment_id
        target_eur_orig = payment_data.get('target_eur_amount_orig')
        expiration_date_str = payment_data.get('expiration_estimate_date')

        if not pay_address or not pay_amount_str or not payment_id: # <<< Check payment_id too
            logger.error(f"Missing critical data in NOWPayments response for display: {payment_data}")
            raise ValueError("Missing payment address, amount, or ID")

        # --- Store payment_id in user_data for cancellation ---
        context.user_data['pending_payment_id'] = payment_id
        logger.debug(f"Stored pending_payment_id {payment_id} in user_data.")
        # -------------------------------------------------------

        pay_amount_decimal = Decimal(pay_amount_str)
        pay_amount_display = '{:f}'.format(pay_amount_decimal.normalize())
        target_eur_display = format_currency(Decimal(str(target_eur_orig))) if target_eur_orig else "N/A"
        expiry_time_display = format_expiration_time(expiration_date_str)

        invoice_title_template = lang_data.get("invoice_title_purchase", "*Payment Invoice Created*") if is_purchase_invoice else lang_data.get("invoice_title_refill", "*Top\\-Up Invoice Created*")
        amount_label = lang_data.get("amount_label", "*Amount:*")
        payment_address_label = lang_data.get("payment_address_label", "*Payment Address:*")
        expires_at_label = lang_data.get("expires_at_label", "*Expires At:*")
        send_warning_template = lang_data.get("send_warning_template", "⚠️ *Important:* Send *exactly* this amount of {asset} to this address\\.")
        confirmation_note = lang_data.get("confirmation_note", "✅ Confirmation is automatic via webhook after network confirmation\\.")
        overpayment_note = lang_data.get("overpayment_note", "ℹ️ _Sending more than this amount is okay\\! Your balance will be credited based on the amount received after network confirmation\\._")
        # --- Use a specific "Cancel Payment" text ---
        cancel_payment_button_text = lang_data.get("cancel_payment_button", "Cancel Payment")
        # --------------------------------------------

        escaped_target_eur = helpers.escape_markdown(target_eur_display, version=2)
        escaped_pay_amount = helpers.escape_markdown(pay_amount_display, version=2)
        escaped_currency = helpers.escape_markdown(pay_currency, version=2)
        escaped_address = helpers.escape_markdown(pay_address, version=2)
        escaped_expiry = helpers.escape_markdown(expiry_time_display, version=2)

        msg = f"""{invoice_title_template}

_{helpers.escape_markdown(f"(Amount: {target_eur_display} EUR)", version=2)}_

Please send the following amount:
{amount_label} `{escaped_pay_amount}` {escaped_currency}

{payment_address_label}
`{escaped_address}`

{expires_at_label} {escaped_expiry}

"""
        if is_purchase_invoice: msg += f"{send_warning_template.format(asset=escaped_currency)}\n"
        else: msg += f"{overpayment_note}\n"
        msg += f"\n{confirmation_note}"

        final_msg = msg.strip()

        # --- Change Cancel Button Callback ---
        keyboard = [[InlineKeyboardButton(f"❌ {cancel_payment_button_text}", callback_data="cancel_crypto_payment")]] # <<< CHANGED callback_data
        # -------------------------------------

        await query.edit_message_text(
            final_msg, reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Error formatting or displaying NOWPayments invoice: {e}. Data: {payment_data}", exc_info=True)
        error_display_msg = lang_data.get("error_preparing_payment", "❌ An error occurred while preparing the payment details. Please try again later.")
        # Determine correct back button on error too (fallback if cancel fails)
        back_button_text = lang_data.get("back_basket_button", "Back to Basket") if is_purchase_invoice else lang_data.get("back_profile_button", "Back to Profile")
        back_callback = "view_basket" if is_purchase_invoice else "profile"
        back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_button_text}", callback_data=back_callback)]])
        try: await query.edit_message_text(error_display_msg, reply_markup=back_button_markup, parse_mode=None)
        except Exception: pass
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower(): logger.error(f"Error editing NOWPayments invoice message: {e}. Attempted message (unescaped for logging): {msg.strip()}")
        else: await query.answer()
    except Exception as e:
         logger.error(f"Unexpected error in display_nowpayments_invoice: {e}", exc_info=True)
         error_display_msg = lang_data.get("error_preparing_payment", "❌ An unexpected error occurred while preparing the payment details.")
         back_button_text = lang_data.get("back_basket_button", "Back to Basket") if is_purchase_invoice else lang_data.get("back_profile_button", "Back to Profile")
         back_callback = "view_basket" if is_purchase_invoice else "profile"
         back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_button_text}", callback_data=back_callback)]])
         try: await query.edit_message_text(error_display_msg, reply_markup=back_button_markup, parse_mode=None)
         except Exception: pass


# --- Process Successful Refill ---
async def process_successful_refill(user_id: int, amount_to_add_eur: Decimal, payment_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    bot = context.bot
    user_lang = 'en'
    conn_lang = None
    try:
        conn_lang = get_db_connection()
        c_lang = conn_lang.cursor()
        c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        lang_res = c_lang.fetchone()
        if lang_res and lang_res['language'] in LANGUAGES:
            user_lang = lang_res['language']
    except sqlite3.Error as e:
        logger.error(f"DB error fetching language for user {user_id} during refill confirmation: {e}")
    finally:
        if conn_lang: conn_lang.close()

    lang_data = LANGUAGES.get(user_lang, LANGUAGES['en'])

    if not isinstance(amount_to_add_eur, Decimal) or amount_to_add_eur <= Decimal('0.0'):
        logger.error(f"Invalid amount_to_add_eur in process_successful_refill: {amount_to_add_eur}")
        return False

    # Use the separate crediting function
    return await credit_user_balance(user_id, amount_to_add_eur, f"Refill payment {payment_id}", context)


# --- HELPER: Finalize Purchase (Send Caption Separately) ---
async def _finalize_purchase(user_id: int, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Shared logic to finalize a purchase after payment confirmation (balance or crypto).
    Decrements stock, adds purchase record, sends media first, then text separately,
    cleans up product records.
    """
    chat_id = context._chat_id or context._user_id or user_id # Try to get chat_id
    if not chat_id:
         logger.error(f"Cannot determine chat_id for user {user_id} in _finalize_purchase")

    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} purchase finalization."); return False

    conn = None
    processed_product_ids = []
    purchases_to_insert = []
    final_pickup_details = defaultdict(list)
    db_update_successful = False
    total_price_paid_decimal = Decimal('0.0')

    # --- Database Operations (Reservation Decrement, Purchase Record) ---
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE")

        product_ids_in_snapshot = list(set(item['product_id'] for item in basket_snapshot))
        if not product_ids_in_snapshot:
            logger.warning(f"Empty snapshot IDs user {user_id} finalization."); conn.rollback(); return False

        placeholders = ','.join('?' * len(product_ids_in_snapshot))
        c.execute(f"SELECT id, name, product_type, size, price, city, district, original_text FROM products WHERE id IN ({placeholders})", product_ids_in_snapshot)
        product_db_details = {row['id']: dict(row) for row in c.fetchall()}
        purchase_time_iso = datetime.now(timezone.utc).isoformat()

        for item_snapshot in basket_snapshot:
            product_id = item_snapshot['product_id']
            details = product_db_details.get(product_id)
            if not details:
                logger.error(f"CRITICAL: Reserved product {product_id} missing from DB during finalization user {user_id}. Skipping item.")
                continue

            avail_update = c.execute("UPDATE products SET available = available - 1 WHERE id = ? AND available > 0", (product_id,))
            if avail_update.rowcount == 0:
                logger.error(f"CRITICAL: Failed available decrement for reserved product P{product_id} user {user_id}. Race condition or logic error?")
                continue

            item_original_price_decimal = Decimal(str(details['price']))
            item_product_type = details['product_type']
            item_reseller_discount_percent = Decimal('0.0')
            try:
                # Use asyncio.to_thread for sync DB call within async function
                item_reseller_discount_percent = await asyncio.to_thread(get_reseller_discount, user_id, item_product_type)
            except Exception as e:
                logger.error(f"Error calling get_reseller_discount for user {user_id} type {item_product_type}: {e}")

            item_reseller_discount_amount = (item_original_price_decimal * item_reseller_discount_percent / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            item_price_paid_decimal = item_original_price_decimal - item_reseller_discount_amount
            total_price_paid_decimal += item_price_paid_decimal
            item_price_paid_float = float(item_price_paid_decimal)

            purchases_to_insert.append((
                user_id, product_id, details['name'], item_product_type, details['size'],
                item_price_paid_float, details['city'], details['district'], purchase_time_iso
            ))
            processed_product_ids.append(product_id)
            final_pickup_details[product_id].append({'name': details['name'], 'size': details['size'], 'text': details.get('original_text')})

        if not purchases_to_insert:
            logger.warning(f"No items processed during finalization for user {user_id}. Rolling back.")
            conn.rollback()
            if chat_id: await send_message_with_retry(context.bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase."), parse_mode=None)
            return False

        c.executemany("INSERT INTO purchases (user_id, product_id, product_name, product_type, product_size, price_paid, city, district, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", purchases_to_insert)
        c.execute("UPDATE users SET total_purchases = total_purchases + ? WHERE user_id = ?", (len(purchases_to_insert), user_id))
        if discount_code_used:
            c.execute("UPDATE discount_codes SET uses_count = uses_count + 1 WHERE code = ?", (discount_code_used,))
        c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,))
        conn.commit()
        db_update_successful = True
        logger.info(f"Finalized purchase DB update user {user_id}. Processed {len(purchases_to_insert)} items. General Discount: {discount_code_used or 'None'}. Total Paid (after reseller disc): {total_price_paid_decimal:.2f} EUR")

    except sqlite3.Error as e:
        logger.error(f"DB error during purchase finalization user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error during purchase finalization user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

    # --- Post-Transaction Cleanup & Message Sending (If DB success) ---
    if db_update_successful:
        context.user_data['basket'] = []
        context.user_data.pop('applied_discount', None)

        # Fetch Media
        media_details = defaultdict(list)
        if processed_product_ids:
            conn_media = None
            try:
                conn_media = get_db_connection()
                c_media = conn_media.cursor()
                media_placeholders = ','.join('?' * len(processed_product_ids))
                c_media.execute(f"SELECT product_id, media_type, telegram_file_id, file_path FROM product_media WHERE product_id IN ({media_placeholders})", processed_product_ids)
                for row in c_media.fetchall(): media_details[row['product_id']].append(dict(row))
            except sqlite3.Error as e: logger.error(f"DB error fetching media post-purchase: {e}")
            finally:
                if conn_media: conn_media.close()

        # Send Pickup Details
        if chat_id:
            success_title = lang_data.get("purchase_success", "🎉 Purchase Complete! Pickup details below:")
            await send_message_with_retry(context.bot, chat_id, success_title, parse_mode=None)

            for prod_id in processed_product_ids:
                item_details_list = final_pickup_details.get(prod_id)
                if not item_details_list: continue
                item_details = item_details_list[0]
                item_name, item_size = item_details['name'], item_details['size']
                item_original_text = item_details['text'] or "(No specific pickup details provided)"
                product_type = product_db_details.get(prod_id, {}).get('product_type', 'Product')
                product_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
                item_header = f"--- Item: {product_emoji} {item_name} {item_size} ---"

                # Prepare combined text caption
                combined_caption = f"{item_header}\n\n{item_original_text}"
                if len(combined_caption) > 4090: combined_caption = combined_caption[:4090] + "..." # Adjust for send_message limit

                media_items_for_product = media_details.get(prod_id, [])
                photo_video_group_details = []
                animations_to_send_details = []
                opened_files = []

                # --- Separate Media ---
                for media_item in media_items_for_product:
                    media_type = media_item.get('media_type')
                    file_id = media_item.get('telegram_file_id')
                    file_path = media_item.get('file_path')
                    if media_type in ['photo', 'video']:
                        photo_video_group_details.append({'type': media_type, 'id': file_id, 'path': file_path})
                    elif media_type == 'gif':
                        animations_to_send_details.append({'type': media_type, 'id': file_id, 'path': file_path})
                    else:
                        logger.warning(f"Unsupported media type '{media_type}' found for P{prod_id}")

                # --- Send Photos/Videos Group (No Caption) ---
                if photo_video_group_details:
                    media_group_input = []
                    files_for_this_group = []
                    try:
                        for item in photo_video_group_details:
                            input_media = None; file_handle = None
                            if item['id']:
                                if item['type'] == 'photo': input_media = InputMediaPhoto(media=item['id'])
                                elif item['type'] == 'video': input_media = InputMediaVideo(media=item['id'])
                            elif item['path'] and await asyncio.to_thread(os.path.exists, item['path']):
                                file_handle = await asyncio.to_thread(open, item['path'], 'rb')
                                opened_files.append(file_handle)
                                files_for_this_group.append(file_handle)
                                if item['type'] == 'photo': input_media = InputMediaPhoto(media=file_handle)
                                elif item['type'] == 'video': input_media = InputMediaVideo(media=file_handle)
                            if input_media: media_group_input.append(input_media)
                            else: logger.warning(f"Could not prepare photo/video InputMedia P{prod_id}: {item}")

                        if media_group_input:
                            await context.bot.send_media_group(chat_id, media=media_group_input, connect_timeout=20, read_timeout=20)
                            logger.info(f"Sent photo/video group ({len(media_group_input)}) for P{prod_id} user {user_id}.")
                    except Exception as group_e:
                        logger.error(f"Error sending photo/video group P{prod_id}: {group_e}")
                    finally:
                        for f in files_for_this_group:
                             try:
                                 if not f.closed: await asyncio.to_thread(f.close); opened_files.remove(f)
                             except Exception: pass

                # --- Send Animations (GIFs) Separately (No Caption) ---
                for item in animations_to_send_details:
                    anim_file_handle = None
                    try:
                        media_to_send_ref = None
                        if item['id']:
                            media_to_send_ref = item['id']
                        elif item['path'] and await asyncio.to_thread(os.path.exists, item['path']):
                            anim_file_handle = await asyncio.to_thread(open, item['path'], 'rb')
                            opened_files.append(anim_file_handle)
                            media_to_send_ref = anim_file_handle
                        else:
                            logger.warning(f"Could not find GIF source for P{prod_id}: ID={item['id']}, Path={item['path']}")
                            continue

                        await context.bot.send_animation(chat_id=chat_id, animation=media_to_send_ref) # NO CAPTION
                        logger.info(f"Sent animation for P{prod_id} user {user_id}.")
                    except Exception as anim_e:
                        logger.error(f"Error sending animation P{prod_id}: {anim_e}")
                    finally:
                        if anim_file_handle and anim_file_handle in opened_files:
                            try: await asyncio.to_thread(anim_file_handle.close); opened_files.remove(anim_file_handle)
                            except Exception: pass

                # --- Always Send Combined Text Separately ---
                if combined_caption:
                    await send_message_with_retry(context.bot, chat_id, combined_caption, parse_mode=None)
                    logger.info(f"Sent separate text details for P{prod_id} user {user_id}.")
                else:
                     # Create a fallback message if both original text and header are missing somehow
                    fallback_text = f"(No details provided for Product ID {prod_id})"
                    await send_message_with_retry(context.bot, chat_id, fallback_text, parse_mode=None)
                    logger.warning(f"No combined caption text to send for P{prod_id} user {user_id}. Sent fallback.")

            # --- Close any remaining opened file handles ---
            for f in opened_files:
                try:
                    if not f.closed: await asyncio.to_thread(f.close)
                except Exception as close_e: logger.warning(f"Error closing file handle during final cleanup: {close_e}")

            # --- Product Record Deletion ---
            conn_del = None
            try:
                conn_del = get_db_connection()
                c_del = conn_del.cursor()
                ids_tuple_list = [(pid,) for pid in processed_product_ids]
                logger.info(f"Purchase Finalization: Attempting to delete product records for user {user_id}. IDs: {processed_product_ids}")
                delete_result = c_del.executemany("DELETE FROM products WHERE id = ?", ids_tuple_list)
                conn_del.commit()
                deleted_count = delete_result.rowcount
                logger.info(f"Deleted {deleted_count} purchased product records for user {user_id}. IDs: {processed_product_ids}")
            except sqlite3.Error as e: logger.error(f"DB error deleting purchased products: {e}", exc_info=True); conn_del.rollback() if conn_del and conn_del.in_transaction else None
            except Exception as e: logger.error(f"Unexpected error deleting purchased products: {e}", exc_info=True)
            finally:
                if conn_del: conn_del.close()

            # --- Final Message to User ---
            leave_review_button = lang_data.get("leave_review_button", "Leave a Review")
            keyboard = [[InlineKeyboardButton(f"✍️ {leave_review_button}", callback_data="leave_review_now")]]
            await send_message_with_retry(context.bot, chat_id, "Thank you for your purchase!", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

        return True # Indicate success
    else: # Purchase failed at DB level
        context.user_data['basket'] = []
        context.user_data.pop('applied_discount', None)
        if chat_id: await send_message_with_retry(context.bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase."), parse_mode=None)
        return False


# --- Process Purchase with Balance (Uses Helper) ---
async def process_purchase_with_balance(user_id: int, amount_to_deduct: Decimal, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates when paying with internal balance."""
    chat_id = context._chat_id or context._user_id or user_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} balance purchase."); return False
    if not isinstance(amount_to_deduct, Decimal) or amount_to_deduct < Decimal('0.0'): logger.error(f"Invalid amount_to_deduct {amount_to_deduct}."); return False

    conn = None
    db_balance_deducted = False
    balance_changed_error = lang_data.get("balance_changed_error", "❌ Transaction failed: Balance changed.")
    error_processing_purchase_contact_support = lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support.")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN EXCLUSIVE")
        # 1. Verify balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        current_balance_result = c.fetchone()
        if not current_balance_result or Decimal(str(current_balance_result['balance'])) < amount_to_deduct:
             logger.warning(f"Insufficient balance user {user_id}. Needed: {amount_to_deduct:.2f}")
             conn.rollback()
             # --- Unreserve items if balance check fails ---
             logger.info(f"Un-reserving items for user {user_id} due to insufficient balance during payment.")
             # Use asyncio.to_thread for synchronous helper
             await asyncio.to_thread(_unreserve_basket_items, basket_snapshot)
             # --- End Unreserve ---
             if chat_id: await send_message_with_retry(context.bot, chat_id, balance_changed_error, parse_mode=None)
             return False
        # 2. Deduct balance
        amount_float_to_deduct = float(amount_to_deduct)
        update_res = c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount_float_to_deduct, user_id))
        if update_res.rowcount == 0: logger.error(f"Failed to deduct balance user {user_id}."); conn.rollback(); return False

        conn.commit() # Commit balance deduction *before* finalizing items
        db_balance_deducted = True
        logger.info(f"Deducted {amount_to_deduct:.2f} EUR from balance for user {user_id}.")

    except sqlite3.Error as e:
        logger.error(f"DB error deducting balance user {user_id}: {e}", exc_info=True); db_balance_deducted = False
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

    # 3. Finalize purchase ONLY if balance was successfully deducted
    if db_balance_deducted:
        logger.info(f"Calling _finalize_purchase for user {user_id} after balance deduction.")
        # Now call the shared finalization logic
        finalize_success = await _finalize_purchase(user_id, basket_snapshot, discount_code_used, context)
        if not finalize_success:
            # Critical issue: Balance deducted but finalization failed.
            logger.critical(f"CRITICAL: Balance deducted for user {user_id} but _finalize_purchase FAILED! Attempting to refund.")
            refund_conn = None
            try:
                refund_conn = get_db_connection()
                refund_c = refund_conn.cursor()
                refund_c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount_float_to_deduct, user_id))
                refund_conn.commit()
                logger.info(f"Successfully refunded {amount_float_to_deduct} EUR to user {user_id} after finalization failure.")
                if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support + " Balance refunded.", parse_mode=None)
            except Exception as refund_e:
                logger.critical(f"CRITICAL REFUND FAILED for user {user_id}: {refund_e}. Manual balance correction required.")
                if ADMIN_ID and chat_id: # Notify admin if refund fails
                    await send_message_with_retry(context.bot, ADMIN_ID, f"⚠️ CRITICAL REFUND FAILED for user {user_id} after purchase finalization error. Amount: {amount_to_deduct}. MANUAL CORRECTION NEEDED!", parse_mode=None)
                if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support, parse_mode=None)
            finally:
                if refund_conn: refund_conn.close()
        return finalize_success
    else:
        logger.error(f"Skipping purchase finalization for user {user_id} due to balance deduction failure.")
        # --- Unreserve items if balance deduction failed ---
        logger.info(f"Un-reserving items for user {user_id} due to balance deduction failure.")
        # Use asyncio.to_thread for synchronous helper
        await asyncio.to_thread(_unreserve_basket_items, basket_snapshot)
        # --- End Unreserve ---
        if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support, parse_mode=None)
        return False

# --- Process Successful Crypto Purchase (Uses Helper) ---
async def process_successful_crypto_purchase(user_id: int, basket_snapshot: list, discount_code_used: str | None, payment_id: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles finalizing a purchase paid via crypto webhook."""
    chat_id = context._chat_id or context._user_id or user_id # Try to get chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    logger.info(f"Processing successful crypto purchase for user {user_id}, payment {payment_id}. Basket items: {len(basket_snapshot) if basket_snapshot else 0}")

    if not basket_snapshot:
        logger.error(f"CRITICAL: Successful crypto payment {payment_id} for user {user_id} received, but basket snapshot was empty/missing in pending record.")
        if ADMIN_ID and chat_id:
            try:
                await send_message_with_retry(context.bot, ADMIN_ID, f"⚠️ Critical Issue: Crypto payment {payment_id} success for user {user_id}, but basket data missing! Manual check needed.", parse_mode=None)
            except Exception as admin_notify_e:
                logger.error(f"Failed to notify admin about critical missing basket data: {admin_notify_e}")
        return False # Cannot proceed

    # Call the shared finalization logic
    finalize_success = await _finalize_purchase(user_id, basket_snapshot, discount_code_used, context)

    if finalize_success:
        # _finalize_purchase now handles the user-facing confirmation messages
        logger.info(f"Crypto purchase finalized for {user_id}, payment {payment_id}. _finalize_purchase handled user messages.")
    else:
        # Finalization failed even after payment confirmed. This is bad.
        logger.error(f"CRITICAL: Crypto payment {payment_id} success for user {user_id}, but _finalize_purchase failed! Items paid for but not processed in DB correctly.")
        if ADMIN_ID and chat_id:
            try:
                await send_message_with_retry(context.bot, ADMIN_ID, f"⚠️ Critical Issue: Crypto payment {payment_id} success for user {user_id}, but finalization FAILED! Check logs! MANUAL INTERVENTION REQUIRED.", parse_mode=None)
            except Exception as admin_notify_e:
                 logger.error(f"Failed to notify admin about critical finalization failure: {admin_notify_e}")
        if chat_id:
            await send_message_with_retry(context.bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support."), parse_mode=None)

    return finalize_success


# --- NEW: Helper Function to Credit User Balance (Moved from Previous Response) ---
async def credit_user_balance(user_id: int, amount_eur: Decimal, reason: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Adds funds to a user's balance and notifies them."""
    if not isinstance(amount_eur, Decimal) or amount_eur <= Decimal('0.0'):
        logger.error(f"Invalid amount provided to credit_user_balance for user {user_id}: {amount_eur}")
        return False

    conn = None
    db_update_successful = False
    amount_float = float(amount_eur)
    new_balance_decimal = Decimal('0.0')

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        logger.info(f"Attempting to credit balance for user {user_id} by {amount_float:.2f} EUR. Reason: {reason}")

        # Get old balance for logging
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        old_balance_res = c.fetchone(); old_balance_float = old_balance_res['balance'] if old_balance_res else 0.0

        update_result = c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount_float, user_id))
        if update_result.rowcount == 0:
            logger.error(f"User {user_id} not found during balance credit update. Reason: {reason}")
            conn.rollback()
            return False

        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance_result = c.fetchone()
        if new_balance_result:
             new_balance_decimal = Decimal(str(new_balance_result['balance']))
        else:
             logger.error(f"Could not fetch new balance for {user_id} after credit update."); conn.rollback(); return False

        conn.commit()
        db_update_successful = True
        logger.info(f"Successfully credited balance for user {user_id}. Added: {amount_eur:.2f} EUR. New Balance: {new_balance_decimal:.2f} EUR. Reason: {reason}")

        # Log this as an automatic system action (or maybe under ADMIN_ID if preferred)
        log_admin_action(
             admin_id=0, # Or ADMIN_ID if you want admin to "own" these logs
             action="BALANCE_CREDIT_AUTO",
             target_user_id=user_id,
             reason=reason,
             amount_change=amount_float,
             old_value=old_balance_float,
             new_value=float(new_balance_decimal)
        )

        # Notify User
        bot_instance = context.bot if hasattr(context, 'bot') else None
        if bot_instance:
            # Get user language for notification
            lang = context.user_data.get("lang", "en") # Get from context if available
            if not lang: # Fallback: Get from DB if not in context
                conn_lang = None
                try:
                    conn_lang = get_db_connection()
                    c_lang = conn_lang.cursor()
                    c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
                    lang_res = c_lang.fetchone()
                    if lang_res and lang_res['language'] in LANGUAGES: lang = lang_res['language']
                except Exception as lang_e: logger.warning(f"Could not fetch user lang for credit msg: {lang_e}")
                finally:
                     if conn_lang: conn_lang.close()
            lang_data = LANGUAGES.get(lang, LANGUAGES['en'])


            # <<< TODO: Add these messages to LANGUAGES dictionary >>>
            if "Overpayment" in reason:
                # Example message key: "credit_overpayment_purchase"
                notify_msg_template = lang_data.get("credit_overpayment_purchase", "✅ Your purchase was successful! Additionally, an overpayment of {amount} EUR has been credited to your balance. Your new balance is {new_balance} EUR.")
            elif "Underpayment" in reason:
                # Example message key: "credit_underpayment_purchase"
                 notify_msg_template = lang_data.get("credit_underpayment_purchase", "ℹ️ Your purchase failed due to underpayment, but the received amount ({amount} EUR) has been credited to your balance. Your new balance is {new_balance} EUR.")
            else: # Generic credit (like Refill)
                # Example message key: "credit_refill"
                notify_msg_template = lang_data.get("credit_refill", "✅ Your balance has been credited by {amount} EUR. Reason: {reason}. New balance: {new_balance} EUR.")

            notify_msg = notify_msg_template.format(
                amount=format_currency(amount_eur),
                new_balance=format_currency(new_balance_decimal),
                reason=reason # Include reason for generic credits
            )

            await send_message_with_retry(bot_instance, user_id, notify_msg, parse_mode=None)
        else:
             logger.error(f"Could not get bot instance to notify user {user_id} about balance credit.")

        return True

    except sqlite3.Error as e:
        logger.error(f"DB error during credit_user_balance user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False
    except Exception as e:
         logger.error(f"Unexpected error during credit_user_balance user {user_id}: {e}", exc_info=True)
         if conn and conn.in_transaction: conn.rollback()
         return False
    finally:
        if conn: conn.close()
# --- END credit_user_balance ---


# --- Callback Handler Wrapper (to keep main.py structure) ---
async def handle_confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """
    This is a wrapper function.
    The main logic for confirm_pay is now in user.py.
    This function ensures the callback router in main.py finds a handler here.
    """
    logger.debug("Payment.handle_confirm_pay called, forwarding to user.handle_confirm_pay")
    # Call the actual handler which is now located in user.py
    await user.handle_confirm_pay(update, context, params)

# --- NEW Handler for Cancelling Crypto Payment ---
async def handle_cancel_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles user explicitly cancelling a pending crypto payment."""
    query = update.callback_query
    user_id = query.from_user.id
    lang, lang_data = _get_lang_data(context) # <<< FIXED: Use helper

    payment_id = context.user_data.pop('pending_payment_id', None)

    if payment_id:
        logger.info(f"User {user_id} cancelled pending crypto payment {payment_id}.")
        # Call remove_pending_deposit, which will handle un-reserving if needed
        # Run the synchronous DB operation in a separate thread
        await asyncio.to_thread(remove_pending_deposit, payment_id, trigger='user_cancel')
        cancel_confirm_msg = lang_data.get("payment_cancelled_user", "Payment cancelled. Reserved items (if any) have been released.")
        await query.answer(cancel_confirm_msg)
    else:
        logger.warning(f"User {user_id} clicked cancel_crypto_payment, but no pending_payment_id found in context.")
        cancel_error_msg = lang_data.get("payment_cancel_error", "Could not cancel payment (already processed or context lost).")
        await query.answer(cancel_error_msg, show_alert=True)

    # Always attempt to refresh the basket view
    logger.debug(f"Redirecting user {user_id} back to basket view after crypto cancel attempt.")
    await user.handle_view_basket(update, context)

# --- END OF FILE payment.py ---
