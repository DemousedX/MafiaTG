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
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
# ТРЕКЕР ПОВІДОМЛЕНЬ
# ─────────────────────────────────────────────
_KEY = "bot_msg_ids"

def _track(chat_data: dict, msg_id: int) -> None:
    ids: list = chat_data.setdefault(_KEY, [])
    ids.append(msg_id)
    if len(ids) > 50:
        chat_data[_KEY] = ids[-50:]

async def _clear_menu_by_chat(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    ids: list = ctx.chat_data.pop(_KEY, [])
    for msg_id in ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden):
            pass
        except TelegramError as e:
            log.debug(f"delete {msg_id}: {e}")

async def safe_delete_msg(bot, chat_id: int, msg_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except (BadRequest, Forbidden):
        pass
    except TelegramError as e:
        log.debug(f"safe_delete_msg: {e}")

async def send_and_track(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
    reply_markup_extra=None,
) -> None:
    """Відправити меню і зберегти ID у трекері."""
    try:
        sent = await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )
        _track(ctx.chat_data, sent.message_id)
    except TelegramError as e:
        log.error(f"send_and_track: {e}")

# ─────────────────────────────────────────────
# PERSISTENT REPLY KEYBOARD (кнопка біля вводу)
# ─────────────────────────────────────────────
def kb_play_button(url: str) -> ReplyKeyboardMarkup:
    """Постійна кнопка 'Грати' біля поля введення повідомлення."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Грати", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Мафія · /help",
    )

async def ensure_play_button(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Надіслати невидиме повідомлення щоб встановити reply keyboard."""
    if not WEBAPP_URL:
        return
    try:
        sent = await ctx.bot.send_message(
            chat_id=chat_id,
            text="​",   # zero-width space — майже невидимо
            reply_markup=kb_play_button(WEBAPP_URL),
        )
        # Одразу видаляємо це технічне повідомлення
        await safe_delete_msg(ctx.bot, chat_id, sent.message_id)
    except TelegramError as e:
        log.debug(f"ensure_play_button: {e}")

# ─────────────────────────────────────────────
# ТЕКСТИ — мінімалізм
# ─────────────────────────────────────────────
WELCOME_TEXT = r"""
🎭 *Мафія*

Соціальна гра для 4–20 гравців\.
Ролі, брехня, дедукція\.
""".strip()

HELP_TEXT = r"""
*Ролі*

🔫 *Мафія* — вбиває вночі
⭐ *Шериф* — перевіряє гравців
💊 *Лікар* — рятує від смерті
🏘️ *Мирний* — шукає мафію

*Як грати*

Вночі кожна роль діє потай\.
Вдень — обговорення та голосування\.
Мирні перемагають, знищивши всю мафію\.
Мафія — зрівнявшись із мирними\.
""".strip()

RULES_TEXT = r"""
*Нічна фаза*

Місто засинає\. Мафія обирає жертву\.
Шериф перевіряє одного гравця\.
Лікар рятує одного гравця\.

*Денна фаза*

Місто дізнається результати ночі\.
Хвилина обговорення — потім голосування\.
Хто набрав більше голосів — вибуває\.

_Мінімум 4 гравці для старту\._
""".strip()

# ─────────────────────────────────────────────
# INLINE КЛАВІАТУРИ
# ─────────────────────────────────────────────
def kb_main(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Грати →", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton("Правила",    callback_data="rules"),
            InlineKeyboardButton("Статистика", callback_data="stats"),
        ],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Назад", callback_data="main")]
    ])

def kb_back_play(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Грати →",  web_app=WebAppInfo(url=url))],
        [InlineKeyboardButton("← Назад", callback_data="main")],
    ])

# ─────────────────────────────────────────────
# УТИЛІТИ
# ─────────────────────────────────────────────
async def fetch_stats() -> dict:
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
    rooms   = data.get("rooms", 0)
    players = data.get("players", 0)
    games   = data.get("games", 0)
    return (
        "*Статистика*\n\n"
        f"Кімнат · `{rooms}`\n"
        f"Гравців · `{players}`\n"
        f"Ігор зіграно · `{games}`"
    )

# ─────────────────────────────────────────────
# КОМАНДИ
# Кожна команда:
#   1. Видаляє саму команду користувача ("/" повідомлення)
#   2. Видаляє всі попередні повідомлення бота (з трекера)
#   3. Надсилає нове меню і трекає його ID
# ─────────────────────────────────────────────
async def _cmd_handler(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Спільна логіка для всіх команд."""
    chat_id = update.effective_chat.id

    # 1. Видалити команду "/" юзера
    if update.message:
        await safe_delete_msg(ctx.bot, chat_id, update.message.message_id)

    # 2. Видалити всі попередні повідомлення бота
    await _clear_menu_by_chat(ctx, chat_id)

    # 3. Надіслати нове меню
    await send_and_track(ctx, chat_id, text, keyboard)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # 1. Завжди видалити команду юзера
    if update.message:
        await safe_delete_msg(ctx.bot, chat_id, update.message.message_id)

    # 2. Очистити попередні повідомлення бота
    await _clear_menu_by_chat(ctx, chat_id)

    # 3. Встановити persistent кнопку "Грати" біля поля вводу
    await ensure_play_button(ctx, chat_id)

    # Якщо start_param — запрошення в кімнату
    start_param = ctx.args[0] if ctx.args else None
    if start_param and start_param.isdigit() and len(start_param) == 5:
        url_with_code = f"{WEBAPP_URL}?start={start_param}"
        await send_and_track(
            ctx, chat_id,
            rf"Запрошення в кімнату `{start_param}`\.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"Зайти →",
                    web_app=WebAppInfo(url=url_with_code),
                )
            ]]),
        )
        return

    if not WEBAPP_URL:
        sent = await ctx.bot.send_message(chat_id, "⚠️ Встанови WEBAPP_URL у .env")
        _track(ctx.chat_data, sent.message_id)
        return

    await send_and_track(ctx, chat_id, WELCOME_TEXT, kb_main(WEBAPP_URL))


async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        return
    await _cmd_handler(
        update, ctx,
        r"*Мафія*\. Натисни щоб почати\.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("Грати →", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]),
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_handler(
        update, ctx,
        RULES_TEXT,
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    data = await fetch_stats()
    await _cmd_handler(
        update, ctx,
        build_stats_text(data),
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_handler(
        update, ctx,
        HELP_TEXT,
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

# ─────────────────────────────────────────────
# CALLBACK — inline кнопки
# ─────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except TelegramError:
        pass

    async def edit(text: str, keyboard: InlineKeyboardMarkup) -> None:
        try:
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.debug(f"edit_message: {e}")
        except TelegramError as e:
            log.debug(f"edit_message error: {e}")

    if q.data == "main":
        if not WEBAPP_URL:
            await edit("⚠️ WEBAPP_URL не налаштовано", kb_back())
        else:
            await edit(WELCOME_TEXT, kb_main(WEBAPP_URL))

    elif q.data == "help":
        await edit(HELP_TEXT, kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back())

    elif q.data == "rules":
        await edit(RULES_TEXT, kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back())

    elif q.data == "stats":
        data = await fetch_stats()
        await edit(build_stats_text(data), kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back())

# ─────────────────────────────────────────────
# GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning(f"NetworkError (ігноруємо): {err}")
    elif isinstance(err, Forbidden):
        log.warning(f"Forbidden — бот заблокований: {err}")
    elif isinstance(err, BadRequest):
        log.warning(f"BadRequest: {err}")
    elif isinstance(err, TelegramError):
        log.error(f"TelegramError: {err}")
    else:
        log.exception(f"Неочікувана помилка: {err}", exc_info=err)

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
    log.info("=" * 40)
    log.info("   🎭  MAFIA BOT  STARTING   ")
    log.info("=" * 40)

    if not BOT_TOKEN:
        log.error("❌ BOT_TOKEN не знайдено в .env!")
        return

    if not WEBAPP_URL:
        log.warning("⚠️  WEBAPP_URL не встановлено — кнопка гри недоступна")

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

    app.add_error_handler(error_handler)

    await app.initialize()

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
