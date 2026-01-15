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

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("❌ Не заданы TELEGRAM_TOKEN или OPENAI_API_KEY")

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

# ================== SUBSCRIPTIONS ==================

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


def has_active_subscription(user_id: int) -> bool:
    cursor.execute(
        "SELECT expires_at FROM subscriptions WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    return row is not None and row[0] > time.time()

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
    keyboard = [
        [InlineKeyboardButton("🟢 Оформить подписку", callback_data="subscribe_start")]
    ]
    return InlineKeyboardMarkup(keyboard)

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


async def pricing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRICING_TEXT, reply_markup=subscribe_keyboard())


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(PRICING_TEXT, reply_markup=subscribe_keyboard())


async def subscribe_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "Спасибо за интерес к подписке.\n\n"
        "Оплата будет доступна в ближайшее время.\n"
        "Я сообщу, когда можно будет оформить подписку."
    )


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not has_history(user_id):
        await update.message.reply_text(
            "Пока у нас ещё не было разговора, который можно было бы обобщить."
        )
        return

    summary = get_summary(user_id)
    if not summary:
        generate_summary(user_id)
        summary = get_summary(user_id)

    await update.message.reply_text(
        "Вот как я сейчас вижу общую картину нашего разговора.\n\n"
        f"{summary}\n\n"
        "Если что-то откликается — можно продолжить с этого места.\n"
        "Если нет — вы можете поправить или написать о том, что сейчас важнее."
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text.strip()

    if not has_active_subscription(user_id):
        usage = get_usage(user_id)
        if usage >= FREE_DAILY_LIMIT and not is_crisis(user_text):
            await update.message.reply_text(
                "Я здесь и готов продолжать разговор.\n\n"
                "На сегодня вы использовали бесплатный лимит сообщений.\n"
                "Если хотите общаться без ограничений — можно оформить подписку.\n\n"
                "Если же сейчас тяжело или тревожно — напишите об этом, я отвечу."
            )
            return

    save_message(user_id, "user", user_text)
    if not has_active_subscription(user_id):
        increment_usage(user_id)

    if count_user_messages(user_id) % SUMMARY_TRIGGER == 0:
        try:
            generate_summary(user_id)
        except Exception:
            pass

    history = load_last_messages(user_id, MAX_HISTORY)
    summary = get_summary(user_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if summary:
        messages.append({
            "role": "system",
            "content": f"Краткое резюме предыдущих разговоров:\n{summary}"
        })

    messages.extend(history)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.6
    )

    answer = response.choices[0].message.content
    save_message(user_id, "assistant", answer)
    await update.message.reply_text(answer)

# ================== ЗАПУСК ==================

app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("pricing", pricing_command))
app.add_handler(CommandHandler("subscribe", subscribe_command))
app.add_handler(CommandHandler("summary", summary_command))
app.add_handler(CallbackQueryHandler(subscribe_button_callback, pattern="^subscribe_start$"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

print("🧠 Психологический ИИ-бот с pricing и кнопкой оформления запущен")
app.run_polling()









