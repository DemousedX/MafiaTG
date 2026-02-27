"""
🎭 МАФІЯ — Telegram Bot + Game Server Launcher
================================================
"""

import asyncio
import logging
import os
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
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────
# КОНФІГ
# ─────────────────────────────────────────────
load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
WEBAPP_URL  = os.getenv("WEBAPP_URL", "")
SERVER_PORT = int(os.getenv("PORT", 3000))
NODE_CWD    = Path(__file__).parent

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("MafiaBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─────────────────────────────────────────────
# ТЕКСТИ
# ─────────────────────────────────────────────
WELCOME_TEXT = r"""
🎭 *Вітаємо у Мафії\!*

Класична соціальна гра для *4–20 гравців* прямо в Telegram\.

Оберіть дію нижче:
""".strip()

HELP_TEXT = r"""
📖 *Як грати у Мафію*

*Ролі:*
🔫 *Мафія* — вбиває одного гравця щоночі
⭐ *Шериф* — перевіряє одного гравця щоночі
💊 *Лікар* — рятує одного гравця щоночі
🏘️ *Мирний* — виявляє мафію голосуванням

*Хід гри:*
1️⃣ Збери 4–20 гравців, поділись кодом кімнати
2️⃣ Хост натискає "Почати гру"
3️⃣ Всі отримують таємні ролі
4️⃣ Вночі — мафія діє потай
5️⃣ Вдень — обговорення та голосування
6️⃣ Мирні перемагають, знищивши всю мафію
7️⃣ Мафія перемагає, зрівнявшись з мирними

*Команди:*
/start — Головне меню
/play — Відкрити гру
/rules — Правила
/stats — Статистика сервера
/help — Довідка
""".strip()

RULES_TEXT = r"""
📜 *Правила гри*

🌙 *Вночі:*
— Всі "засипають"
— Мафія обирає жертву \(або пропускає\)
— Шериф перевіряє одного гравця
— Лікар рятує одного гравця

☀️ *Вдень:*
— Всі дізнаються результати ночі
— 1 хвилина обговорення в чаті
— Голосування за підозрюваного
— Більшість голосів — гравець вибуває

⚖️ *Умови перемоги:*
— 🕊️ *Мирні:* знищити всю мафію
— 🔫 *Мафія:* зрівнятись або перевищити к\-сть мирних

🎭 *Мінімум 4 гравці для початку гри\.*
""".strip()

# ─────────────────────────────────────────────
# КЛАВІАТУРИ
# ─────────────────────────────────────────────
def kb_main(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Відкрити гру", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton("📖 Як грати", callback_data="help"),
            InlineKeyboardButton("📜 Правила",  callback_data="rules"),
        ],
        [InlineKeyboardButton("📊 Статистика",  callback_data="stats")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Назад до меню", callback_data="main")]
    ])

def kb_back_play(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Грати", web_app=WebAppInfo(url=url))],
        [InlineKeyboardButton("← Назад до меню", callback_data="main")],
    ])

# ─────────────────────────────────────────────
# УТИЛІТИ
# ─────────────────────────────────────────────
async def safe_delete(message) -> None:
    """Видалити повідомлення, ігноруючи помилки (вже видалено, без прав тощо)."""
    try:
        await message.delete()
    except Exception:
        pass

async def fetch_stats() -> dict:
    """Отримати статистику з Node.js сервера."""
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
    return {"rooms": 0, "players": 0, "games": 0}

def build_stats_text(data: dict) -> str:
    return (
        r"📊 *Статистика сервера*" + "\n\n"
        + rf"🏠 *Кімнат активних:* `{data['rooms']}`" + "\n"
        + rf"👥 *Гравців онлайн:* `{data['players']}`" + "\n"
        + rf"🎮 *Зіграно ігор:* `{data.get('games', 0)}`"
    )

# ─────────────────────────────────────────────
# КОМАНДИ  (кожна команда видаляє своє повідомлення)
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Головне меню."""
    await safe_delete(update.message)
    if not WEBAPP_URL:
        await update.effective_chat.send_message(
            r"⚠️ Встанови `WEBAPP_URL` у \.env файлі",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    # Якщо є start_param — це посилання з ботом+кодом кімнати
    start_param = ctx.args[0] if ctx.args else None
    if start_param and start_param.isdigit() and len(start_param) == 5:
        url_with_code = f"{WEBAPP_URL}?start={start_param}"
        await update.effective_chat.send_message(
            rf"🎮 *Запрошення в кімнату* `{start_param}`\!",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🎭 Зайти в кімнату {start_param}",
                    web_app=WebAppInfo(url=url_with_code),
                )
            ]]),
        )
        return
    await update.effective_chat.send_message(
        WELCOME_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_main(WEBAPP_URL),
    )

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Відкрити гру напряму."""
    await safe_delete(update.message)
    if not WEBAPP_URL:
        return
    await update.effective_chat.send_message(
        r"🎮 *Натисни щоб відкрити гру\!*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎭 Грати зараз", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]),
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Правила гри."""
    await safe_delete(update.message)
    await update.effective_chat.send_message(
        RULES_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Статистика сервера."""
    await safe_delete(update.message)
    data = await fetch_stats()
    await update.effective_chat.send_message(
        build_stats_text(data),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Довідка."""
    await safe_delete(update.message)
    await update.effective_chat.send_message(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

# ─────────────────────────────────────────────
# CALLBACK — inline кнопки меню
# ─────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if q.data == "main":
        if not WEBAPP_URL:
            await q.edit_message_text("⚠️ WEBAPP_URL не налаштовано")
            return
        await q.edit_message_text(
            WELCOME_TEXT,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_main(WEBAPP_URL),
        )

    elif q.data == "help":
        await q.edit_message_text(
            HELP_TEXT,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
        )

    elif q.data == "rules":
        await q.edit_message_text(
            RULES_TEXT,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
        )

    elif q.data == "stats":
        data = await fetch_stats()
        await q.edit_message_text(
            build_stats_text(data),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
        )

# ─────────────────────────────────────────────
# NODE.JS УПРАВЛІННЯ
# ─────────────────────────────────────────────
_node_process: subprocess.Popen | None = None

def start_node_server() -> subprocess.Popen:
    log.info("🚀 Запускаємо Node.js сервер...")
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
    import threading
    def _pipe():
        for line in proc.stdout:
            if line.strip():
                log.info(f"[node] {line.strip()}")
    threading.Thread(target=_pipe, daemon=True).start()
    return proc

def stop_node_server(proc: subprocess.Popen) -> None:
    if proc and proc.poll() is None:
        log.info("🛑 Зупиняємо Node.js...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main_async():
    log.info("=" * 36)
    log.info("    🎭  MAFIA BOT  STARTING    ")
    log.info("=" * 36)

    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN не знайдено в .env!")
        return

    if not WEBAPP_URL:
        log.warning("⚠️  WEBAPP_URL не встановлено — кнопка гри буде недоступна")

    # 1. Node.js
    global _node_process
    _node_process = start_node_server()
    await asyncio.sleep(2)

    if _node_process.poll() is not None:
        log.error("❌ Node.js аварійно завершився відразу після запуску!")
        return

    log.info(f"✅ Node.js PID={_node_process.pid} | порт {SERVER_PORT}")

    # 2. Telegram Application
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("play",  cmd_play))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))

    await app.initialize()

    # Встановити список команд у меню "/" в Telegram
    await app.bot.set_my_commands([
        BotCommand("start", "🎭 Головне меню"),
        BotCommand("play",  "🎮 Відкрити гру"),
        BotCommand("rules", "📜 Правила гри"),
        BotCommand("stats", "📊 Статистика"),
        BotCommand("help",  "📖 Як грати"),
    ])

    await app.start()
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    log.info("✅ Бот та Сервер онлайн! Ctrl+C для зупинки.")

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
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass
