import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from pymongo import MongoClient
import re

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
YOUR_TELEGRAM_USER_ID = int(os.getenv("YOUR_TELEGRAM_USER_ID"))  # Convert to int
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))  # Convert to int
AUTO_FILTER_BOT_USERNAME = os.getenv("AUTO_FILTER_BOT_USERNAME") # Your auto-filter bot's username (without @)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- MongoDB Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client.get_database()  # Gets the default database specified in the URI
    payments_collection = db["payments"]
    users_collection = db["users"] # Optional: to store general user info if needed
    logger.info("MongoDB connected successfully.")
except Exception as e:
    logger.error(f"Error connecting to MongoDB: {e}")
    # Exit or handle gracefully if DB connection is critical

# --- Helper Functions ---

def parse_time_period(amount):
    """
    Interpolates the time period in days based on the amount paid,
    using the provided tier structure from your JS.
    Returns a string like '3days', '1month', '1year', etc.
    """
    tiers = [
        {"amount": 5, "days": 3},
        {"amount": 10, "days": 7},
        {"amount": 25, "days": 30},
        {"amount": 60, "days": 90},
        {"amount": 100, "days": 180},
        {"amount": 150, "days": 365},
    ]

    amount = max(5, min(150, amount)) # Clamp amount between 5 and 150

    exact_tier = next((t for t in tiers if t["amount"] == amount), None)
    if exact_tier:
        total_days = exact_tier["days"]
    else:
        # Handle amounts below first tier
        if amount < tiers[0]["amount"]:
            total_days = max(1, round((amount / tiers[0]["amount"]) * tiers[0]["days"]))
        # Handle amounts above last tier (simple linear extrapolation)
        elif amount > tiers[-1]["amount"]:
            total_days = round((amount / tiers[-1]["amount"]) * tiers[-1]["days"])
        else:
            # Interpolate for amounts within tiers
            lower_tier, upper_tier = None, None
            for i in range(len(tiers) - 1):
                if amount >= tiers[i]["amount"] and amount <= tiers[i+1]["amount"]:
                    lower_tier = tiers[i]
                    upper_tier = tiers[i+1]
                    break
            
            if lower_tier and upper_tier:
                # Calculate proportional days based on tier rates
                # This is a simplification; a more precise interpolation
                # would match your JavaScript's `effectiveRate` calculation.
                # For simplicity, we'll do linear interpolation here.
                ratio = (amount - lower_tier["amount"]) / (upper_tier["amount"] - lower_tier["amount"])
                total_days = round(lower_tier["days"] + ratio * (upper_tier["days"] - lower_tier["days"]))
            else:
                total_days = 0 # Should not happen with clamping

    # Convert total_days into optimal 'Xdays', 'Xmonth', 'Xyear' format for the bot
    if total_days >= 365 and total_days % 365 == 0:
        return f"{total_days // 365}year"
    elif total_days >= 30 and total_days % 30 == 0:
        return f"{total_days // 30}month"
    elif total_days > 0:
        return f"{total_days}days"
    else:
        return "1day" # Default to 1 day for very small or zero amounts

async def log_to_channel(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Sends a message to the designated log channel."""
    try:
        await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=message)
        logger.info(f"Logged to channel: {message}")
    except Exception as e:
        logger.error(f"Failed to send log to channel: {e}")

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and instructions."""
    await update.message.reply_text(
        "Hello! Welcome to Movie Hub Premium Payment Bot.\n\n"
        "To activate your premium access, please send me your:\n"
        "1. **Telegram Username** (without @)\n"
        "2. **UPI Transaction ID** (12 digits)\n"
        "3. **Amount Paid** (e.g., `Harmish 123456789012 100`)\n\n"
        "Example: `MyUsername 123456789012 10`\n\n"
        "Make sure to send the payment screenshot to @Mr_HKs on Telegram after sending details here."
    )

async def handle_payment_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming payment detail messages."""
    text = update.message.text
    user_telegram_id = update.message.from_user.id

    # Regex to parse the message: Username, 12-digit Txn ID, Amount (integer/float)
    match = re.match(r"(\S+)\s+(\d{12})\s+(\d+(\.\d+)?)", text)

    if not match:
        await update.message.reply_text(
            "❌ Invalid format. Please send your details in this format:\n"
            "`YourUsername 123456789012 10` (Username, Transaction ID, Amount)"
        )
        return

    telegram_username = match.group(1)
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

    # Check if this transaction ID has already been processed
    if payments_collection.find_one({"txn_id": txn_id}):
        await update.message.reply_text(
            "Looks like this Transaction ID has already been submitted or processed. "
            "If you believe this is an error, please contact @Mr_HKs."
        )
        logger.warning(f"Duplicate transaction ID submitted: {txn_id} by {telegram_username}")
        await log_to_channel(context, f"⚠️ Duplicate Txn ID: `{txn_id}` submitted by @{telegram_username} (User ID: `{user_telegram_id}`).")
        return

    # Determine premium duration
    premium_duration_string = parse_time_period(amount_paid) # e.g., "3days", "1month", "1year"

    # Store payment details in MongoDB
    payment_record = {
        "user_telegram_id": user_telegram_id,
        "telegram_username": telegram_username,
        "txn_id": txn_id,
        "amount_paid": amount_paid,
        "premium_duration": premium_duration_string,
        "submission_date": datetime.now(),
        "status": "pending_admin_verification",
        "processed_by_bot": False, # Flag to indicate if the bot has sent the /add_premium command
    }
    payments_collection.insert_one(payment_record)
    logger.info(f"Payment record saved to DB: {payment_record}")

    # Prepare command for auto-filter bot
    # Note: Your auto-filter bot needs to be able to receive messages from THIS bot.
    # It might be easier to have this bot just send a message to you (admin)
    # and you manually forward it to the auto-filter bot if direct bot-to-bot
    # command execution isn't set up.
    # However, for full automation, this bot should send the command.

    # Option 1: Send the command directly to the auto-filter bot
    # This requires the auto-filter bot to recognize commands from this bot.
    # It's generally safer if this bot directly interacts with the admin or sends a webhook.
    # For a simple setup, if your auto-filter bot is public or in a shared group, this might work.
    
    # We will simulate sending to the auto-filter bot by sending it to the log channel.
    # In a real scenario, this command needs to be sent to the auto-filter bot's chat_id
    # or a group where both bots are present and the command is listened for.

    # For automation, you'd typically send a message to a group where the auto-filter bot is.
    # Or, the auto-filter bot has an API you can call. Since you described it as a Telegram bot,
    # sending a message to it directly might work if it processes commands from other bots.
    
    # Construct the command for your auto-filter bot
    add_premium_command = f"/add_premium {user_telegram_id} {premium_duration_string}"
    
    # Send confirmation to the user
    await update.message.reply_text(
        f"✅ Thank you! Your payment details have been received:\n"
        f"Username: `@`{telegram_username}\n"
        f"Transaction ID: `{txn_id}`\n"
        f"Amount: ₹{amount_paid}\n"
        f"Premium Period: {premium_duration_string.replace('days', ' Days').replace('month', ' Month').replace('year', ' Year')}\n\n"
        f"Please forward your payment screenshot to @Mr_HKs to complete the activation process."
    )

    # Log to admin channel for verification and action
    log_message = (
        f"🔔 **New Payment Submitted!**\n"
        f"👤 User: @{telegram_username} (ID: `{user_telegram_id}`)\n"
        f"💳 Txn ID: `{txn_id}`\n"
        f"💰 Amount: `₹{amount_paid}`\n"
        f"⏳ Period: `{premium_duration_string}`\n"
        f"🤖 **Action Needed:** Send this command to `{AUTO_FILTER_BOT_USERNAME}`: \n"
        f"`{add_premium_command}`"
    )
    await log_to_channel(context, log_message)

    # Mark as processed by bot in DB if direct command sent successfully or if you trust manual process
    # If this bot is meant to directly trigger the auto-filter bot, then upon successful triggering,
    # you'd update the database:
    payments_collection.update_one(
        {"txn_id": txn_id},
        {"$set": {"processed_by_bot": True, "processed_date": datetime.now()}}
    )
    logger.info(f"Command prepared for auto-filter bot for Txn ID {txn_id}: {add_premium_command}")


async def check_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to check pending payments."""
    if update.message.from_user.id != YOUR_TELEGRAM_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    pending_payments = payments_collection.find({"status": "pending_admin_verification"})
    
    response = "📊 **Pending Payments:**\n\n"
    found = False
    for payment in pending_payments:
        found = True
        response += (
            f"👤 User: @{payment.get('telegram_username', 'N/A')} (ID: `{payment.get('user_telegram_id', 'N/A')}`)\n"
            f"💳 Txn ID: `{payment.get('txn_id', 'N/A')}`\n"
            f"💰 Amount: `₹{payment.get('amount_paid', 'N/A')}`\n"
            f"⏳ Period: `{payment.get('premium_duration', 'N/A')}`\n"
            f"⏰ Submitted: `{payment.get('submission_date').strftime('%Y-%m-%d %H:%M:%S')}`\n"
            f"Command: `/add_premium {payment.get('user_telegram_id', 'N/A')} {payment.get('premium_duration', 'N/A')}`\n\n"
        )
    
    if not found:
        response = "✅ No pending payments found."

    await update.message.reply_text(response, parse_mode="Markdown")


async def mark_processed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to manually mark a payment as processed."""
    if update.message.from_user.id != YOUR_TELEGRAM_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return
    
    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text("Usage: `/mark_processed <transaction_id>`")
        return
    
    txn_id_to_mark = args[0]
    result = payments_collection.update_one(
        {"txn_id": txn_id_to_mark},
        {"$set": {"status": "processed", "admin_processed_date": datetime.now()}}
    )
    
    if result.modified_count > 0:
        await update.message.reply_text(f"✅ Transaction ID `{txn_id_to_mark}` marked as processed.")
        await log_to_channel(context, f"✅ Admin (`{update.message.from_user.id}`) marked Txn ID `{txn_id_to_mark}` as processed.")
    else:
        await update.message.reply_text(f"❌ Transaction ID `{txn_id_to_mark}` not found or already processed.")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a message to the user."""
    logger.error(f"Update {update} caused error {context.error}")
    if update.effective_message:
        await update.effective_message.reply_text(
            "Oops! Something went wrong. Please try again later or contact @Mr_HKs if the problem persists."
        )
    await log_to_channel(context, f"🚨 Bot Error: `{context.error}`\nUpdate: `{update}`")


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check_payments", check_payments))
    application.add_handler(CommandHandler("mark_processed", mark_processed))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_details)
    )

    # Error handler
    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

