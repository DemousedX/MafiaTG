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
from aiohttp import web
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
PING_PORT   = int(os.getenv("PING_PORT", 8080))   # порт для keep-alive пінгу
SELF_URL    = os.getenv("SELF_URL", "")            # напр. https://your-app.onrender.com

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("MafiaBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─────────────────────────────────────────────
# ТРЕКЕР ПОВІДОМЛЕНЬ
# Використовуємо context.chat_data (вбудований PTB сховище),
# щоб ID не губились між хендлерами.
# Ключ: "bot_msg_ids" → list[int]
# ─────────────────────────────────────────────
_KEY = "bot_msg_ids"

def _track(chat_data: dict, msg_id: int) -> None:
    ids: list = chat_data.setdefault(_KEY, [])
    ids.append(msg_id)
    # Обмеження: не більше 50 ID на чат
    if len(ids) > 50:
        chat_data[_KEY] = ids[-50:]

async def _clear_menu(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Видалити всі відстежені повідомлення бота в поточному чаті."""
    ids: list = ctx.chat_data.pop(_KEY, [])
    if not ids:
        return
    # Telegram дозволяє видаляти масово через deleteMessages (Bot API 6.0+)
    # PTB fallback: видаляємо по одному
    for msg_id in ids:
        try:
            await ctx.bot.delete_message(
                chat_id=ctx.effective_chat.id if hasattr(ctx, 'effective_chat')
                       else ctx._chat_id,
                message_id=msg_id,
            )
        except (BadRequest, Forbidden):
            pass  # вже видалено або немає прав — ок
        except TelegramError as e:
            log.debug(f"delete_message {msg_id}: {e}")

async def _clear_menu_by_chat(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Видалити всі відстежені повідомлення — версія з явним chat_id."""
    ids: list = ctx.chat_data.pop(_KEY, [])
    for msg_id in ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except (BadRequest, Forbidden):
            pass
        except TelegramError as e:
            log.debug(f"delete {msg_id}: {e}")

async def safe_delete_msg(bot, chat_id: int, msg_id: int) -> None:
    """Видалити одне конкретне повідомлення без краша."""
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

# ─────────────────────────────────────────────
# КОМАНДИ
# Кожна команда:
#   1. Видаляє саму команду користувача
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

    # 1. Видалити команду юзера
    await safe_delete_msg(ctx.bot, chat_id, update.message.message_id)

    # 2. Видалити всі попередні повідомлення бота
    await _clear_menu_by_chat(ctx, chat_id)

    # 3. Надіслати нове меню
    await send_and_track(ctx, chat_id, text, keyboard)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # Якщо start_param — запрошення в кімнату
    start_param = ctx.args[0] if ctx.args else None
    if start_param and start_param.isdigit() and len(start_param) == 5:
        await safe_delete_msg(ctx.bot, chat_id, update.message.message_id)
        await _clear_menu_by_chat(ctx, chat_id)
        url_with_code = f"{WEBAPP_URL}?start={start_param}"
        await send_and_track(
            ctx, chat_id,
            rf"🎮 *Запрошення в кімнату* `{start_param}`\!",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"🎭 Зайти в кімнату {start_param}",
                    web_app=WebAppInfo(url=url_with_code),
                )
            ]]),
        )
        return

    if not WEBAPP_URL:
        await safe_delete_msg(ctx.bot, chat_id, update.message.message_id)
        await _clear_menu_by_chat(ctx, chat_id)
        sent = await ctx.bot.send_message(chat_id, "⚠️ Встанови WEBAPP_URL у .env")
        _track(ctx.chat_data, sent.message_id)
        return

    await _cmd_handler(update, ctx, WELCOME_TEXT, kb_main(WEBAPP_URL))


async def cmd_play(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not WEBAPP_URL:
        return
    await _cmd_handler(
        update, ctx,
        r"🎮 *Натисни щоб відкрити гру\!*",
        InlineKeyboardMarkup([[
            InlineKeyboardButton("🎭 Грати зараз", web_app=WebAppInfo(url=WEBAPP_URL))
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
# Редагує ПОТОЧНЕ повідомлення (без видалення).
# Коли клікають "← Назад" — редагує те саме, не надсилає нове.
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
# GLOBAL ERROR HANDLER — бот не падає ні від чого
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
    # Не піднімаємо — бот продовжує роботу

# ─────────────────────────────────────────────
# KEEP-ALIVE PING SERVER
# Render безкоштовний tier засинає після 15 хв.
# Цей мінімальний HTTP-сервер відповідає на GET /ping
# щоб зовнішній сервіс (UptimeRobot / cron-job.org тощо)
# міг тримати сервіс живим.
# ─────────────────────────────────────────────
async def handle_ping(request: web.Request) -> web.Response:
    """GET /ping → 200 OK. Використовується для keep-alive."""
    return web.Response(
        text='{"status":"ok","service":"mafia-bot"}',
        content_type="application/json",
    )

async def handle_health(request: web.Request) -> web.Response:
    """GET /health → статус сервісів."""
    node_alive = _node_process is not None and _node_process.poll() is None
    return web.Response(
        text=f'{{"status":"ok","node":{str(node_alive).lower()}}}',
        content_type="application/json",
        status=200 if node_alive else 503,
    )

async def start_ping_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/ping",   handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/",       handle_ping)   # fallback для Render health check
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PING_PORT)
    await site.start()
    log.info(f"🌐 Ping-сервер запущено на порту {PING_PORT}  (GET /ping)")
    return runner

async def self_ping_loop() -> None:
    """Пінгує сам себе кожні 14 хв щоб Render не засипав."""
    if not SELF_URL:
        log.info("ℹ️  SELF_URL не задано — само-пінг вимкнено")
        return
    import aiohttp as _aio
    url = SELF_URL.rstrip("/") + "/ping"
    log.info(f"🔁 Само-пінг активний: {url} кожні 14 хв")
    await asyncio.sleep(60)  # перший пінг через 1 хв після старту
    while True:
        try:
            async with _aio.ClientSession() as session:
                async with session.get(url, timeout=_aio.ClientTimeout(total=10)) as r:
                    log.debug(f"self-ping → {r.status}")
        except Exception as e:
            log.warning(f"self-ping failed: {e}")
        await asyncio.sleep(14 * 60)  # 14 хвилин

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
    # persistence=None — не зберігаємо chat_data на диск (достатньо в пам'яті)
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

    # 3. Keep-alive ping сервер
    ping_runner = await start_ping_server()
    asyncio.create_task(self_ping_loop())

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
        try:
            await ping_runner.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass
