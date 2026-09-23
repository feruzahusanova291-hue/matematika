"""
🧠 Matematika AI Ustozi — Telegram + Gemini boti

Ishga tushirish:
    pip install -r requirements.txt
    cp .env.example .env   # so'ng kalitlarni to'ldiring
    python bot.py
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

load_dotenv()

# --------------------------------------------------------------------------- #
# Sozlamalar
# --------------------------------------------------------------------------- #
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
SYSTEM_FILE = Path(os.getenv("SYSTEM_INSTRUCTION_FILE", "system_instruction.json"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY_MESSAGES", "16"))  # oxirgi N ta xabar
COOLDOWN = float(os.getenv("COOLDOWN_SECONDS", "2"))  # xabarlar orasidagi pauza
ALLOWED_USERS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}  # bo'sh bo'lsa — hamma foydalanishi mumkin

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("math-bot")

# --------------------------------------------------------------------------- #
# System prompt (JSON fayldan o'qiladi)
# --------------------------------------------------------------------------- #
TELEGRAM_RULES = """

=== TELEGRAM UCHUN QO'SHIMCHA QOIDALAR ===
- Javob tili: o'quvchi qaysi tilda yozsa, shu tilda javob ber (asosan o'zbek tilida).
- LaTeX ($...$, \\frac, \\sqrt) ISHLATMA. Telegram uni ko'rsatmaydi. Buning o'rniga oddiy
  belgilardan foydalan: x², x³, √, ×, ÷, ≤, ≥, ≠, π, ½, a/b.
- Formatlash: faqat **qalin** va `kod` ishlat. Sarlavha (#), jadval va murakkab
  Markdown ishlatma.
- Javob juda uzun bo'lmasin (imkon qadar 3000 belgidan kam).
- Foydalanuvchi rasm yuborsa, undagi masalani diqqat bilan o'qi, keyin yuqoridagi
  qoidalar bo'yicha davom et.
- Matematikaga aloqasi yo'q savollarga muloyimlik bilan "men matematika ustoziman"
  deb, mavzuga qaytishni taklif qil.
"""


def build_system_prompt() -> str:
    data = json.loads(SYSTEM_FILE.read_text(encoding="utf-8"))
    body = json.dumps(data.get("system_instruction", data), ensure_ascii=False, indent=2)
    return (
        "Quyidagi JSON — sening rolingni, xulq-atvor qoidalaringni va javob "
        "formatlaringni belgilaydi. Unga qat'iy amal qil:\n\n" + body + TELEGRAM_RULES
    )


SYSTEM_PROMPT = build_system_prompt()
client = genai.Client(api_key=GEMINI_API_KEY)

LEVELS = ["🟢 Boshlang'ich", "🔵 O'rta", "🟠 Kuchli", "🔴 Olimpiada darajasi"]

# --------------------------------------------------------------------------- #
# Foydalanuvchi holati (xotirada; ko'p foydalanuvchi bo'lsa Redis/DB ishlating)
# --------------------------------------------------------------------------- #
histories: dict[int, list[types.Content]] = defaultdict(list)
user_levels: dict[int, int] = {}
last_call: dict[int, float] = {}
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# --------------------------------------------------------------------------- #
# Tugmalar
# --------------------------------------------------------------------------- #
ANSWER_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("💡 Hint", callback_data="hint"),
            InlineKeyboardButton("✅ To'liq yechim", callback_data="full"),
        ],
        [
            InlineKeyboardButton("📝 O'xshash masala", callback_data="similar"),
            InlineKeyboardButton("🔽 Osonroq tushuntir", callback_data="easier"),
        ],
    ]
)

CALLBACK_PROMPTS = {
    "hint": "Menga keyingi hint (ishora)ni ber. Hint tizimidagi navbatdagi darajani qo'lla, "
    "lekin yakuniy javobni aytma.",
    "full": "Iltimos, shu masalaning to'liq yechimini va yakuniy javobini "
    "'full_solution_mode' formatida ko'rsat.",
    "similar": "Shunga o'xshash, lekin sonlari boshqacha qisqa mini-mashq ber. "
    "Javobini hozir aytma, men o'zim yechib ko'raman.",
    "easier": "Buni tushunmadim. Masalani soddalashtirib, kichik bo'laklarga ajratib tushuntir.",
}

# --------------------------------------------------------------------------- #
# Yordamchi funksiyalar
# --------------------------------------------------------------------------- #


def to_telegram_html(text: str) -> str:
    """Gemini'ning Markdown javobini Telegram HTML'iga xavfsiz o'giradi."""
    text = html.escape(text, quote=False)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"^\s*\*\s+", "• ", text, flags=re.M)
    return text


def split_text(text: str, limit: int = 3500) -> list[str]:
    """Uzun javobni Telegram limitiga (4096) mos bo'laklarga bo'ladi."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


async def send_reply(message, text: str) -> None:
    chunks = split_text(text)
    for i, chunk in enumerate(chunks):
        markup = ANSWER_KEYBOARD if i == len(chunks) - 1 else None
        try:
            await message.reply_text(
                to_telegram_html(chunk), parse_mode=ParseMode.HTML, reply_markup=markup
            )
        except BadRequest:
            # HTML buzilib qolsa — oddiy matn sifatida yuboramiz
            await message.reply_text(chunk, reply_markup=markup)


async def keep_typing(chat) -> None:
    try:
        while True:
            await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def make_config(user_id: int) -> types.GenerateContentConfig:
    instruction = SYSTEM_PROMPT
    level_idx = user_levels.get(user_id)
    if level_idx is not None:
        instruction += (
            f"\n\nO'quvchi tanlagan daraja: {LEVELS[level_idx]}. "
            "Tushuntirish va masalalarni shu darajaga moslashtir."
        )
    return types.GenerateContentConfig(
        system_instruction=instruction,
        temperature=0.4,
        max_output_tokens=8192,
    )


def trim_history(history: list[types.Content]) -> None:
    del history[:-MAX_HISTORY]
    while history and history[0].role != "user":
        history.pop(0)


async def ask_gemini(user_id: int, parts: list[types.Part]) -> str:
    history = histories[user_id]
    history.append(types.Content(role="user", parts=parts))
    trim_history(history)

    models_to_try: list[str] = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
    if MODEL and MODEL not in models_to_try:
        models_to_try.append(MODEL)

    last_error = None
    for model_name in models_to_try:
        try:
            response = await client.aio.models.generate_content(
                model=model_name, contents=history, config=make_config(user_id)
            )
            text = (response.text or "").strip()
            if text:
                history.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
                trim_history(history)
                return text
        except Exception as e:
            last_error = e
            log.warning("Model %s xato berdi (%s), boshqa modelga o'tilmoqda...", model_name, e)

    history.pop()  # muvaffaqiyatsiz bo'lsa tarixdan olib tashlaymiz
    if last_error:
        raise last_error
    return "🤔 Javob shakllantira olmadim. Masalani boshqacha yozib, qayta yuborib ko'ring."


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


async def handle_prompt(message, user_id: int, parts: list[types.Part]) -> None:
    if not is_allowed(user_id):
        await message.reply_text("⛔ Kechirasiz, sizga bu botdan foydalanishga ruxsat berilmagan.")
        return

    now = time.monotonic()
    if now - last_call.get(user_id, 0) < COOLDOWN:
        await message.reply_text("⏳ Iltimos, bir necha soniya kuting.")
        return
    last_call[user_id] = now

    async with user_locks[user_id]:
        typing_task = asyncio.create_task(keep_typing(message.chat))
        try:
            answer = await ask_gemini(user_id, parts)
            await send_reply(message, answer)
        except Exception:
            log.exception("Gemini so'rovida xatolik")
            await message.reply_text(
                "⚠️ Kutilmagan xatolik yuz berdi. Bir ozdan so'ng qayta urinib ko'ring."
            )
        finally:
            typing_task.cancel()


# --------------------------------------------------------------------------- #
# Handlerlar
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.effective_user.first_name or "do'stim"
    await update.message.reply_text(
        f"👋 Salom, {name}! Men — Matematika ustozingman 🧠\n\n"
        "Masalani matn ko'rinishida yozing yoki rasmga olib yuboring.\n\n"
        "Men darhol javobni aytib qo'ymayman: avval o'ylashga yo'naltiraman, "
        "keyin kerak bo'lsa hint va to'liq yechim beraman. "
        "Maqsad — sizning o'zingiz yechishni o'rganishingiz 💪\n\n"
        "Buyruqlar:\n"
        "/level — qiyinlik darajasini tanlash\n"
        "/reset — yangi mavzuni boshlash\n"
        "/help — yordam"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📚 Qanday foydalanish:\n\n"
        "1. Masalani yozing yoki rasmini yuboring.\n"
        "2. Men savol yoki hint bilan yo'naltiraman.\n"
        "3. Qiynalsangiz — «💡 Hint» tugmasini bosing (3 bosqichli yordam).\n"
        "4. Yechimingizni yozsangiz, xatoni tahlil qilib beraman.\n"
        "5. To'liq yechim kerak bo'lsa — «✅ To'liq yechim» yoki shunday deb yozing.\n\n"
        "Olimpiada rejimi uchun «olimpiada», «isbotla» kabi so'zlarni ishlating."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    histories.pop(update.effective_user.id, None)
    await update.message.reply_text("🔄 Suhbat tozalandi. Yangi masalani yuboring!")


async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(name, callback_data=f"lvl:{i}")] for i, name in enumerate(LEVELS)]
    )
    await update.message.reply_text("🎚 Qiyinlik darajasini tanlang:", reply_markup=keyboard)


async def on_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split(":")[1])
    user_levels[query.from_user.id] = idx
    await query.edit_message_text(f"✅ Daraja tanlandi: {LEVELS[idx]}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    prompt = CALLBACK_PROMPTS.get(query.data)
    if not prompt:
        return
    try:  # eski xabardagi tugmalarni olib tashlaymiz
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await handle_prompt(
        query.message, query.from_user.id, [types.Part.from_text(text=prompt)]
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_prompt(
        update.message,
        update.effective_user.id,
        [types.Part.from_text(text=update.message.text)],
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]  # eng katta o'lcham
    file = await photo.get_file()
    data = bytes(await file.download_as_bytearray())
    caption = update.message.caption or "Rasmdagi masalani o'qib, tahlil qil."
    await handle_prompt(
        update.message,
        update.effective_user.id,
        [
            types.Part.from_bytes(data=data, mime_type="image/jpeg"),
            types.Part.from_text(text=caption),
        ],
    )


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📎 Hozircha faqat matn va rasm qabul qila olaman.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handlerda xatolik", exc_info=context.error)


async def start_dummy_server() -> None:
    port = os.getenv("PORT")
    if not port:
        return
    async def handle_ping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n\r\nOK v3"
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        await asyncio.start_server(handle_ping, "0.0.0.0", int(port))
        log.info("Render Web Service uchun port ochildi: %s", port)
    except Exception as e:
        log.warning("Port serverini ishga tushirishda xatolik: %s", e)


async def keep_alive_task() -> None:
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    log.info("Keep-alive faollashtirildi: %s", url)
    import urllib.request
    while True:
        await asyncio.sleep(600)  # har 10 daqiqada
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Render-KeepAlive/1.0"})
            urllib.request.urlopen(req, timeout=15)
            log.info("Keep-alive ping muvaffaqiyatli: %s", url)
        except Exception as e:
            log.debug("Keep-alive ping xatosi: %s", e)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Botni boshlash"),
            BotCommand("level", "Qiyinlik darajasini tanlash"),
            BotCommand("reset", "Suhbatni tozalash"),
            BotCommand("help", "Yordam"),
        ]
    )
    await start_dummy_server()
    asyncio.create_task(keep_alive_task())


def main() -> None:
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .concurrent_updates(True)  # foydalanuvchilar bir vaqtda xizmat oladi
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("level", level_cmd))
    app.add_handler(CallbackQueryHandler(on_level, pattern=r"^lvl:\d$"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(hint|full|similar|easier)$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(
        MessageHandler(~filters.COMMAND & ~filters.TEXT & ~filters.PHOTO, on_other)
    )
    app.add_error_handler(on_error)

    log.info("Bot ishga tushdi. Model: %s", MODEL)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
        timeout=20,
    )


if __name__ == "__main__":
    main()
