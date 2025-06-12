import os
import logging
import pymongo.errors
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from pymongo import MongoClient
import re

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
YOUR_TELEGRAM_USER_ID = int(os.getenv("YOUR_TELEGRAM_USER_ID"))  # Convert to int
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]  # Multiple admins
ADMIN_IDS.append(YOUR_TELEGRAM_USER_ID)  # Ensure primary admin is included
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))  # Convert to int
AUTO_FILTER_BOT_USERNAME = os.getenv("AUTO_FILTER_BOT_USERNAME") # Your auto-filter bot's username (without @)
PAYMENT_SCREENSHOT_LINK = os.getenv("PAYMENT_SCREENSHOT_LINK", "")  # Optional link for payment proof

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MongoDB Setup ---
try:
    # URL-encode password if needed
    password = os.getenv("MONGO_PASSWORD")
    if password:
        from urllib.parse import quote_plus
        encoded_password = quote_plus(password)
        MONGO_URI = MONGO_URI.replace("<password>", encoded_password)
    
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db_name = os.getenv("MONGO_DB_NAME", "moviehub")
    db = client[db_name]
    payments_collection = db["payments"]
    users_collection = db["users"]
    logger.info(f"MongoDB connected successfully to database: {db_name}")
except Exception as e:
    logger.critical(f"Error connecting to MongoDB: {e}")
    if "bad auth" in str(e).lower():
        logger.critical("Authentication failed. Please check MongoDB username and password.")
    elif "Temporary failure in name resolution" in str(e):
        logger.critical("Network issue. Check your DNS settings or MongoDB cluster configuration.")
    exit(1)

# --- Helper Functions ---

def parse_time_period(amount):
    """Interpolates the time period in days based on the amount paid."""
    tiers = [
        {"amount": 5, "days": 3},
        {"amount": 10, "days": 7},
        {"amount": 25, "days": 30},
        {"amount": 60, "days": 90},
        {"amount": 100, "days": 180},
        {"amount": 150, "days": 365},
    ]

    amount = max(5, min(150, amount))  # Clamp amount between 5 and 150

    exact_tier = next((t for t in tiers if t["amount"] == amount), None)
    if exact_tier:
        total_days = exact_tier["days"]
    else:
        if amount < tiers[0]["amount"]:
            total_days = max(1, round((amount / tiers[0]["amount"]) * tiers[0]["days"]))
        elif amount > tiers[-1]["amount"]:
            total_days = round((amount / tiers[-1]["amount"]) * tiers[-1]["days"])
        else:
            lower_tier, upper_tier = None, None
            for i in range(len(tiers) - 1):
                if amount >= tiers[i]["amount"] and amount <= tiers[i+1]["amount"]:
                    lower_tier = tiers[i]
                    upper_tier = tiers[i+1]
                    break
            
            if lower_tier and upper_tier:
                ratio = (amount - lower_tier["amount"]) / (upper_tier["amount"] - lower_tier["amount"])
                total_days = round(lower_tier["days"] + ratio * (upper_tier["days"] - lower_tier["days"]))
            else:
                total_days = 0

    if total_days >= 365 and total_days % 365 == 0:
        return f"{total_days // 365}year"
    elif total_days >= 30 and total_days % 30 == 0:
        return f"{total_days // 30}month"
    elif total_days > 0:
        return f"{total_days}days"
    else:
        return "1day"

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, message: str, reply_markup=None):
    """Sends a message to the designated log channel."""
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=message,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        logger.info(f"Logged to channel: {message}")
    except Exception as e:
        logger.error(f"Failed to send log to channel: {e}")

async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
    """Sends a notification to a user."""
    try:
        await context.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")
        # Try to notify admin if user notification fails
        await log_to_channel(context, f"⚠️ Failed to notify user {user_id}: {e}")

async def create_payment_buttons(payment_id):
    """Create inline buttons for approving/rejecting a payment."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
        ],
        [
            InlineKeyboardButton("📝 Add Note", callback_data=f"note_{payment_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def update_payment_status(payment_id, status, admin_id, note=None):
    """Update payment status in database."""
    update_data = {
        "status": status,
        "admin_processed_date": datetime.now(),
        "processed_by": admin_id,
    }
    if note:
        update_data["admin_note"] = note
    
    result = payments_collection.update_one(
        {"_id": payment_id},
        {"$set": update_data}
    )
    return result.modified_count > 0

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and instructions."""
    user = update.effective_user
    welcome_msg = (
        f"Hello {user.mention_html()}! Welcome to Movie Hub Premium Payment Bot.\n\n"
        "To activate your premium access, please send me your:\n"
        "1. **Telegram Username** (without @)\n"
        "2. **UPI Transaction ID** (12 digits)\n"
        "3. **Amount Paid** (e.g., `Harmish 123456789012 100`)\n\n"
        "Example: `MyUsername 123456789012 10`\n\n"
    )
    
    if PAYMENT_SCREENSHOT_LINK:
        welcome_msg += f"📌 Please send payment proof here: {PAYMENT_SCREENSHOT_LINK}"
    else:
        welcome_msg += "📌 Make sure to send the payment screenshot to @Mr_HKs after submitting details."
    
    await update.message.reply_html(welcome_msg)

async def handle_payment_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming payment detail messages."""
    text = update.message.text
    user = update.effective_user
    user_telegram_id = user.id

    # Regex to parse the message: Username, 12-digit Txn ID, Amount (integer/float)
    match = re.match(r"(\S+)\s+(\d{12})\s+(\d+(\.\d+)?)", text)

    if not match:
        await update.message.reply_text(
            "❌ Invalid format. Please send your details in this format:\n"
            "`YourUsername 123456789012 10` (Username, Transaction ID, Amount)"
        )
        return

    telegram_username = match.group(1).lower().replace("@", "")  # Normalize username
    txn_id = match.group(2)
    amount_str = match.group(3)

    try:
        amount_paid = float(amount_str)
        if amount_paid < 5:
            await update.message.reply_text("❌ Minimum amount is ₹5. Please pay at least ₹5.")
            return
        if amount_paid > 150:
            await update.message.reply_text("❌ Maximum amount for automatic activation is ₹150. For higher amounts, please contact @Mr_HKs directly.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Please enter a valid number.")
        return

    try:
        # Check for duplicate transaction ID
        if payments_collection.find_one({"txn_id": txn_id}):
            await update.message.reply_text(
                "❌ This Transaction ID has already been submitted.\n"
                "If you believe this is an error, please contact @Mr_HKs."
            )
            logger.warning(f"Duplicate transaction ID submitted: {txn_id} by {telegram_username}")
            await log_to_channel(context, f"⚠️ Duplicate Txn ID: `{txn_id}` submitted by @{telegram_username} (User ID: `{user_telegram_id}`).")
            return

        # Determine premium duration
        premium_duration_string = parse_time_period(amount_paid)

        # Store payment details in MongoDB
        payment_record = {
            "user_telegram_id": user_telegram_id,
            "telegram_username": telegram_username,
            "user_full_name": user.full_name,
            "txn_id": txn_id,
            "amount_paid": amount_paid,
            "premium_duration": premium_duration_string,
            "submission_date": datetime.now(),
            "status": "pending_admin_verification",
            "processed_by_bot": False,
        }
        result = payments_collection.insert_one(payment_record)
        payment_id = result.inserted_id
        logger.info(f"Payment record saved to DB: {payment_record}")

        # Construct the command for auto-filter bot
        add_premium_command = f"/add_premium {user_telegram_id} {premium_duration_string}"

        # Send confirmation to the user
        user_msg = (
            f"✅ Thank you {user.mention_html()}! Your payment details have been received:\n"
            f"👤 Username: @{telegram_username}\n"
            f"💳 Transaction ID: <code>{txn_id}</code>\n"
            f"💰 Amount: ₹{amount_paid}\n"
            f"⏳ Premium Period: {premium_duration_string.replace('days', ' Days').replace('month', ' Month').replace('year', ' Year')}\n\n"
        )
        
        if PAYMENT_SCREENSHOT_LINK:
            user_msg += f"📌 Please send payment proof here: {PAYMENT_SCREENSHOT_LINK}"
        else:
            user_msg += "📌 Please forward your payment screenshot to @Mr_HKs to complete activation."
        
        await update.message.reply_html(user_msg)

        # Prepare admin notification with buttons
        keyboard = await create_payment_buttons(payment_id)
        
        log_message = (
            f"🔔 **New Payment Submitted**\n"
            f"🆔 Payment ID: <code>{payment_id}</code>\n"
            f"👤 User: @{telegram_username} (<code>{user_telegram_id}</code>)\n"
            f"📛 Name: {user.full_name}\n"
            f"💳 Txn ID: <code>{txn_id}</code>\n"
            f"💰 Amount: ₹{amount_paid}\n"
            f"⏳ Period: {premium_duration_string}\n"
            f"📅 Submitted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"🤖 Command for @{AUTO_FILTER_BOT_USERNAME}:\n"
            f"<code>{add_premium_command}</code>"
        )
        
        await log_to_channel(context, log_message, reply_markup=keyboard)

    except pymongo.errors.OperationFailure as e:
        logger.critical(f"MongoDB operation failed: {e}")
        await update.message.reply_text("⚠️ Database error. Please try again later or contact @Mr_HKs.")
        await log_to_channel(context, f"🚨 CRITICAL DB ERROR: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in handle_payment_details: {e}")
        await update.message.reply_text("⚠️ An unexpected error occurred. Please try again later or contact @Mr_HKs.")
        await log_to_channel(context, f"🚨 Unexpected error in payment handling: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button callbacks for payment approval/rejection."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("❌ You are not authorized to perform this action.")
        return
    
    data = query.data
    payment_id = data.split("_")[1]
    
    if data.startswith("approve_"):
        # Handle approval
        payment = payments_collection.find_one({"_id": payment_id})
        if not payment:
            await query.edit_message_text("❌ Payment record not found.")
            return
            
        if payment["status"] != "pending_admin_verification":
            await query.edit_message_text(f"⚠️ Payment is already {payment['status']}.")
            return
            
        # Update status in database
        success = await update_payment_status(payment_id, "approved", user_id)
        if not success:
            await query.edit_message_text("❌ Failed to update payment status.")
            return
            
        # Notify user
        user_msg = (
            f"🎉 Your payment has been approved!\n\n"
            f"🔹 Transaction ID: <code>{payment['txn_id']}</code>\n"
            f"🔹 Amount: ₹{payment['amount_paid']}\n"
            f"🔹 Premium Period: {payment['premium_duration'].replace('days', ' Days').replace('month', ' Month').replace('year', ' Year')}\n\n"
            f"Your premium access should be activated shortly. Thank you!"
        )
        await notify_user(context, payment["user_telegram_id"], user_msg)
        
        # Update admin message
        await query.edit_message_text(
            query.message.text + f"\n\n✅ Approved by admin {query.from_user.mention_html()}",
            parse_mode="HTML"
        )
        
        await log_to_channel(
            context,
            f"✅ Payment approved by {query.from_user.mention_html()} (ID: {user_id})\n"
            f"🆔 Payment ID: <code>{payment_id}</code>\n"
            f"👤 User: @{payment['telegram_username']} (<code>{payment['user_telegram_id']}</code>)",
        )
        
    elif data.startswith("reject_"):
        # Ask for rejection reason
        context.user_data["current_payment_id"] = payment_id
        context.user_data["rejecting_admin_id"] = user_id
        await query.edit_message_text(
            "Please enter the reason for rejecting this payment:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel_{payment_id}")]
            ])
        )
        
    elif data.startswith("note_"):
        # Ask for note to add
        context.user_data["current_payment_id"] = payment_id
        context.user_data["noting_admin_id"] = user_id
        await query.edit_message_text(
            "Please enter your note for this payment:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel_{payment_id}")]
            ])
        )
        
    elif data.startswith("cancel_"):
        # Cancel action
        payment_id = data.split("_")[1]
        payment = payments_collection.find_one({"_id": payment_id})
        if payment:
            await query.edit_message_text(
                f"Action cancelled. Payment remains {payment['status']}.\n\n"
                f"Original message:\n\n{query.message.text}"
            )
        else:
            await query.edit_message_text("Action cancelled.")

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles admin replies for rejection reasons or notes."""
    if update.message.from_user.id not in ADMIN_IDS:
        return
        
    user_data = context.user_data
    text = update.message.text
    
    if "current_payment_id" in user_data:
        payment_id = user_data["current_payment_id"]
        payment = payments_collection.find_one({"_id": payment_id})
        
        if not payment:
            await update.message.reply_text("❌ Payment record not found.")
            return
            
        if "rejecting_admin_id" in user_data:
            # Handle payment rejection
            admin_id = user_data["rejecting_admin_id"]
            success = await update_payment_status(payment_id, "rejected", admin_id, text)
            
            if success:
                # Notify user
                user_msg = (
                    f"⚠️ Your payment has been rejected.\n\n"
                    f"🔹 Transaction ID: <code>{payment['txn_id']}</code>\n"
                    f"🔹 Amount: ₹{payment['amount_paid']}\n"
                    f"🔹 Reason: {text}\n\n"
                    f"Please contact @Mr_HKs if you believe this is an error."
                )
                await notify_user(context, payment["user_telegram_id"], user_msg)
                
                # Log to channel
                await log_to_channel(
                    context,
                    f"❌ Payment rejected by admin (ID: {admin_id})\n"
                    f"🆔 Payment ID: <code>{payment_id}</code>\n"
                    f"👤 User: @{payment['telegram_username']} (<code>{payment['user_telegram_id']}</code>)\n"
                    f"📝 Reason: {text}"
                )
                
                await update.message.reply_text(
                    f"✅ Payment rejected successfully. User has been notified."
                )
            else:
                await update.message.reply_text("❌ Failed to update payment status.")
                
        elif "noting_admin_id" in user_data:
            # Handle adding note
            admin_id = user_data["noting_admin_id"]
            payments_collection.update_one(
                {"_id": payment_id},
                {"$set": {"admin_note": text, "note_added_by": admin_id, "note_added_at": datetime.now()}}
            )
            
            await update.message.reply_text(
                f"✅ Note added to payment record."
            )
            
            await log_to_channel(
                context,
                f"📝 Note added to payment by admin (ID: {admin_id})\n"
                f"🆔 Payment ID: <code>{payment_id}</code>\n"
                f"👤 User: @{payment['telegram_username']} (<code>{payment['user_telegram_id']}</code>)\n"
                f"📝 Note: {text}"
            )
        
        # Clean up user data
        for key in ["current_payment_id", "rejecting_admin_id", "noting_admin_id"]:
            if key in user_data:
                del user_data[key]

async def check_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to check pending payments."""
    if update.message.from_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    status_filter = "pending_admin_verification"
    if context.args and context.args[0].lower() in ["approved", "rejected", "all"]:
        status_filter = context.args[0].lower()
        if status_filter == "all":
            status_filter = None

    query = {}
    if status_filter:
        query["status"] = status_filter

    payments = payments_collection.find(query).sort("submission_date", -1).limit(50)
    
    if status_filter:
        response = f"📊 **{status_filter.capitalize()} Payments:**\n\n"
    else:
        response = "📊 **All Recent Payments:**\n\n"
    
    found = False
    for payment in payments:
        found = True
        status_emoji = "🟢" if payment.get("status") == "approved" else "🔴" if payment.get("status") == "rejected" else "🟡"
        response += (
            f"{status_emoji} <b>Payment ID:</b> <code>{payment.get('_id')}</code>\n"
            f"👤 <b>User:</b> @{payment.get('telegram_username', 'N/A')} (<code>{payment.get('user_telegram_id', 'N/A')}</code>)\n"
            f"💳 <b>Txn ID:</b> <code>{payment.get('txn_id', 'N/A')}</code>\n"
            f"💰 <b>Amount:</b> ₹{payment.get('amount_paid', 'N/A')}\n"
            f"⏳ <b>Period:</b> {payment.get('premium_duration', 'N/A')}\n"
            f"📅 <b>Submitted:</b> {payment.get('submission_date').strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        
        if payment.get("status") != "pending_admin_verification":
            response += (
                f"👨‍💼 <b>Processed by:</b> {payment.get('processed_by', 'N/A')}\n"
                f"⏰ <b>Processed at:</b> {payment.get('admin_processed_date', 'N/A')}\n"
            )
            
        if payment.get("admin_note"):
            response += f"📝 <b>Note:</b> {payment.get('admin_note')}\n"
            
        response += "\n"

    if not found:
        response = f"✅ No {status_filter if status_filter else ''} payments found."

    await update.message.reply_html(response)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a message to the user."""
    logger.error(f"Update {update} caused error {context.error}", exc_info=True)
    
    if update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Oops! Something went wrong. Please try again later or contact @Mr_HKs if the problem persists."
        )
    
    error_msg = (
        f"🚨 Bot Error:\n"
        f"Error: {context.error}\n"
        f"Update: {update}"
    )
    
    try:
        await log_to_channel(context, error_msg)
    except Exception as e:
        logger.error(f"Failed to send error to log channel: {e}")

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check_payments", check_payments))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_details))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_IDS), handle_admin_reply))
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
