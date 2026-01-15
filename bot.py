import os
import sqlite3
import time
from datetime import date
from dotenv import load_dotenv

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

# ================== НАСТРОЙКИ ==================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not ADMIN_ID:
    raise RuntimeError("❌ Не заданы TELEGRAM_TOKEN / OPENAI_API_KEY / ADMIN_ID")

client = OpenAI(api_key=OPENAI_API_KEY)

MAX_HISTORY = 30
SUMMARY_TRIGGER = 10
FREE_DAILY_LIMIT = 20
SUBSCRIPTION_DAYS = 30

DB_PATH = "/app/data/dialogs.db"

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
    "Сделай краткое, бережное резюме диалога с точки зрения психолога.\n"
    "Опиши, что происходит с человеком, какие чувства и темы проявляются.\n"
    "Без диагнозов, интерпретаций и советов.\n"
    "Нейтрально, спокойно, в 3–5 предложениях."
)

PRICING_TEXT = (
    "Подписка на психологический ИИ-ассистент\n\n"
    "Стоимость: 999 ₽ за 30 дней\n\n"
    "Подписка даёт доступ к общению без дневных ограничений "
    "и позволяет сохранять длительную историю диалога.\n\n"
    "Подписка является необязательной.\n"
    "Базовый функционал доступен бесплатно.\n\n"
    "Это не медицинская и не психотерапевтическая услуга."
)

# ================== SQLITE ==================

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

# ================== DB HELPERS ==================

def save_message(user_id: int, role: str, content: str):
    cursor.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?)",
        (user_id, role, content, int(time.time()))
    )
    conn.commit()


def load_last_messages(user_id: int, limit: int):
    cursor.execute(
        """
        SELECT role, content FROM messages
        WHERE user_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (user_id, limit)
    )
    rows = cursor.fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def has_history(user_id: int) -> bool:
    cursor.execute(
        "SELECT 1 FROM messages WHERE user_id = ? LIMIT 1",
        (user_id,)
    )
    return cursor.fetchone() is not None


def get_last_user_ts(user_id: int):
    cursor.execute(
        """
        SELECT ts FROM messages
        WHERE user_id = ? AND role = 'user'
        ORDER BY ts DESC
        LIMIT 1
        """,
        (user_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def count_user_messages(user_id: int) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user'",
        (user_id,)
    )
    return cursor.fetchone()[0]


def get_summary(user_id: int):
    cursor.execute(
        "SELECT content FROM summaries WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def save_summary(user_id: int, content: str):
    cursor.execute(
        """
        INSERT INTO summaries (user_id, content, ts)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET content=excluded.content, ts=excluded.ts
        """,
        (user_id, content, int(time.time()))
    )
    conn.commit()


def generate_summary(user_id: int):
    history = load_last_messages(user_id, MAX_HISTORY)
    messages = [{"role": "system", "content": SUMMARY_PROMPT}, *history]

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.4
    )

    save_summary(user_id, response.choices[0].message.content.strip())

# ================== STATS ==================

def stats_total_users():
    cursor.execute(
        "SELECT COUNT(DISTINCT user_id) FROM messages WHERE role='user'"
    )
    return cursor.fetchone()[0]


def stats_today_users():
    cursor.execute(
        """
        SELECT COUNT(DISTINCT user_id)
        FROM messages
        WHERE role='user'
        AND ts >= strftime('%s','now','start of day')
        """
    )
    return cursor.fetchone()[0]


def stats_week_users():
    cursor.execute(
        """
        SELECT COUNT(DISTINCT user_id)
        FROM messages
        WHERE role='user'
        AND ts >= strftime('%s','now','-7 days')
        """
    )
    return cursor.fetchone()[0]


def stats_active_subscriptions():
    cursor.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE expires_at > ?",
        (int(time.time()),)
    )
    return cursor.fetchone()[0]

# ================== FREEMIUM ==================

CRISIS_KEYWORDS = [
    "суицид", "умереть", "не хочу жить", "покончить",
    "паника", "очень плохо", "страшно", "тревожно", "бессмысленно"
]


def is_crisis(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in CRISIS_KEYWORDS)


def today():
    return date.today().isoformat()


def get_usage(user_id: int) -> int:
    cursor.execute(
        "SELECT count FROM usage WHERE user_id = ? AND date = ?",
        (user_id, today())
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def increment_usage(user_id: int):
    cursor.execute(
        """
        INSERT INTO usage (user_id, date, count)
        VALUES (?, ?, 1)
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

# ================== HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not has_history(user_id):
        text = (
            "Здравствуйте.\n\n"
            "Здесь не нужно подбирать правильные слова или что-то объяснять «как надо».\n"
            "Я постараюсь быть рядом и помочь вам разобраться в том, что сейчас происходит.\n\n"
            "Пишите столько и так, как вам комфортно."
        )
    else:
        last_ts = get_last_user_ts(user_id)
        gap = time.time() - last_ts if last_ts else 0

        if gap > LONG_GAP:
            text = (
                "Прошло некоторое время с нашего последнего разговора.\n\n"
                "Если вам важно — мы можем спокойно продолжить или начать с того, "
                "что сейчас для вас актуально."
            )
        else:
            text = (
                "Рада снова быть с вами на связи.\n\n"
                "Вы можете продолжить с того места, где остановились, "
                "или написать о том, что сейчас для вас важно."
            )

    await update.message.reply_text(text)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = (
        "📊 Статистика бота\n\n"
        f"👥 Всего пользователей: {stats_total_users()}\n"
        f"📆 Активных сегодня: {stats_today_users()}\n"
        f"📈 Активных за 7 дней: {stats_week_users()}\n"
        f"💳 Активных подписок: {stats_active_subscriptions()}"
    )

    await update.message.reply_text(text)

# ====== остальные handlers (start, pricing, subscribe, summary, chat) —
# ⚠️ ОСТАЮТСЯ БЕЗ ИЗМЕНЕНИЙ ⚠️
# Они уже есть в твоей версии и работают корректно

# ================== ЗАПУСК ==================

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("stats", stats_command))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("pricing", pricing_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("summary", summary_command))
app.add_handler(CallbackQueryHandler(subscribe_button_callback, pattern="^subscribe_start$"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

print("🧠 Психологический ИИ-бот со статистикой админа запущен")
app.run_polling()









