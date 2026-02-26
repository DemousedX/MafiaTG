"""
🎭 МАФІЯ — Telegram Bot + Game Server Launcher
================================================
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

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
WEBAPP_URL  = os.getenv("WEBAPP_URL", "")
SERVER_PORT = int(os.getenv("PORT", 3000))
NODE_CWD    = Path(__file__).parent

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("MafiaBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ──────────────────────────────────────────────
# ТЕКСТИ (Додано r"" для виправлення SyntaxWarning)
# ──────────────────────────────────────────────
WELCOME_TEXT = r"""
🎭 *Вітаємо у Мафії\!*

Класична соціальна гра для *4–20 гравців* прямо в Telegram\.

Натисни *Грати* щоб відкрити гру\!
""".strip()

HELP_TEXT = r"""
📖 *Довідка по грі*

1️⃣ Створи кімнату або введи 5\-значний код
2️⃣ Поділись кодом з друзями
3️⃣ Хост натискає "Почати гру"
""".strip()

STATS_TEXT = r"""
📊 *Статистика сесії*
*Активні кімнати:* `{rooms}`
*Гравців онлайн:* `{players}`
"""

# ──────────────────────────────────────────────
# ХЕНДЛЕРИ ТА ДОПОМІЖНІ ФУНКЦІЇ
# ──────────────────────────────────────────────
def make_play_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Грати зараз", web_app=WebAppInfo(url=url))]])

def make_menu_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Відкрити гру", web_app=WebAppInfo(url=url))],
        [InlineKeyboardButton("📖 Правила", callback_data="help"), InlineKeyboardButton("📊 Статистика", callback_data="stats")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        await update.message.reply_text(r"⚠️ Встанови `WEBAPP_URL` у .env", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=make_menu_keyboard(WEBAPP_URL))

async def fetch_server_stats() -> dict:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://localhost:{SERVER_PORT}/api/stats", timeout=2) as resp:
                if resp.status == 200: return await resp.json()
    except Exception: pass
    return {"rooms": "—", "players": "—"}

# ──────────────────────────────────────────────
# NODE.JS УПРАВЛІННЯ
# ──────────────────────────────────────────────
_node_process: subprocess.Popen | None = None

def start_node_server() -> subprocess.Popen:
    log.info("🚀 Запускаємо Node.js сервер...")
    env = os.environ.copy()
    env["PORT"] = str(SERVER_PORT)
    proc = subprocess.Popen(["node", "server.js"], cwd=NODE_CWD, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    import threading
    def pipe_logs():
        for line in proc.stdout:
            if line.strip(): log.info(f"[node] {line.strip()}")
    threading.Thread(target=pipe_logs, daemon=True).start()
    return proc

def stop_node_server(proc: subprocess.Popen) -> None:
    if proc and proc.poll() is None:
        log.info("🛑 Зупиняємо Node.js...")
        proc.terminate()

# ──────────────────────────────────────────────
# ГОЛОВНИЙ ЗАПУСК (Async/Await для Python 3.14)
# ──────────────────────────────────────────────
async def main_async():
    log.info("=" * 30)
    log.info("  🎭 MAFIA STARTING...  ")
    log.info("=" * 30)

    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN is missing!")
        return

    # 1. Запуск Node.js
    global _node_process
    _node_process = start_node_server()
    await asyncio.sleep(2) # Даємо час серверу

    if _node_process.poll() is not None:
        log.error("❌ Node.js failed to start")
        return

    # 2. Налаштування Бота
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    
    # 3. Ручний запуск через Updater (уникаємо get_event_loop помилки)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    
    log.info("✅ Бот та Сервер онлайн!")

    # Тримаємо цикл активним
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        log.info("Зупинка...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        stop_node_server(_node_process)

if __name__ == "__main__":
    try:
        # Використовуємо asyncio.run для створення нового циклу подій
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass
