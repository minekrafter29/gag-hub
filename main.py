import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "users.json"  # {username_в_нижнем_регистре: chat_id}
MAX_GIFT = 10_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def load_env() -> None:
    """Читает файл .env рядом с main.py (KEY=VALUE), не трогая уже заданные переменные."""
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
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()  # https://ваш-ник.github.io/gag-hub/
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)    # ваш числовой Telegram ID (команда /myid)

if not TOKEN:
    sys.exit("Не задан BOT_TOKEN. Создайте файл .env по образцу .env.example")

bot = Bot(token=TOKEN)
dp = Dispatcher()


def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def remember_user(message: Message) -> None:
    user = message.from_user
    if not user or not user.username:
        return
    users = load_users()
    users[user.username.lower()] = message.chat.id
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def is_admin(message: Message) -> bool:
    return bool(ADMIN_ID) and message.from_user is not None and message.from_user.id == ADMIN_ID


def open_app_keyboard(url: str, text: str = "🎮 Открыть GAG Hub") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]]
    )


async def give_petals(admin_message: Message, username: str, amount_raw: str) -> None:
    """Отправляет игроку подарок: кнопку, которая откроет приложение и зачислит Petals."""
    username = username.lstrip("@")
    if not USERNAME_RE.match(username):
        await admin_message.answer("Неверный юзернейм.")
        return
    if not amount_raw.isdigit() or not (1 <= int(amount_raw) <= MAX_GIFT):
        await admin_message.answer(f"Количество должно быть числом от 1 до {MAX_GIFT}.")
        return
    if not WEBAPP_URL.startswith("https://"):
        await admin_message.answer("В .env не задан WEBAPP_URL (нужна ссылка https).")
        return

    chat_id = load_users().get(username.lower())
    if not chat_id:
        await admin_message.answer(
            f"@{username} ещё не запускал бота. Пусть откроет бота и нажмёт /start, затем повторите."
        )
        return

    amount = int(amount_raw)
    gift_url = WEBAPP_URL + "?" + urlencode(
        {"gift": amount, "gid": uuid.uuid4().hex[:12], "to": username.lower()}
    )
    try:
        await bot.send_message(
            chat_id,
            f"🎁 Вам подарок от администратора: {amount} Petals!\nНажмите кнопку, чтобы забрать.",
            reply_markup=open_app_keyboard(gift_url, "🌸 Забрать подарок"),
        )
    except TelegramAPIError as error:
        await admin_message.answer(f"Не удалось отправить: {error}")
        return
    await admin_message.answer(f"✅ Подарок {amount} Petals отправлен @{username}.")


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    remember_user(message)

    # Быстрая выдача из админ-вкладки приложения: /start give_<username>_<amount>
    args = command.args or ""
    if args.startswith("give_"):
        if not is_admin(message):
            await message.answer("Эта команда доступна только администратору.")
            return
        try:
            username, amount = args[len("give_"):].rsplit("_", 1)
        except ValueError:
            await message.answer("Неверный формат команды.")
            return
        await give_petals(message, username, amount)
        return

    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Приложение пока не подключено: администратору нужно задать WEBAPP_URL.")
        return
    await message.answer(
        "👋 Добро пожаловать в GAG Hub!\n\n"
        "🎡 Крутите ежедневное колесо, копите Petals и открывайте кейсы с питомцами из Grow a Garden. "
        "Петалы и питомцы живут только внутри приложения.\n\n"
        "Нажмите кнопку ниже:",
        reply_markup=open_app_keyboard(WEBAPP_URL),
    )


@dp.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject):
    if not is_admin(message):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /give @username 100")
        return
    await give_petals(message, parts[0], parts[1])


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = "Команды: /start — открыть приложение, /help — справка."
    if is_admin(message):
        text += "\nАдмин: /give @username 100 — подарить Petals."
    await message.answer(text)


async def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    if not ADMIN_ID:
        print("ADMIN_ID не задан: админ-команды выключены. Отправьте боту /myid и впишите число в .env")
    print("Бот GAG Hub запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
