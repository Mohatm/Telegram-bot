"""
Telegram Scheduling Bot
Features:
- Schedule Sun-Thu only
- Skips days until 21 Jan 2026 (relative to today)
- Max 10 people per day
- User must upload a document (file/photo)
- Booking is sent to ADMIN_ID for final approval (Approve / Reject)
"""

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
FILES_DIR = os.environ.get('FILES_DIR', 'uploaded_docs')

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError('Please set BOT_TOKEN and ADMIN_ID environment variables')

os.makedirs(FILES_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Conversation states ---
ASK_DATE, ASK_DOC = range(2)

# --- Database helpers ---

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


def get_pending_bookings():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, date, doc_file_id, doc_file_name FROM bookings WHERE status = 'PENDING'")
    rows = cur.fetchall()
    conn.close()
    return rows


def set_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    conn.close()


def get_booking(booking_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, username, date, status, doc_file_id, doc_file_name FROM bookings WHERE id = ?",
        (booking_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row


def user_bookings(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, date, status FROM bookings WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# --- Helpers ---

def is_allowed_weekday(dt):
    # Allowed: Sunday(6) to Thursday(3)
    return dt.weekday() in (6, 0, 1, 2, 3)


def parse_date(text):
    try:
        return datetime.strptime(text.strip(), '%Y-%m-%d')
    except Exception:
        return None


def days_until_start():
    start_date = datetime(2026, 1, 21).date()
    today = datetime.utcnow().date()
    delta = (start_date - today).days
    return max(delta, 0)

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome!\n"
        "Use /schedule to make a booking (Sun–Thu).\n"
        "/mybookings - view your bookings"
    )


async def schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send the booking date in YYYY-MM-DD format.\n"
        "(Sun–Thu only)"
    )
    return ASK_DATE


async def receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dt = parse_date(text)

    if not dt:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return ASK_DATE

    today = datetime.utcnow().date()
    skip_days = days_until_start()
    min_allowed = today + timedelta(days=skip_days)

    if dt.date() < min_allowed:
        await update.message.reply_text(
            f"Scheduling starts on {min_allowed.isoformat()}. Please choose a later date."
        )
        return ASK_DATE

    if not is_allowed_weekday(dt):
        await update.message.reply_text(
            "Invalid day. Bookings are allowed only from Sunday to Thursday."
        )
        return ASK_DATE

    date_str = dt.date().isoformat()
    if count_bookings_for_date(date_str) >= 10:
        await update.message.reply_text(
            "That date is fully booked. Please choose another date."
        )
        return ASK_DATE

    context.user_data['chosen_date'] = date_str
    await update.message.reply_text(
        "Great. Now upload the document (file or photo)."
    )
    return ASK_DOC


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    date_str = context.user_data.get('chosen_date')

    if not date_str:
        await update.message.reply_text("Date missing. Please restart /schedule.")
        return ConversationHandler.END

    file_id = None
    file_name = None

    if update.message.document:
        file_id = update.message.document.file_id
        file_name = update.message.document.file_name
    elif update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        file_name = f'photo_{user.id}_{int(datetime.utcnow().timestamp())}.jpg'
    else:
        await update.message.reply_text("Please upload a file or photo.")
        return ASK_DOC

    booking_id = add_booking(
        user.id, user.username or '', date_str, file_id, file_name
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"approve:{booking_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{booking_id}")
        ]
    ])

    caption = (
        f"New booking #{booking_id}\n"
        f"User: {user.full_name} (@{user.username})\n"
        f"User ID: {user.id}\n"
        f"Date: {date_str}"
    )

    await context.bot.send_message(chat_id=ADMIN_ID, text=caption)
    await context.bot.send_document(
        chat_id=ADMIN_ID,
        document=file_id,
        filename=file_name,
        reply_markup=keyboard
    )

    await update.message.reply_text(
        "Your booking has been submitted and is pending approval."
    )
    return ConversationHandler.END


async def approve_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, booking_id_str = query.data.split(":")
    booking_id = int(booking_id_str)
    booking = get_booking(booking_id)

    if not booking:
        await query.edit_message_text("Booking not found.")
        return

    _, user_id, _, date_str, _, _, _ = booking

    if action == "approve":
        if count_bookings_for_date(date_str) >= 10:
            set_booking_status(booking_id, "REJECTED")
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Your booking #{booking_id} was rejected (date full)."
            )
            await query.edit_message_text("Rejected — date full.")
            return

        set_booking_status(booking_id, "APPROVED")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Your booking #{booking_id} for {date_str} has been APPROVED."
        )
        await query.edit_message_text("APPROVED")

    else:
        set_booking_status(booking_id, "REJECTED")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Your booking #{booking_id} for {date_str} has been REJECTED."
        )
        await query.edit_message_text("REJECTED")


async def mybookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = user_bookings(update.message.from_user.id)
    if not rows:
        await update.message.reply_text("You have no bookings.")
        return

    lines = [f"#{bid} — {date} — {status}" for bid, date, status in rows]
    await update.message.reply_text("\n".join(lines))


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("schedule", schedule_start)],
        states={
            ASK_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date)],
            ASK_DOC: [MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, receive_document)]
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mybookings", mybookings))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(approve_reject_callback))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
