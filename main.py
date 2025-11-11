import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- Configuration ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID') or 0)
DB_PATH = os.environ.get('DB_PATH', 'bookings.db')

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError('Please set BOT_TOKEN and ADMIN_ID environment variables')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_FILE_COUNT, ASK_DATE, ASK_DOC = range(3)

# --- DB Helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            doc_file_id TEXT,
            doc_file_name TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def add_booking(user_id, username, date_str, doc_file_id, doc_file_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO bookings (user_id, username, date, status, doc_file_id, doc_file_name, created_at)
        VALUES (?, ?, ?, 'PENDING', ?, ?, ?)
    ''', (user_id, username, date_str, doc_file_id, doc_file_name, datetime.utcnow().isoformat()))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id

def count_bookings_for_date(date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE date = ? AND status = 'APPROVED'", (date_str,))
    (count,) = cur.fetchone()
    conn.close()
    return count

def user_has_booking_on_date(user_id, date_str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookings WHERE user_id = ? AND date = ?", (user_id, date_str))
    (count,) = cur.fetchone()
    conn.close()
    return count > 0

# --- Helpers ---
def is_allowed_weekday(dt):
    return dt.weekday() in (6, 0, 1, 2, 3)  # Sun=6, Mon=0, ..., Thu=3

def generate_next_two_weeks_buttons():
    buttons = []
    today = datetime.utcnow().date()
    min_allowed = today + timedelta(days=2)
    for i in range(14):
        d = today + timedelta(days=i)
        if d < min_allowed or not is_allowed_weekday(d):
            continue
        buttons.append([InlineKeyboardButton(d.isoformat(), callback_data=f'date:{d.isoformat()}')])
    return InlineKeyboardMarkup(buttons)

# --- Handlers ---
async def set_commands(app):
    await app.bot.set_my_commands([
        ('start', 'Start the bot'),
        ('schedule', 'Make a booking'),
        ('mybookings', 'View your bookings'),
        ('pending', 'View pending bookings (admin only)'),
        ('cancel', 'Cancel current action')
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! Use /schedule to make a booking (Sun–Thu).\nCommands:\n/schedule - start booking\n/mybookings - view your bookings\n/status - view pending bookings (admin)"
    )

async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please select a booking date (only Sun–Thu, at least 2 days in advance):",
        reply_markup=generate_next_two_weeks_buttons()
    )
    return ASK_DATE

async def receive_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    date_str = query.data.split(':')[1]

    user_id = query.from_user.id
    if user_has_booking_on_date(user_id, date_str):
        await query.edit_message_text(f"You already have a booking on {date_str}.")
        return ASK_DATE

    if count_bookings_for_date(date_str) >= 10:
        await query.edit_message_text(f"{date_str} is fully booked. Choose another date.")
        return ASK_DATE

    context.user_data['chosen_date'] = date_str
    await query.edit_message_text(f"Selected date: {date_str}\nNow, please upload your document(s).")
    return ASK_DOC

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    date_str = context.user_data.get('chosen_date')
    if not date_str:
        await update.message.reply_text("Date missing. Please start with /schedule.")
        return ConversationHandler.END

    file_id = None
    file_name = None
    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    elif update.message.photo:
        file = update.message.photo[-1]
        file_id = file.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
    else:
        await update.message.reply_text("Please send a file or photo as the document.")
        return ASK_DOC

    booking_id = add_booking(user.id, user.username or '', date_str, file_id, file_name)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"approve:{booking_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{booking_id}")
        ]
    ])

    caption = f"New booking #{booking_id}\nUser: {user.full_name} (@{user.username})\nDate: {date_str}"

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_id, filename=file_name, reply_markup=keyboard)
    except Exception as e:
        logger.exception("Failed to send to admin: %s", e)
        await update.message.reply_text("Failed to send booking to admin. Please contact support.")
        return ConversationHandler.END

    await update.message.reply_text("Booking submitted. You will be notified after admin approval.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Booking canceled.")
    return ConversationHandler.END

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I did not understand that command.")

# --- Main ---
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(set_commands).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('schedule', schedule_start)],
        states={
            ASK_DATE: [
                CallbackQueryHandler(receive_date_button, pattern=r'^date:'),
                CommandHandler('schedule', schedule_start)
            ],
            ASK_DOC: [
                MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_document),
                CommandHandler('schedule', schedule_start)
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
