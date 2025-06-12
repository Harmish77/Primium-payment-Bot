import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
AUTO_FILTER_BOT_USERNAME = os.getenv('AUTO_FILTER_BOT_USERNAME', 'your_auto_filter_bot_username')
DATABASE_URL = os.getenv('DATABASE_URL')

# Premium durations mapping
DURATION_MAPPING = {
    '3day': '3days',
    '7day': '7days',
    '30day': '1month',
    '90day': '3months',
    '180day': '6months',
    '365day': '1year'
}

class PremiumBot:
    def __init__(self):
        self.updater = Updater(TOKEN, use_context=True)
        self.dp = self.updater.dispatcher
        
        # Add handlers
        self.dp.add_handler(CommandHandler("start", self.start))
        self.dp.add_handler(MessageHandler(Filters.text & ~Filters.command, self.handle_message))
        self.dp.add_handler(CallbackQueryHandler(self.button))
        
        # Error handler
        self.dp.add_error_handler(self.error_handler)
        
        # Initialize database
        self.init_db()

    def init_db(self):
        """Initialize database connection"""
        # For Koyeb, you might want to use their built-in PostgreSQL
        # or connect to an external DB
        try:
            import psycopg2
            self.conn = psycopg2.connect(DATABASE_URL)
            self.create_tables()
        except Exception as e:
            logger.error(f"Database connection error: {e}")
            self.conn = None

    def create_tables(self):
        """Create necessary tables if they don't exist"""
        if not self.conn:
            return
            
        try:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username VARCHAR(255),
                    amount INTEGER NOT NULL,
                    txn_id VARCHAR(50) UNIQUE NOT NULL,
                    duration VARCHAR(20) NOT NULL,
                    payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed BOOLEAN DEFAULT FALSE,
                    processed_date TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS premium_users (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    expiry_date TIMESTAMP NOT NULL,
                    payment_id INTEGER REFERENCES payments(id)
                )
            """)
            self.conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Error creating tables: {e}")
            self.conn.rollback()

    def start(self, update: Update, context: CallbackContext) -> None:
        """Send a message when the command /start is issued."""
        user = update.effective_user
        update.message.reply_text(
            f'Hi {user.first_name}! This bot handles premium subscriptions for Movie Hub.\n\n'
            'If you\'ve made a payment, please send your Telegram username and transaction ID.'
        )

    def handle_message(self, update: Update, context: CallbackContext) -> None:
        """Handle incoming messages with payment details"""
        user_id = update.effective_user.id
        text = update.message.text
        
        # Check if message contains payment details (sent from your payment page)
        if "Transaction ID:" in text and "Amount Paid:" in text:
            self.process_payment_details(update, text)
        elif user_id in ADMIN_IDS:
            self.handle_admin_command(update, text)
        else:
            update.message.reply_text(
                "Please provide your payment details in this format:\n\n"
                "Username: your_username\n"
                "Transaction ID: 123456789012\n"
                "Amount: 25"
            )

    def process_payment_details(self, update: Update, text: str) -> None:
        """Process payment details from the payment page"""
        try:
            # Parse the message (adjust based on your payment page format)
            lines = text.split('\n')
            data = {}
            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    data[key.strip().lower()] = value.strip()
            
            username = data.get('telegram username', '').replace('@', '')
            txn_id = data.get('transaction id', '')
            amount = int(data.get('amount', '0').replace('₹', '').strip())
            
            if not username or not txn_id or amount <= 0:
                update.message.reply_text("Invalid payment details. Please check and try again.")
                return
            
            # Determine duration based on amount
            duration = self.get_duration_from_amount(amount)
            
            # Store payment in database
            if self.conn:
                try:
                    cur = self.conn.cursor()
                    cur.execute("""
                        INSERT INTO payments (user_id, username, amount, txn_id, duration)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                    """, (update.effective_user.id, username, amount, txn_id, duration))
                    payment_id = cur.fetchone()[0]
                    self.conn.commit()
                    
                    # Create keyboard with confirm button
                    keyboard = [
                        [InlineKeyboardButton("✅ Confirm Payment", callback_data=f"confirm_{payment_id}")],
                        [InlineKeyboardButton("❌ Reject Payment", callback_data=f"reject_{payment_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # Notify admin
                    for admin_id in ADMIN_IDS:
                        context.bot.send_message(
                            admin_id,
                            f"New payment received:\n\n"
                            f"User: @{username}\n"
                            f"Amount: ₹{amount}\n"
                            f"Duration: {duration}\n"
                            f"TXN ID: {txn_id}",
                            reply_markup=reply_markup
                        )
                    
                    update.message.reply_text(
                        "Thank you for your payment! Your premium access is being processed. "
                        "You'll receive a confirmation shortly."
                    )
                    
                except Exception as e:
                    logger.error(f"Database error: {e}")
                    update.message.reply_text("An error occurred. Please contact support.")
                    self.conn.rollback()
            else:
                update.message.reply_text("Database connection error. Please contact support.")
            
        except Exception as e:
            logger.error(f"Error processing payment: {e}")
            update.message.reply_text("Invalid payment details format. Please contact support.")

    def get_duration_from_amount(self, amount: int) -> str:
        """Map payment amount to duration string"""
        if amount == 5:
            return "3day"
        elif amount == 10:
            return "7day"
        elif amount == 25:
            return "30day"
        elif amount == 60:
            return "90day"
        elif amount == 100:
            return "180day"
        elif amount == 150:
            return "365day"
        else:
            # For custom amounts, default to days based on amount/5 ratio
            days = max(1, amount // 5)
            return f"{days}day"

    def button(self, update: Update, context: CallbackContext) -> None:
        """Handle button callbacks"""
        query = update.callback_query
        query.answer()
        
        if query.data.startswith('confirm_'):
            payment_id = int(query.data.split('_')[1])
            self.confirm_payment(update, context, payment_id)
        elif query.data.startswith('reject_'):
            payment_id = int(query.data.split('_')[1])
            self.reject_payment(update, context, payment_id)

    def confirm_payment(self, update: Update, context: CallbackContext, payment_id: int) -> None:
        """Confirm payment and add premium"""
        if not self.conn:
            query = update.callback_query
            query.edit_message_text("Database connection error. Cannot process payment.")
            return
            
        try:
            cur = self.conn.cursor()
            
            # Get payment details
            cur.execute("""
                SELECT user_id, username, duration FROM payments 
                WHERE id = %s AND processed = FALSE
            """, (payment_id,))
            payment = cur.fetchone()
            
            if not payment:
                query = update.callback_query
                query.edit_message_text("Payment not found or already processed.")
                return
                
            user_id, username, duration = payment
            
            # Calculate expiry date
            expiry_date = self.calculate_expiry_date(duration)
            
            # Add to premium users
            cur.execute("""
                INSERT INTO premium_users (user_id, username, expiry_date, payment_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET expiry_date = EXCLUDED.expiry_date,
                    payment_id = EXCLUDED.payment_id
            """, (user_id, username, expiry_date, payment_id))
            
            # Mark payment as processed
            cur.execute("""
                UPDATE payments SET processed = TRUE, processed_date = NOW()
                WHERE id = %s
            """, (payment_id,))
            
            self.conn.commit()
            
            # Send /add_premium command to auto filter bot
            duration_cmd = DURATION_MAPPING.get(duration, duration)
            context.bot.send_message(
                AUTO_FILTER_BOT_USERNAME,
                f"/add_premium {user_id} {duration_cmd}"
            )
            
            # Notify user
            context.bot.send_message(
                user_id,
                f"🎉 Your premium access has been activated for {duration_cmd}!\n\n"
                "Thank you for subscribing to Movie Hub Premium."
            )
            
            # Update admin message
            query = update.callback_query
            query.edit_message_text(
                f"✅ Payment confirmed and premium access granted to @{username} for {duration_cmd}."
            )
            
        except Exception as e:
            logger.error(f"Error confirming payment: {e}")
            query = update.callback_query
            query.edit_message_text("Error processing payment. Please try again.")
            self.conn.rollback()

    def calculate_expiry_date(self, duration: str) -> datetime:
        """Calculate expiry date from duration string"""
        now = datetime.now()
        
        if duration.endswith('day'):
            days = int(duration[:-3])
            return now + timedelta(days=days)
        elif duration.endswith('month'):
            months = int(duration[:-5])
            return now + timedelta(days=months*30)
        elif duration.endswith('year'):
            years = int(duration[:-4])
            return now + timedelta(days=years*365)
        else:
            # Default to 1 month if format not recognized
            return now + timedelta(days=30)

    def reject_payment(self, update: Update, context: CallbackContext, payment_id: int) -> None:
        """Reject a payment"""
        if not self.conn:
            query = update.callback_query
            query.edit_message_text("Database connection error. Cannot process payment.")
            return
            
        try:
            cur = self.conn.cursor()
            
            # Get payment details
            cur.execute("""
                SELECT user_id, username FROM payments 
                WHERE id = %s AND processed = FALSE
            """, (payment_id,))
            payment = cur.fetchone()
            
            if not payment:
                query = update.callback_query
                query.edit_message_text("Payment not found or already processed.")
                return
                
            user_id, username = payment
            
            # Delete payment record
            cur.execute("""
                DELETE FROM payments WHERE id = %s
            """, (payment_id,))
            
            self.conn.commit()
            
            # Notify user
            context.bot.send_message(
                user_id,
                "❌ Your payment was rejected by the admin. Please contact support if you believe this is an error."
            )
            
            # Update admin message
            query = update.callback_query
            query.edit_message_text(f"❌ Payment from @{username} rejected and deleted.")
            
        except Exception as e:
            logger.error(f"Error rejecting payment: {e}")
            query = update.callback_query
            query.edit_message_text("Error rejecting payment. Please try again.")
            self.conn.rollback()

    def handle_admin_command(self, update: Update, text: str) -> None:
        """Handle admin commands"""
        if text.startswith('/addpremium'):
            parts = text.split()
            if len(parts) == 3:
                try:
                    user_id = int(parts[1])
                    duration = parts[2]
                    
                    # Send command to auto filter bot
                    update.message.reply_text(
                        f"Forwarding to @{AUTO_FILTER_BOT_USERNAME}: /add_premium {user_id} {duration}"
                    )
                    update.message.bot.send_message(
                        AUTO_FILTER_BOT_USERNAME,
                        f"/add_premium {user_id} {duration}"
                    )
                except ValueError:
                    update.message.reply_text("Invalid user ID. Must be a number.")
            else:
                update.message.reply_text("Usage: /addpremium <user_id> <duration>")

    def error_handler(self, update: Update, context: CallbackContext) -> None:
        """Log errors"""
        logger.error(msg="Exception while handling update:", exc_info=context.error)

    def run(self):
        """Run the bot"""
        self.updater.start_polling()
        self.updater.idle()

if __name__ == '__main__':
    bot = PremiumBot()
    bot.run()
