import os
import logging
import pymongo.errors
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
AUTO_FILTER_BOT_USERNAME = os.getenv("AUTO_FILTER_BOT_USERNAME")

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# MongoDB setup
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db = client[os.getenv("MONGO_DB_NAME", "moviehub")]
    payments = db["payments"]
    logger.info("Connected to MongoDB")
except Exception as e:
    logger.critical(f"MongoDB connection failed: {e}")
    exit(1)

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Send message to log channel"""
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to log to channel: {e}")

async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
    """Send notification to user"""
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to notify user {user_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message"""
    await update.message.reply_text(
        "📌 Send your payment details in this format:\n"
        "`username upi_transaction_id amount`\n\n"
        "Example: `john_doe 123456789012 50`\n\n"
        "After submission, admin will verify your payment."
    )

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process payment details from user"""
    user = update.effective_user
    text = update.message.text.strip()
    
    # Validate input format
    if not re.match(r"^\w+\s+\d{12}\s+\d+$", text):
        await update.message.reply_text(
            "❌ Invalid format. Use:\n"
            "`username transaction_id amount`\n\n"
            "Example: `john_doe 123456789012 50`"
        )
        return
    
    username, txn_id, amount = text.split()
    
    # Check for duplicate transaction
    if payments.find_one({"txn_id": txn_id}):
        await update.message.reply_text(
            "⚠️ This transaction ID was already submitted.\n"
            "Contact admin if this is an error."
        )
        return
    
    # Save to database
    payment_data = {
        "user_id": user.id,
        "username": username,
        "txn_id": txn_id,
        "amount": int(amount),
        "status": "pending",
        "submitted_at": datetime.now(),
    }
    payment_id = payments.insert_one(payment_data).inserted_id
    
    # Notify user
    await update.message.reply_text(
        "✅ Payment details received!\n"
        "Admin will verify shortly."
    )
    
    # Create approval buttons for admin
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
        ]
    ])
    
    # Send to admin channel
    admin_msg = (
        f"🆕 Payment Submission\n\n"
        f"👤 User: @{username} ({user.id})\n"
        f"💳 Txn ID: `{txn_id}`\n"
        f"💰 Amount: ₹{amount}\n"
        f"⏰ Submitted: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    
    await context.bot.send_message(
        chat_id=LOG_CHANNEL_ID,
        text=admin_msg,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process admin approval/rejection"""
    query = update.callback_query
    await query.answer()
    
    # Verify admin
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized")
        return
    
    action, payment_id = query.data.split("_")
    payment = payments.find_one({"_id": payment_id})
    
    if not payment:
        await query.edit_message_text("⚠️ Payment not found")
        return
    
    # Update status
    new_status = "approved" if action == "approve" else "rejected"
    payments.update_one(
        {"_id": payment_id},
        {"$set": {
            "status": new_status,
            "processed_at": datetime.now(),
            "processed_by": query.from_user.id
        }}
    )
    
    # Notify user
    user_msg = (
        f"🔔 Payment Update\n\n"
        f"Status: {'Approved ✅' if new_status == 'approved' else 'Rejected ❌'}\n"
        f"Txn ID: `{payment['txn_id']}`\n"
        f"Amount: ₹{payment['amount']}\n\n"
    )
    
    if new_status == "approved":
        user_msg += "Your premium access will be activated shortly!"
    else:
        user_msg += "Contact admin if you have questions."
    
    await notify_user(context, payment["user_id"], user_msg)
    
    # Update admin message
    status_emoji = "✅" if new_status == "approved" else "❌"
    await query.edit_message_text(
        query.message.text + f"\n\n{status_emoji} {new_status.capitalize()} by admin",
        parse_mode="Markdown"
    )
    
    # Log final status
    log_msg = (
        f"Payment {new_status}\n\n"
        f"User: @{payment['username']} ({payment['user_id']})\n"
        f"Txn ID: `{payment['txn_id']}`\n"
        f"Amount: ₹{payment['amount']}\n"
        f"Admin: {query.from_user.id}"
    )
    await log_to_channel(context, log_msg)

def main():
    """Start the bot"""
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment))
    app.add_handler(CallbackQueryHandler(handle_approval, pattern=r"^(approve|reject)_"))
    
    # Error handler
    app.add_error_handler(lambda u, c: logger.error(c.error) if c.error else None)
    
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
