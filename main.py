import os
import logging
from datetime import datetime
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

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "").split(",") if id]
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
AUTO_FILTER_BOT_USERNAME = os.getenv("AUTO_FILTER_BOT_USERNAME")

# --- Database Setup ---
client = MongoClient(MONGO_URI)
db = client[os.getenv("MONGO_DB_NAME", "moviehub")]
payments = db["payments"]

# --- Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class PaymentBot:
    @staticmethod
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "💰 Send your payment details:\n"
            "Format: `username transaction_id amount`\n\n"
            "Example: `john_doe 123456789012 50`"
        )

    @staticmethod
    async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        text = update.message.text.strip()

        # Validate input
        if not re.match(r"^\w+\s+\d{12}\s+\d+$", text):
            await update.message.reply_text(
                "❌ Invalid format. Use:\n"
                "`username 12digit_transaction_id amount`"
            )
            return

        username, txn_id, amount = text.split()
        
        # Check duplicate
        if payments.find_one({"txn_id": txn_id}):
            await update.message.reply_text("⚠️ This transaction was already submitted")
            return

        # Save payment
        payment_id = payments.insert_one({
            "user_id": user.id,
            "username": username,
            "txn_id": txn_id,
            "amount": int(amount),
            "status": "pending",
            "date": datetime.now()
        }).inserted_id

        # User confirmation
        await update.message.reply_text("✅ Received! Admin will verify shortly.")

        # Admin approval buttons
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}")
        ]])

        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=(
                f"🆕 Payment Submission\n\n"
                f"👤 @{username} ({user.id})\n"
                f"💳 `{txn_id}`\n"
                f"💰 ₹{amount}\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            ),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    @staticmethod
    async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        # Verify admin
        if query.from_user.id not in ADMIN_IDS:
            await query.edit_message_text("❌ Access denied")
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
                "processed_by": query.from_user.id,
                "processed_at": datetime.now()
            }}
        )

        # Notify user
        await context.bot.send_message(
            chat_id=payment["user_id"],
            text=(
                f"🔔 Payment {new_status}!\n\n"
                f"Txn ID: `{payment['txn_id']}`\n"
                f"Amount: ₹{payment['amount']}\n\n"
                f"{'✅ Access will be activated soon' if new_status == 'approved' else '❌ Contact admin for help'}"
            ),
            parse_mode="Markdown"
        )

        # Update admin message
        await query.edit_message_text(
            f"{query.message.text}\n\n"
            f"{'✅ Approved' if new_status == 'approved' else '❌ Rejected'} by admin",
            parse_mode="Markdown"
        )

        # Log to channel
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=(
                f"Payment {new_status}\n\n"
                f"User: @{payment['username']}\n"
                f"Txn: `{payment['txn_id']}`\n"
                f"Amount: ₹{payment['amount']}\n"
                f"Admin: {query.from_user.id}"
            ),
            parse_mode="Markdown"
        )

        # If approved, send activation command
        if new_status == "approved":
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"Run this command:\n/add_premium {payment['user_id']} 30days",
                parse_mode="Markdown"
            )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", PaymentBot.start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, PaymentBot.handle_payment))
    app.add_handler(CallbackQueryHandler(PaymentBot.handle_decision, pattern=r"^(approve|reject)_"))
    
    # Error handling
    app.add_error_handler(lambda u, c: logger.error(c.error) if c.error else None)
    
    logger.info("Payment bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
