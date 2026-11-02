"""Автокопия базы gag.db в закрытый Telegram-канал (для бесплатного Render, где диск стирается).

Как работает:
  • restore_if_needed() вызывается ДО `import server`: скачивает закреплённый в канале gag.db.
  • backup_loop() раз в N секунд присылает свежую копию, редактируя то же закреплённое сообщение.
Только стандартная библиотека + объект бота aiogram (передаётся снаружи).
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "gag.db"
MARKER = BASE / ".gag_live"          # есть → диск пережил перезапуск, базу не трогаем
API = os.getenv("TG_API_BASE", "https://api.telegram.org")

STATE = {"safe": True, "message_id": None, "changes": -1}   # safe=False → копии не шлём (не затираем хорошую пустой)


def _call(token: str, method: str, **params):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"{API}/bot{token}/{method}", data=data, timeout=30) as r:
        res = json.loads(r.read().decode())
    if not res.get("ok"):
        raise RuntimeError(res.get("description", "Telegram error"))
    return res["result"]


def _valid_db(path: Path) -> bool:
    try:
        c = sqlite3.connect(path)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and \
            c.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone() is not None
        c.close()
        return ok
    except Exception:
        return False


def restore_if_needed(token: str, channel_id: int) -> str:
    """Возвращает: 'off' | 'kept' | 'none' | 'restored' | 'error'. Вызывать до import server."""
    if not channel_id:
        return "off"
    on_render = bool(os.getenv("RENDER"))
    if DB.exists() and not (on_render and not MARKER.exists()):
        _mark()
        return "kept"
    try:
        chat = _call(token, "getChat", chat_id=channel_id)
        pin = chat.get("pinned_message")
        doc = pin.get("document") if pin else None
        if not doc:
            STATE["safe"] = True
            _mark()
            return "none"            # копий ещё нет — это первый запуск
        path = _call(token, "getFile", file_id=doc["file_id"])["file_path"]
        tmp = BASE / "restore.tmp"
        with urllib.request.urlopen(f"{API}/file/bot{token}/{path}", timeout=60) as r:
            tmp.write_bytes(r.read())
        if not _valid_db(tmp):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("скачанная копия повреждена")
        if DB.exists():
            DB.replace(BASE / "gag.db.old")
        tmp.replace(DB)
        STATE["message_id"] = pin["message_id"]
        _mark()
        return "restored"
    except Exception as error:
        STATE["safe"] = False         # не знаем, есть ли хорошая копия → не перезаписываем её
        logging.error("Не удалось восстановить базу из канала: %s", error)
        return "error"


def _mark() -> None:
    try:
        MARKER.write_text(str(time.time()))
    except OSError:
        pass


async def _send(bot, channel_id: int, path: Path, caption: str) -> None:
    from aiogram.types import FSInputFile, InputMediaDocument
    file = FSInputFile(path, filename="gag.db")
    if STATE["message_id"]:
        try:
            await bot.edit_message_media(chat_id=channel_id, message_id=STATE["message_id"],
                                         media=InputMediaDocument(media=file, caption=caption))
            return
        except Exception as error:
            if "not modified" in str(error).lower():
                return
            logging.warning("Не удалось обновить закреплённое сообщение, шлю новое: %s", error)
    msg = await bot.send_document(channel_id, file, caption=caption, disable_notification=True)
    STATE["message_id"] = msg.message_id
    await bot.pin_chat_message(channel_id, msg.message_id, disable_notification=True)


async def backup_once(bot, channel_id: int, conn) -> str:
    if not channel_id or not STATE["safe"]:
        return "off"
    if conn.total_changes == STATE["changes"]:
        return "same"                  # с прошлой копии база не менялась
    snap = BASE / "snapshot.db"
    dst = sqlite3.connect(snap)
    conn.backup(dst)
    dst.close()
    await _send(bot, channel_id, snap, "gag.db " + time.strftime("%d.%m.%Y %H:%M:%S"))
    STATE["changes"] = conn.total_changes
    snap.unlink(missing_ok=True)
    return "sent"


async def backup_loop(bot, channel_id: int, conn, every: int = 600) -> None:
    if not channel_id:
        return
    delay = 30                         # первая копия почти сразу после запуска
    while True:
        await asyncio.sleep(delay)
        delay = every
        try:
            await backup_once(bot, channel_id, conn)
        except Exception as error:
            logging.warning("Не удалось отправить копию базы: %s", error)
