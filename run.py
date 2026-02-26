"""
🎭 МАФІЯ — Telegram Bot + Game Server Launcher
================================================
Запускає одночасно:
  • Node.js WebSocket сервер (server.js)
  • Telegram бота (python-telegram-bot)

Запуск:
    python run.py

Залежності:
    pip install python-telegram-bot aiohttp python-dotenv
    npm install   (для server.js)
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# ──────────────────────────────────────────────
# КОНФІГ
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")          # Отримай у @BotFather
WEBAPP_URL  = os.getenv("WEBAPP_URL", "")          # https://your-domain.com
SERVER_PORT = int(os.getenv("PORT", 3000))
NODE_CWD    = Path(__file__).parent                 # Папка з server.js

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mafia.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("MafiaBot")

# Приглушуємо httpx spam
logging.getLogger("httpx").setLevel(logging.WARNING)


# ──────────────────────────────────────────────
# ТЕКСТИ
# ──────────────────────────────────────────────
WELCOME_TEXT = """
🎭 *Вітаємо у Мафії\!*

Класична соціальна гра для *4–20 гравців* прямо в Telegram\.

*Ролі:*
🔫 Мафія — вбиває вночі
⭐ Шериф — розкриває мафію
💊 Лікар — рятує від смерті
🏘️ Мирний — знаходить мафію голосуванням

*Цикл гри:*
🌙 Ніч → 🌅 Світанок → ☀️ Обговорення → ⚖️ Голосування

Натисни *Грати* щоб відкрити гру\!
""".strip()

HELP_TEXT = """
📖 *Довідка по грі*

*Як грати:*
1️⃣ Створи кімнату або введи 5\-значний код
2️⃣ Поділись кодом з друзями \(до 20 гравців\)
3️⃣ Хост натискає "Почати гру"

*Ролі та цілі:*
• 🔫 *Мафія* — щоночі обирають жертву\. Перемогли, якщо мафія ≥ мирних
• ⭐ *Шериф* — перевіряє гравця кожну ніч \(результат тільки йому\)
• 💊 *Лікар* — рятує гравця від вбивства, навіть себе
• 🏘️ *Мирний* — шукає мафію обговоренням та голосуванням

*Кількість гравців → ролі:*
`4–5` → 1 мафія, 1 шериф
`6–8` → 2 мафії, 1 шериф, 1 лікар
`9–12` → 3 мафії, 1 шериф, 1 лікар
`13+` → 4\-5 мафій, 1 шериф, 1 лікар

*Підказки:*
• Свою роль видно на *карті знизу екрану* — потягни її вгору
• Мирні сплять вночі — не треба нічого робити
• Голосуй розумно — помилкове виключення мирного допомагає мафії\!
""".strip()

STATS_TEXT = """
📊 *Статистика сесії*

*Активні кімнати:* `{rooms}`
*Гравців онлайн:* `{players}`
*Ігор зіграно:* `{games}`

_Дані оновлюються в реальному часі_
""".strip()


# ──────────────────────────────────────────────
# ХЕНДЛЕРИ БОТА
# ──────────────────────────────────────────────
def make_play_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎭 Грати зараз",
            web_app=WebAppInfo(url=url),
        )
    ]])


def make_menu_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Відкрити гру", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton("📖 Правила", callback_data="help"),
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
        ],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        await update.message.reply_text(
            "⚠️ Сервер ще не налаштований\. Встанови `WEBAPP_URL` у \.env",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await update.message.reply_photo(
        photo="https://i.imgur.com/rM8KiRY.png",  # fallback if missing
        caption=WELCOME_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=make_menu_keyboard(WEBAPP_URL),
    )


async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        await update.message.reply_text("⚠️ WEBAPP\\_URL не встановлено", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await update.message.reply_text(
        "🎭 Натисни щоб розпочати гру\!",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=make_play_keyboard(WEBAPP_URL),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 Грати", web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL
        else InlineKeyboardButton("⚙️ Налаштуй WEBAPP_URL", callback_data="noop")
    ]])
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb,
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    # Запит статистики до локального сервера
    stats = await fetch_server_stats()
    text = STATS_TEXT.format(
        rooms=stats.get("rooms", "?"),
        players=stats.get("players", "?"),
        games=stats.get("games", "?"),
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_stop_game(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Тільки для адмінів"""
    user_id = update.effective_user.id
    admin_ids_raw = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()]
    if user_id not in admin_ids:
        await update.message.reply_text("❌ Немає прав")
        return
    await update.message.reply_text("🛑 Команда зупинки не реалізована через бот\. Зупини сервер вручну\.")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await query.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)
    elif query.data == "stats":
        stats = await fetch_server_stats()
        text = STATS_TEXT.format(
            rooms=stats.get("rooms", "?"),
            players=stats.get("players", "?"),
            games=stats.get("games", "?"),
        )
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def unknown_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        return
    await update.message.reply_text(
        "🎭 Натисни кнопку нижче щоб зіграти у Мафію\!",
        reply_markup=make_play_keyboard(WEBAPP_URL),
    )


# ──────────────────────────────────────────────
# СТАТИСТИКА СЕРВЕРА
# ──────────────────────────────────────────────
async def fetch_server_stats() -> dict:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://localhost:{SERVER_PORT}/api/stats",
                timeout=aiohttp.ClientTimeout(total=2),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return {"rooms": "—", "players": "—", "games": "—"}


# ──────────────────────────────────────────────
# NODE.JS СЕРВЕР
# ──────────────────────────────────────────────
_node_process: subprocess.Popen | None = None


def start_node_server() -> subprocess.Popen:
    """Запускає server.js у фоні"""
    log.info("🚀 Запускаємо Node.js сервер (server.js)…")
    env = os.environ.copy()
    env["PORT"] = str(SERVER_PORT)

    proc = subprocess.Popen(
        ["node", "server.js"],
        cwd=NODE_CWD,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Перенаправляємо логи Node.js в наш logger
    import threading

    def pipe_logs():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.info(f"[node] {line}")

    t = threading.Thread(target=pipe_logs, daemon=True)
    t.start()

    log.info(f"✅ Node.js PID={proc.pid} | порт {SERVER_PORT}")
    return proc


def stop_node_server(proc: subprocess.Popen) -> None:
    if proc and proc.poll() is None:
        log.info("🛑 Зупиняємо Node.js сервер…")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info("✅ Node.js зупинено")


# ──────────────────────────────────────────────
# TELEGRAM BOT
# ──────────────────────────────────────────────
async def post_init(app: Application) -> None:
    """Встановлює команди в меню бота"""
    await app.bot.set_my_commands([
        BotCommand("start",  "🎭 Головне меню"),
        BotCommand("play",   "🎮 Грати одразу"),
        BotCommand("help",   "📖 Правила гри"),
        BotCommand("stats",  "📊 Статистика сервера"),
    ])
    info = await app.bot.get_me()
    log.info(f"✅ Бот запущено: @{info.username}")


def build_bot(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("play",   cmd_play))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("admin",  cmd_stop_game))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    return app


# ──────────────────────────────────────────────
# ВАЛІДАЦІЯ КОНФІГА
# ──────────────────────────────────────────────
def check_config() -> bool:
    ok = True
    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN не встановлено! Додай у .env")
        ok = False
    if not WEBAPP_URL:
        log.warning("⚠️  WEBAPP_URL не встановлено — кнопка гри не буде відображатись")
    if not Path(NODE_CWD / "server.js").exists():
        log.error("❌ server.js не знайдено! Переконайся що run.py поруч з server.js")
        ok = False
    # Перевіряємо node
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        log.info(f"✅ Node.js: {result.stdout.strip()}")
    except FileNotFoundError:
        log.error("❌ Node.js не встановлено! Встанови з https://nodejs.org")
        ok = False
    return ok


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main() -> None:
    log.info("=" * 55)
    log.info("  🎭  МАФІЯ — Game Server + Telegram Bot  ")
    log.info("=" * 55)

    if not check_config():
        log.error("Зупинка через помилки конфігурації")
        sys.exit(1)

    # Стартуємо Node.js
    global _node_process
    _node_process = start_node_server()

    # Даємо Node.js секунду стартануть
    import time
    time.sleep(1.5)

    if _node_process.poll() is not None:
        log.error("❌ Node.js аварійно завершився відразу після запуску!")
        sys.exit(1)

    # Хендлер сигналів для коректного завершення
    def handle_signal(sig, frame):
        log.info(f"Отримано сигнал {sig}, завершуємо…")
        stop_node_server(_node_process)
        sys.exit(0)

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Запускаємо Telegram бот (блокує main thread)
    log.info("🤖 Запускаємо Telegram бота…")
    bot_app = build_bot(BOT_TOKEN)
    bot_app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
