import os
import sqlite3
import time
from datetime import date
from dotenv import load_dotenv
from telegram import LabeledPrice
from telegram.ext import PreCheckoutQueryHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ================== ENV ==================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("❌ Не заданы TELEGRAM_TOKEN или OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ================== CONFIG ==================

DB_PATH = "/app/data/dialogs.db"
MAX_HISTORY = 30
FREE_DAILY_LIMIT = 20
SUMMARY_TRIGGER = 10
SUBSCRIPTION_DAYS = 30

SHORT_GAP = 3 * 24 * 60 * 60
LONG_GAP = 14 * 24 * 60 * 60

# ================== PROMPTS ==================

SYSTEM_PROMPT = (
    "Ты — поддерживающий психологический ассистент.\n"
    "Ты отвечаешь мягко, спокойно и рационально.\n"
    "Ты помогаешь человеку разобраться в своих чувствах и мыслях.\n"
    "Ты не ставишь диагнозы и не даёшь медицинских или юридических советов.\n"
    "Ты не осуждаешь и не обесцениваешь чувства.\n"
    "Ты можешь задавать аккуратные уточняющие вопросы.\n"
    "Если тема кажется серьёзной или кризисной, мягко рекомендуй обратиться к специалисту."
)

SUMMARY_PROMPT = (
    "Сделай краткое, бережное резюме диалога.\n"
    "Опиши чувства и темы без диагнозов и советов.\n"
    "3–5 предложений."
)

SUBSCRIPTION_PRICE = 99900   # 999 ₽ в копейках
CURRENCY = "RUB"

PRICING_TEXT = (
    "Подписка на психологический ИИ-ассистент\n\n"
    "Стоимость: $9.99 ₽ за 30 дней\n\n"
    "Подписка даёт доступ к общению без дневных ограничений "
    "и позволяет сохранять длительную историю диалога.\n\n"
    "Подписка является необязательной.\n"
    "Базовый функционал доступен бесплатно.\n\n"
    "Это не медицинская и не психотерапевтическая услуга."
)

# ================== DB ==================

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS messages (
    user_id INTEGER,
    role TEXT,
    content TEXT,
    ts INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS summaries (
    user_id INTEGER PRIMARY KEY,
    content TEXT,
    ts INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER,
    date TEXT,
    count INTEGER,
    PRIMARY KEY (user_id, date)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER
)
""")

conn.commit()

# ================== HELPERS ==================

def save_message(user_id, role, content):
    cursor.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?)",
        (user_id, role, content, int(time.time()))
    )
    conn.commit()

def load_history(user_id, limit):
    cursor.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def has_history(user_id):
    cursor.execute("SELECT 1 FROM messages WHERE user_id=? LIMIT 1", (user_id,))
    return cursor.fetchone() is not None

def last_user_ts(user_id):
    cursor.execute(
        "SELECT ts FROM messages WHERE user_id=? AND role='user' ORDER BY ts DESC LIMIT 1",
        (user_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None

def count_user_messages(user_id):
    cursor.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id=? AND role='user'",
        (user_id,)
    )
    return cursor.fetchone()[0]

def get_summary(user_id):
    cursor.execute("SELECT content FROM summaries WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def save_summary(user_id, content):
    cursor.execute(
        """
        INSERT INTO summaries VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET content=excluded.content, ts=excluded.ts
        """,
        (user_id, content, int(time.time()))
    )
    conn.commit()

def generate_summary(user_id):
    history = load_history(user_id, MAX_HISTORY)
    messages = [{"role": "system", "content": SUMMARY_PROMPT}] + history
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.4
    )
    save_summary(user_id, r.choices[0].message.content.strip())

def today():
    return date.today().isoformat()

def get_usage(user_id):
    cursor.execute(
        "SELECT count FROM usage WHERE user_id=? AND date=?",
        (user_id, today())
    )
    row = cursor.fetchone()
    return row[0] if row else 0

def inc_usage(user_id):
    cursor.execute(
        """
        INSERT INTO usage VALUES (?, ?, 1)
        ON CONFLICT(user_id, date)
        DO UPDATE SET count = count + 1
        """,
        (user_id, today())
    )
    conn.commit()

# ================== UI ==================

def subscribe_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🟢 Оформить подписку", callback_data="subscribe_start")]]
    )

def activate_subscription(user_id: int):
    expires_at = int(time.time()) + SUBSCRIPTION_DAYS * 86400
    cursor.execute(
        """
        INSERT INTO subscriptions (user_id, expires_at)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET expires_at=excluded.expires_at
        """,
        (user_id, expires_at)
    )
    conn.commit()

# ================== HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not has_history(uid):
        text = (
            "Здравствуйте.\n\n"
            "Здесь можно писать так, как вам сейчас получается.\n"
            "Я постараюсь быть рядом и помочь разобраться.\n\n"
            "С чего бы вы хотели начать?"
        )
    else:
        gap = time.time() - (last_user_ts(uid) or time.time())
        if gap > LONG_GAP:
            text = (
                "Прошло некоторое время.\n\n"
                "Если хотите — можем начать заново или продолжить."
            )
        else:
            text = (
                "Рада снова быть на связи.\n\n"
                "Можете продолжить с того места, где остановились."
            )

    await update.message.reply_text(text)
    

async def pricing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRICING_TEXT, reply_markup=subscribe_keyboard())

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prices = [LabeledPrice("Подписка на 30 дней", SUBSCRIPTION_PRICE)]

    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Подписка на психологический ИИ-ассистент",
        description="Доступ без дневных ограничений на 30 дней.",
        payload="subscription_30_days",
        provider_token=os.getenv("PAYMENT_PROVIDER_TOKEN"),
        currency=CURRENCY,
        prices=prices,
    )

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "Чтобы оформить подписку, пожалуйста, напишите команду:\n\n"
        "/subscribe"
    )


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    activate_subscription(user_id)

    await update.message.reply_text(
        "Спасибо за оплату 💚\n\n"
        "Подписка активирована на 30 дней.\n"
        "Вы можете продолжать общение без ограничений."
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not has_history(uid):
        await update.message.reply_text("Пока нет диалога для резюме.")
        return

    if not get_summary(uid):
        generate_summary(uid)

    await update.message.reply_text(get_summary(uid))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM messages WHERE role='user'")
    total = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(DISTINCT user_id) FROM messages "
        "WHERE role='user' AND ts >= strftime('%s','now','start of day')"
    )
    today_users = cursor.fetchone()[0]

    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"📆 Активных сегодня: {today_users}"
    )

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    if get_usage(uid) >= FREE_DAILY_LIMIT:
        await update.message.reply_text(
            "На сегодня бесплатный лимит исчерпан.\n"
            "Можно оформить подписку или продолжить завтра."
        )
        return

    save_message(uid, "user", text)
    inc_usage(uid)

    if count_user_messages(uid) % SUMMARY_TRIGGER == 0:
        try:
            generate_summary(uid)
        except Exception:
            pass

    history = load_history(uid, MAX_HISTORY)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.6
    )

    answer = r.choices[0].message.content
    save_message(uid, "assistant", answer)
    await update.message.reply_text(answer)

# ================== RUN ==================

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("pricing", pricing_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("summary", summary_command))
app.add_handler(CommandHandler("stats", stats_command))
app.add_handler(CallbackQueryHandler(subscribe_callback, pattern="^subscribe_start$"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

print("🧠 Бот успешно запущен")
app.run_polling()










