"""
🎭 МАФІЯ — Telegram Bot + Game Server Launcher
================================================
"""

import asyncio
import logging
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
# ТРЕКЕР ПОВІДОМЛЕНЬ БОТА
# Зберігає ID повідомлень бота для кожного чату,
# щоб видаляти їх перед показом нового меню.
# ─────────────────────────────────────────────
# { chat_id: [msg_id, msg_id, ...] }
_bot_messages: dict[int, list[int]] = defaultdict(list)
MAX_TRACKED = 20  # максимум відслідковуємо N повідомлень на чат

def track_message(chat_id: int, msg_id: int) -> None:
    lst = _bot_messages[chat_id]
    lst.append(msg_id)
    # Обрізаємо щоб не накопичувати нескінченно
    if len(lst) > MAX_TRACKED:
        _bot_messages[chat_id] = lst[-MAX_TRACKED:]

async def delete_all_bot_messages(bot, chat_id: int) -> None:
    """Видалити всі збережені повідомлення бота в цьому чаті."""
    msg_ids = _bot_messages.pop(chat_id, [])
    for msg_id in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden):
            # Повідомлення вже видалено або немає прав — ігноруємо
            pass
        except TelegramError as e:
            log.debug(f"delete_message {msg_id}: {e}")

async def safe_delete(message) -> None:
    """Видалити одне повідомлення без краша."""
    try:
        await message.delete()
    except (BadRequest, Forbidden):
        pass
    except TelegramError as e:
        log.debug(f"safe_delete: {e}")

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
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
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
    return (
        r"📊 *Статистика сервера*" + "\n\n"
        + rf"🏠 *Кімнат активних:* `{data['rooms']}`" + "\n"
        + rf"👥 *Гравців онлайн:* `{data['players']}`" + "\n"
        + rf"🎮 *Зіграно ігор:* `{data.get('games', 0)}`"
    )

async def send_menu(chat_id: int, bot, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Відправити нове меню і зберегти ID повідомлення."""
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
        )
        track_message(chat_id, sent.message_id)
    except TelegramError as e:
        log.error(f"send_menu failed: {e}")

# ─────────────────────────────────────────────
# КОМАНДИ
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await safe_delete(update.message)
    await delete_all_bot_messages(ctx.bot, chat_id)

    if not WEBAPP_URL:
        sent = await ctx.bot.send_message(chat_id, "⚠️ Встанови WEBAPP_URL у .env")
        track_message(chat_id, sent.message_id)
        return

    # Якщо start_param — запрошення в конкретну кімнату
    start_param = ctx.args[0] if ctx.args else None
    if start_param and start_param.isdigit() and len(start_param) == 5:
        url_with_code = f"{WEBAPP_URL}?start={start_param}"
        await send_menu(
            chat_id, ctx.bot,
            rf"🎮 *Запрошення в кімнату* `{start_param}`\!",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🎭 Зайти в кімнату {start_param}",
                    web_app=WebAppInfo(url=url_with_code),
                )
            ]]),
        )
        return

    await send_menu(chat_id, ctx.bot, WELCOME_TEXT, kb_main(WEBAPP_URL))

async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await safe_delete(update.message)
    await delete_all_bot_messages(ctx.bot, chat_id)
    if not WEBAPP_URL:
        return
    await send_menu(
        chat_id, ctx.bot,
        r"🎮 *Натисни щоб відкрити гру\!*",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🎭 Грати зараз", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]),
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await safe_delete(update.message)
    await delete_all_bot_messages(ctx.bot, chat_id)
    await send_menu(
        chat_id, ctx.bot,
        RULES_TEXT,
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await safe_delete(update.message)
    await delete_all_bot_messages(ctx.bot, chat_id)
    data = await fetch_stats()
    await send_menu(
        chat_id, ctx.bot,
        build_stats_text(data),
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await safe_delete(update.message)
    await delete_all_bot_messages(ctx.bot, chat_id)
    await send_menu(
        chat_id, ctx.bot,
        HELP_TEXT,
        kb_back_play(WEBAPP_URL) if WEBAPP_URL else kb_back(),
    )

# ─────────────────────────────────────────────
# CALLBACK — inline кнопки (редагують існуюче повідомлення)
# ─────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        await q.answer()
    except TelegramError:
        pass  # query протух — не критично

    async def edit(text: str, keyboard: InlineKeyboardMarkup) -> None:
        try:
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        except BadRequest as e:
            # "Message is not modified" або повідомлення видалено
            if "not modified" not in str(e).lower():
                log.debug(f"edit_message_text: {e}")
        except TelegramError as e:
            log.debug(f"edit_message_text error: {e}")

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
# GLOBAL ERROR HANDLER — не дає боту впасти
# ─────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    if isinstance(err, NetworkError):
        log.warning(f"NetworkError (ігноруємо): {err}")
    elif isinstance(err, Forbidden):
        log.warning(f"Forbidden — бот заблокований користувачем: {err}")
    elif isinstance(err, BadRequest):
        log.warning(f"BadRequest: {err}")
    elif isinstance(err, TelegramError):
        log.error(f"TelegramError: {err}")
    else:
        log.exception(f"Неочікувана помилка: {err}", exc_info=err)
    # Нічого не піднімаємо — бот продовжує роботу

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

    # Команди
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("play",  cmd_play))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Inline кнопки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Глобальний обробник помилок — бот не падає
    app.add_error_handler(error_handler)

    await app.initialize()

    # Меню "/" в Telegram
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
