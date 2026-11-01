import asyncio
import html
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiohttp import web

import server

BASE_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8080"))
TRADE_CHANNEL_ID = int(os.getenv("TRADE_CHANNEL_ID", "0") or 0)  # публичный канал обменов
PENDING = {}  # user_id -> id публикации, на которую человек сейчас пишет предложение
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0") or 0)  # канал для запросов (необязательно, иначе придёт вам в личку)

if not TOKEN:
    sys.exit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")

bot = Bot(token=TOKEN)
dp = Dispatcher()


def is_admin(message: Message) -> bool:
    return bool(ADMIN_ID) and message.from_user is not None and message.from_user.id == ADMIN_ID


def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎮 Открыть GAG Hub", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


async def redeem(message: Message, code: str) -> None:
    u = message.from_user
    await message.answer(server.redeem_code(u.id, u.username, code.strip()))


async def new_code(message: Message, parts: list) -> None:
    ok = len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit() and \
        server.create_code(parts[0], int(parts[1]), parts[2], int(parts[3]))
    await message.answer(f"✅ Код {parts[0].upper()} создан." if ok else
                         "Формат: /newcode КОД АКТИВАЦИИ petals|case КОЛИЧЕСТВО")


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    args = command.args or ""
    if args.startswith("promo_"):
        await redeem(message, args[6:])
        return
    if args.startswith("bid_") and args[4:].isdigit():
        post = server.get_post(int(args[4:]))
        if not post:
            await message.answer("Это предложение уже недоступно.")
        elif post["uid"] == message.from_user.id:
            await message.answer("Это ваше собственное предложение.")
        else:
            PENDING[message.from_user.id] = post["id"]
            await message.answer(f"Обмен #{post['id']}: {post['give']} → {post['want']}\n\nНапишите одним сообщением, что вы предлагаете взамен (до 300 символов).")
        return
    if args.startswith("ref_") and args[4:].isdigit():
        note = server.referral(message.from_user.id, message.from_user.username, int(args[4:]))
        if note:
            await message.answer(note)
    if args.startswith("newcode_"):
        if is_admin(message):
            await new_code(message, args[8:].split("_"))
        return
    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Приложение пока не подключено: администратору нужно задать WEBAPP_URL.")
        return
    await message.answer(
        "👋 Добро пожаловать в GAG Hub!\n\nКрутите колесо, открывайте кейсы, меняйтесь питомцами. "
        "Petals и питомцы живут только внутри приложения.",
        reply_markup=app_keyboard(),
    )


@dp.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    await redeem(message, command.args or "")


@dp.message(Command("newcode"))
async def cmd_newcode(message: Message, command: CommandObject):
    if is_admin(message):
        await new_code(message, (command.args or "").split())


@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    p = (command.args or "").split()
    if len(p) != 2 or not p[1].isdigit() or not server.credit(p[0], petals=int(p[1])):
        await message.answer("Формат: /give @username 100. Игрок должен хотя бы раз открыть приложение.")
        return
    await message.answer("✅ Petals зачислены.")


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


async def do_broadcast(text: str) -> int:
    n = 0
    for uid in server.user_ids():
        try:
            await bot.send_message(uid, "📢 " + text)
            n += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return n


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    if is_admin(message) and command.args:
        await message.answer(f"✅ Отправлено: {await do_broadcast(command.args[:500])}")


@dp.message(Command("export"))
async def cmd_export(message: Message):
    if is_admin(message):
        await message.answer_document(BufferedInputFile(server.export_csv().encode("utf-8-sig"), filename="players.csv"))


@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if is_admin(message) and command.args:
        await message.answer("✅ Забанен" if server.set_ban(command.args.strip(), True) else "Игрок не найден")


@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if is_admin(message) and command.args:
        await message.answer("✅ Разбанен" if server.set_ban(command.args.strip(), False) else "Игрок не найден")


async def notify_post(pid: int, text: str) -> bool:
    if not TRADE_CHANNEL_ID:
        return False
    me = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Предложить обмен", url=f"https://t.me/{me.username}?start=bid_{pid}")]])
    try:
        await bot.send_message(TRADE_CHANNEL_ID, text, reply_markup=kb)
        return True
    except Exception as error:
        logging.warning("Не удалось опубликовать в канал обменов: %s", error)
        return False


async def notify_request(rid: int, uid: int, text: str) -> bool:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выдано", callback_data=f"req_ok_{rid}"),
        InlineKeyboardButton(text="↩ Отклонить", callback_data=f"req_no_{rid}")]])
    try:
        await bot.send_message(CHANNEL_ID or ADMIN_ID, text, reply_markup=kb)
        return True
    except Exception as error:
        logging.warning("Не удалось отправить запрос: %s", error)
        return False


@dp.callback_query(F.data.startswith("req_"))
async def on_request(cb: CallbackQuery):
    if not ADMIN_ID or cb.from_user.id != ADMIN_ID:
        await cb.answer("Только администратор", show_alert=True)
        return
    _, act, rid = cb.data.split("_")
    r = server.resolve_request(int(rid), act == "ok")
    if not r:
        await cb.answer("Уже обработан")
        return
    await cb.answer("Готово")
    if cb.message:
        await cb.message.edit_text(cb.message.text + ("\n\n✅ Выдано" if act == "ok" else "\n\n↩ Отклонён, питомец возвращён"))
    try:
        await bot.send_message(r["uid"], f"Запрос на {r['pet']}: " + ("админ принял, проверьте Roblox." if act == "ok" else "отклонён, питомец вернулся в коллекцию."))
    except Exception:
        pass


@dp.message(F.text & ~F.text.startswith("/"))
async def on_offer_text(message: Message):
    pid = PENDING.pop(message.from_user.id, None)
    post = server.get_post(pid) if pid else None
    if not post:
        return
    u = message.from_user
    who = f"@{u.username}" if u.username else f'<a href="tg://user?id={u.id}">{html.escape(u.full_name)}</a>'
    try:
        await bot.send_message(post["uid"], f"📩 Предложение по обмену #{pid} ({html.escape(post['give'])} → {html.escape(post['want'])}) от {who}:\n\n{html.escape(message.text[:300])}", parse_mode="HTML")
        await message.answer("✅ Предложение отправлено автору. Он свяжется с вами сам.")
    except Exception:
        await message.answer("Не удалось доставить: автор не запускал бота или заблокировал его.")


@dp.my_chat_member()
async def on_added(ev: ChatMemberUpdated):
    if ev.chat.type == "channel" and ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"ID канала «{ev.chat.title}»: {ev.chat.id}\nВпишите CHANNEL_ID={ev.chat.id} в .env и перезапустите бота.")


async def wheel_reminders():
    tick = 0
    while True:
        if tick % 720 == 0:
            try:
                logging.info("Копия базы: %s", server.backup_db())
            except Exception as error:
                logging.warning("Не удалось сделать копию базы: %s", error)
        tick += 1
        await asyncio.sleep(30)
        for r in server.due_reminders():
            server.delete_reminder(r["id"])
            try:
                await bot.send_message(r["uid"], "⏰ В Grow a Garden обновился " + ("магазин семян и гаджетов." if r["kind"] == "seed" else "магазин яиц.") + " Загляните в игру!")
            except Exception:
                pass
        for uid in server.due_wheel_users():
            server.mark_notified(uid)
            try:
                await bot.send_message(uid, "🎡 Колесо снова доступно!", reply_markup=app_keyboard())
            except Exception:
                pass


async def main():
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(BASE_DIR / "server.log", encoding="utf-8")])
    runner = web.AppRunner(server.make_app(TOKEN, ADMIN_ID, notify_request, notify_post, do_broadcast))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"API запущен на порту {PORT}. Бот GAG Hub запущен!")
    asyncio.create_task(wheel_reminders())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")