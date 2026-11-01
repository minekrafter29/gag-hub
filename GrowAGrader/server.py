"""API мини-приложения: SQLite, проверка подписи Telegram, колесо, кейсы, обмен, промокоды."""
import calendar
import datetime
import hashlib
import hmac
import json
import random
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

BASE = Path(__file__).resolve().parent
PETS = json.loads((BASE / "pets.json").read_text(encoding="utf-8"))
ALL_NAMES = {p["name"]: k for k, v in PETS.items() for p in v}
COOLDOWN = 24 * 3600
# t: p = Petals, pet = питомец; v — базовое значение (масштабируется по дню месяца), w — вес в %
WHEEL = [("p", 10, 30), ("p", 25, 28), ("pet", "rare", 8), ("p", 50, 18),
         ("pet", "legendary", 3), ("p", 75, 8), ("p", 100, 4), ("pet", "mythical", 1)]
SCALE = {10: .4, 25: .6, 50: .8, 75: .9, 100: 1}
W_WEIGHTS = [30, 28, 8, 18, 3, 8, 4, 1]
CASES = {
    "sprout": (30, {"rare": 90, "legendary": 10}),
    "garden": (90, {"rare": 55, "legendary": 44, "mythical": 1}),
    "mythic": (250, {"legendary": 60, "mythical": 39, "divine": 1}),
    "prism": (600, {"mythical": 60, "divine": 39, "prismatic": 1}),
    "free": (0, {"rare": 40, "legendary": 30, "mythical": 20, "divine": 9, "prismatic": 1}),
}
VAL = {p["name"]: int(p["value"].split()[0]) for v in PETS.values() for p in v}
SELL = {"rare": 4, "legendary": 15, "mythical": 60, "divine": 200, "prismatic": 400}
ORDER = ["rare", "legendary", "mythical", "divine", "prismatic"]
TIERS = {1: [100], 2: [1, 99], 3: [1, 24, 75], 4: [1, 9, 30, 60], 5: [1, 4, 10, 25, 60]}
EVENTS = {  # только питомцы, найденные в источниках и присутствующие в каталоге
    "ev_halloween": ["Reaper", "Headless Horseman", "Wolf", "Ghost Bear", "Black Cat", "Hex Serpent", "Tomb Marmot"],
    "ev_christmas": ["Gift Rat", "Arctic Fox", "Turtle Dove", "Partridge", "Christmas Gorilla", "Reindeer", "Ice Golem",
                     "Penguin", "Snowman Builder", "Pine Beetle"],
}
CODE_RE = re.compile(r"^[A-Za-z0-9]{3,20}$")
POOLS = {}
for _cid, _names in EVENTS.items():
    _pool = [n for n in _names if n in ALL_NAMES]
    _keys = sorted({ALL_NAMES[n] for n in _pool}, key=ORDER.index, reverse=True)
    CASES[_cid] = (120, dict(zip(_keys, TIERS[len(_keys)])))
    POOLS[_cid] = _pool
CASES["daily"] = (0, {"rare": 100})  # кейс дня: только самые дешёвые питомцы
CASES["piggy"] = (20, {"c5": 45, "c10": 30, "c20": 12, "c50": 8, "rare": 4.9, "legendary": 0.1})
CASES["lucky"] = (100, {"c20": 30, "c50": 30, "c100": 15, "c200": 10, "c500": 4, "rare": 5.5, "legendary": 5, "mythical": 0.5})
CASES["gold"] = (400, {"c100": 30, "c200": 25, "c400": 15, "c1000": 5, "legendary": 15, "mythical": 8.99, "divine": 1, "prismatic": 0.01})
CASES["rainbow"] = (1000, {"c200": 25, "c500": 25, "c1000": 15, "c3000": 5, "mythical": 20, "divine": 9.99, "prismatic": 0.01})
POOLS["daily"] = [p["name"] for p in sorted(PETS["rare"], key=lambda p: int(p["value"].split()[0]))[:15]]

conn = sqlite3.connect(BASE / "gag.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, petals INTEGER DEFAULT 0,
  last_spin REAL DEFAULT 0, free_cases INTEGER DEFAULT 0, inv TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, uname TEXT,
  give TEXT, want TEXT, created REAL);
CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, left INTEGER, kind TEXT, amt INTEGER);
CREATE TABLE IF NOT EXISTS used(code TEXT, uid INTEGER, PRIMARY KEY(code, uid));
""")


conn.execute("CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, uname TEXT, roblox TEXT, pet TEXT, status TEXT DEFAULT 'open', created REAL)")
conn.execute("CREATE TABLE IF NOT EXISTS trade_posts(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, uname TEXT, give TEXT, want TEXT, note TEXT, created REAL)")
conn.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
conn.execute("CREATE TABLE IF NOT EXISTS admin_log(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created REAL)")
conn.execute("CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, at REAL, kind TEXT)")
conn.executescript("""
CREATE TABLE IF NOT EXISTS activity(day TEXT, uid INTEGER, PRIMARY KEY(day,uid));
CREATE TABLE IF NOT EXISTS actions(day TEXT, kind TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day,kind));
CREATE TABLE IF NOT EXISTS trade_log(id INTEGER PRIMARY KEY AUTOINCREMENT, a_uid INTEGER, a_name TEXT, b_uid INTEGER, b_name TEXT, give TEXT, want TEXT, created REAL);
CREATE TABLE IF NOT EXISTS reviews(tid INTEGER, from_uid INTEGER, to_uid INTEGER, up INTEGER, PRIMARY KEY(tid,from_uid));
CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, uname TEXT, target TEXT, text TEXT, created REAL);
""")
RATE = {}
for _col in ("notified REAL DEFAULT 0", "referred INTEGER DEFAULT 0", "daily_at REAL DEFAULT 0",
             "bonus_spins INTEGER DEFAULT 0", "ref_code TEXT", "banned INTEGER DEFAULT 0"):
    try:
        conn.execute("ALTER TABLE users ADD COLUMN " + _col)
    except sqlite3.OperationalError:
        pass
try:
    conn.execute("ALTER TABLE users ADD COLUMN stats TEXT DEFAULT '{}'")
except sqlite3.OperationalError:
    pass


def bump(uid: int, **kw) -> None:
    st = json.loads(conn.execute("SELECT stats FROM users WHERE id=?", (uid,)).fetchone()["stats"] or "{}")
    for k, v in kw.items():
        st[k] = st.get(k, 0) + v
    conn.execute("UPDATE users SET stats=? WHERE id=?", (json.dumps(st), uid))
    day = time.strftime("%Y-%m-%d")
    for k, v in kw.items():
        conn.execute("INSERT INTO actions(day,kind,n) VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET n=n+?", (day, k, v, v))


def get_setting(k: str):
    r = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def set_setting(k: str, v) -> None:
    conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
    conn.commit()


def log_admin(text: str) -> None:
    conn.execute("INSERT INTO admin_log(text,created) VALUES(?,?)", (f"{time.strftime('%d.%m %H:%M')} {text}", time.time()))
    conn.commit()


def due_reminders() -> list:
    return conn.execute("SELECT * FROM reminders WHERE at<=?", (time.time(),)).fetchall()


def delete_reminder(rid: int) -> None:
    conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()


def promo_info():
    p = json.loads(get_setting("promo") or "null")
    return {"mult": p["mult"], "until": int(p["until"] * 1000)} if p and p.get("until", 0) > time.time() else None


def active_theme():
    today = time.strftime("%Y-%m-%d")
    for e in json.loads(get_setting("schedule") or "[]"):
        if e["from"] <= today <= e["to"]:
            return e["theme"]
    return json.loads(get_setting("theme") or "null")


def theme_pick() -> bool:
    return get_setting("theme_pick") != "0"


def export_csv() -> str:
    out = ["id,username,petals,unique,total,spins,cases"]
    for r in conn.execute("SELECT * FROM users").fetchall():
        inv = json.loads(r["inv"])
        st = json.loads(r["stats"] or "{}")
        out.append(f'{r["id"]},{r["username"] or ""},{r["petals"]},{len(inv)},{sum(inv.values())},{st.get("spins", 0)},{st.get("cases", 0)}')
    return "\n".join(out)


def backup_db() -> str:
    d = BASE / "backups"
    d.mkdir(exist_ok=True)
    path = d / time.strftime("gag-%Y%m%d-%H%M.db")
    dst = sqlite3.connect(path)
    conn.backup(dst)
    dst.close()
    for old in sorted(d.glob("gag-*.db"))[:-20]:
        old.unlink()
    return str(path)


def user_ids() -> list:
    return [r["id"] for r in conn.execute("SELECT id FROM users").fetchall()]


def set_ban(username: str, banned: bool) -> bool:
    r = conn.execute("SELECT id FROM users WHERE username=?", (username.lower().lstrip("@"),)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, r["id"]))
    conn.commit()
    return True


def wheel_cap() -> int:
    d = datetime.date.today()
    dim = calendar.monthrange(d.year, d.month)[1]
    return int(2 + 18 * (d.day - 1) / (dim - 1) + 0.5)


def touch(uid: int, uname: str | None) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
    if uname:
        conn.execute("UPDATE users SET username=? WHERE id=?", (uname.lower(), uid))
    conn.execute("UPDATE users SET ref_code=? WHERE id=? AND (ref_code IS NULL OR ref_code='')",
                 (hashlib.sha1(f"gag{uid}".encode()).hexdigest()[:6].upper(), uid))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def state_of(uid: int) -> dict:
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {"petals": r["petals"], "inv": json.loads(r["inv"]), "lastSpin": int(r["last_spin"] * 1000),
            "freeCases": r["free_cases"], "stats": json.loads(r["stats"] or "{}"),
            "dailyAt": int((r["daily_at"] or 0) * 1000),
            "bonusSpins": r["bonus_spins"] or 0, "refCode": r["ref_code"], "referred": bool(r["referred"]),
            "rep": conn.execute("SELECT COALESCE(SUM(up),0) c FROM reviews WHERE to_uid=?", (uid,)).fetchone()["c"]}


def add_pet(uid: int, name: str, n: int = 1) -> bool:
    inv = json.loads(conn.execute("SELECT inv FROM users WHERE id=?", (uid,)).fetchone()["inv"])
    inv[name] = inv.get(name, 0) + n
    if inv[name] < 0:
        return False
    if inv[name] == 0:
        del inv[name]
    conn.execute("UPDATE users SET inv=? WHERE id=?", (json.dumps(inv, ensure_ascii=False), uid))
    return True


def pick(weights: dict) -> str:
    return random.choices(list(weights), weights=list(weights.values()))[0]


def rand_pet(rarity: str, pool=None) -> str:
    names = [p["name"] for p in PETS[rarity]] if pool is None else [n for n in pool if ALL_NAMES[n] == rarity]
    return random.choice(names)


# ---- функции для бота ----
def credit(username: str, petals: int = 0, free_cases: int = 0) -> bool:
    r = conn.execute("SELECT id FROM users WHERE username=?", (username.lower().lstrip("@"),)).fetchone()
    if not r:
        return False
    conn.execute("UPDATE users SET petals=petals+?, free_cases=free_cases+? WHERE id=?", (petals, free_cases, r["id"]))
    conn.commit()
    return True


def create_code(code: str, left: int, kind: str, amt: int) -> bool:
    if not CODE_RE.match(code) or kind not in ("petals", "case") or left < 1:
        return False
    conn.execute("INSERT OR REPLACE INTO codes VALUES(?,?,?,?)", (code.upper(), left, kind, amt))
    conn.commit()
    return True


def redeem_code(uid: int, uname: str | None, code: str) -> str:
    touch(uid, uname)
    c = conn.execute("SELECT * FROM codes WHERE code=?", (code.upper(),)).fetchone()
    if not c or c["left"] <= 0:
        return "Код недействителен или закончился."
    if conn.execute("SELECT 1 FROM used WHERE code=? AND uid=?", (c["code"], uid)).fetchone():
        return "Вы уже активировали этот код."
    conn.execute("INSERT INTO used VALUES(?,?)", (c["code"], uid))
    conn.execute("UPDATE codes SET left=left-1 WHERE code=?", (c["code"],))
    if c["kind"] == "petals":
        conn.execute("UPDATE users SET petals=petals+? WHERE id=?", (c["amt"], uid))
        msg = f"🌸 +{c['amt']} Petals"
    else:
        conn.execute("UPDATE users SET free_cases=free_cases+1 WHERE id=?", (uid,))
        msg = "📦 Бесплатный кейс со всеми питомцами"
    conn.commit()
    return "🎟 Код принят! " + msg


def referral(uid: int, uname, inviter: int):
    """Реферальная ссылка: один раз для нового игрока, бесплатный кейс обоим."""
    if inviter == uid:
        return "Нельзя пригласить самого себя."
    if conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return "Реферальная ссылка работает только для новых игроков."
    if not conn.execute("SELECT 1 FROM users WHERE id=?", (inviter,)).fetchone():
        return None
    touch(uid, uname)
    conn.execute("UPDATE users SET free_cases=free_cases+1, referred=? WHERE id=?", (inviter, uid))
    conn.execute("UPDATE users SET free_cases=free_cases+1, bonus_spins=bonus_spins+1 WHERE id=?", (inviter,))
    conn.commit()
    return "🎁 Вы пришли по приглашению: бесплатный кейс со всеми питомцами уже в приложении!"


def due_wheel_users() -> list:
    now = time.time()
    rows = conn.execute("SELECT id FROM users WHERE last_spin>0 AND last_spin+?<=? AND notified<last_spin",
                        (COOLDOWN, now)).fetchall()
    return [r["id"] for r in rows]


def mark_notified(uid: int) -> None:
    conn.execute("UPDATE users SET notified=? WHERE id=?", (time.time(), uid))
    conn.commit()


def get_post(pid: int):
    return conn.execute("SELECT * FROM trade_posts WHERE id=?", (pid,)).fetchone()


def resolve_request(rid: int, ok: bool):
    """Админ принял (ok) или отклонил запрос; при отклонении питомец возвращается."""
    r = conn.execute("SELECT * FROM requests WHERE id=? AND status='open'", (rid,)).fetchone()
    if not r:
        return None
    conn.execute("UPDATE requests SET status=? WHERE id=?", ("done" if ok else "declined", rid))
    if not ok:
        add_pet(r["uid"], r["pet"])
    conn.commit()
    return r


# ---- HTTP ----
def verify(init_data: str, token: str):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def make_app(token: str, admin_id: int, notify=None, notify_post=None, broadcast=None) -> web.Application:
    @web.middleware
    async def mw(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        elif not request.path.startswith("/api/"):
            return await handler(request)
        else:
            try:
                auth = request.headers.get("Authorization", "")
                user = verify(auth[4:], token) if auth.startswith("tma ") else None
                if not user:
                    raise web.HTTPUnauthorized(text=json.dumps({"error": "Откройте приложение из Telegram"}))
                request["user"] = user
                row = touch(user["id"], user.get("username"))
                if row["banned"] and user["id"] != admin_id:
                    raise web.HTTPForbidden(text=json.dumps({"error": "Вы заблокированы администратором"}))
                now = time.time()
                q = RATE.setdefault(user["id"], [])
                q[:] = [t for t in q if now - t < 10]
                if len(q) >= 40:
                    raise web.HTTPTooManyRequests(text=json.dumps({"error": "Слишком часто, подождите несколько секунд"}))
                q.append(now)
                conn.execute("INSERT OR IGNORE INTO activity VALUES(?,?)", (time.strftime("%Y-%m-%d"), user["id"]))
                conn.commit()
                resp = await handler(request)
            except web.HTTPException as e:
                resp = web.Response(status=e.status, text=e.text or json.dumps({"error": e.reason}),
                                    content_type="application/json")
        resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type",
                             "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})
        return resp

    def fail(msg):
        raise web.HTTPBadRequest(text=json.dumps({"error": msg}))

    async def me(request):
        uid = request["user"]["id"]
        return web.json_response({"state": state_of(uid), "cap": wheel_cap(), "theme": active_theme(), "themePick": theme_pick(), "promo": promo_info()})

    async def spin(request):
        uid = request["user"]["id"]
        r = conn.execute("SELECT last_spin, bonus_spins FROM users WHERE id=?", (uid,)).fetchone()
        use_bonus = False
        if time.time() < r["last_spin"] + COOLDOWN:
            if (r["bonus_spins"] or 0) > 0:
                use_bonus = True
            else:
                fail("Колесо ещё не готово")
        idx = random.choices(range(len(WHEEL)), weights=W_WEIGHTS)[0]
        t, v, _ = WHEEL[idx]
        out = {"idx": idx}
        if t == "p":
            gain = max(1, int(wheel_cap() * SCALE[v] + 0.5)) * (promo_info() or {"mult": 1})["mult"]
            conn.execute("UPDATE users SET petals=petals+? WHERE id=?", (gain, uid))
            out["gain"] = gain
        else:
            name = rand_pet(v)
            add_pet(uid, name)
            out["pet"] = {"name": name, "key": v}
        if use_bonus:
            conn.execute("UPDATE users SET bonus_spins=bonus_spins-1 WHERE id=?", (uid,))
        else:
            conn.execute("UPDATE users SET last_spin=? WHERE id=?", (time.time(), uid))
        bump(uid, spins=1)
        conn.commit()
        out["state"] = state_of(uid)
        return web.json_response(out)

    async def case(request):
        uid = request["user"]["id"]
        cid = (await request.json()).get("id")
        if cid not in CASES:
            fail("Нет такого кейса")
        price, odds = CASES[cid]
        if cid == "daily":
            last = conn.execute("SELECT daily_at FROM users WHERE id=?", (uid,)).fetchone()["daily_at"] or 0
            if time.time() < last + COOLDOWN:
                fail("Кейс дня уже открыт, приходите позже")
            conn.execute("UPDATE users SET daily_at=? WHERE id=?", (time.time(), uid))
        u = conn.execute("SELECT petals, free_cases FROM users WHERE id=?", (uid,)).fetchone()
        if cid == "free":
            if u["free_cases"] < 1:
                fail("Нет бесплатных кейсов")
            conn.execute("UPDATE users SET free_cases=free_cases-1 WHERE id=?", (uid,))
        else:
            if u["petals"] < price:
                fail("Не хватает Petals")
            conn.execute("UPDATE users SET petals=petals-? WHERE id=?", (price, uid))
        key = pick(odds)
        if re.fullmatch(r"c\d+", key):
            gain = int(key[1:])
            conn.execute("UPDATE users SET petals=petals+? WHERE id=?", (gain, uid))
            name, key = f"{gain} Petals", "coin"
        else:
            name = rand_pet(key, POOLS.get(cid))
            add_pet(uid, name)
        bump(uid, cases=1)
        conn.commit()
        return web.json_response({"win": {"name": name, "key": key}, "state": state_of(uid)})

    def offers_json(uid):
        rows = conn.execute("SELECT * FROM offers ORDER BY id DESC LIMIT 100").fetchall()
        return [{"id": o["id"], "uname": o["uname"] or "игрок", "give": o["give"], "want": o["want"],
                 "mine": o["uid"] == uid} for o in rows]

    async def offers(request):
        return web.json_response({"offers": offers_json(request["user"]["id"])})

    async def offer_new(request):
        uid = request["user"]["id"]
        b = await request.json()
        give, want = str(b.get("give", "")), str(b.get("want", ""))
        if give not in ALL_NAMES or want not in ALL_NAMES or give == want:
            fail("Выберите двух разных питомцев из каталога")
        if conn.execute("SELECT COUNT(*) c FROM offers WHERE uid=?", (uid,)).fetchone()["c"] >= 5:
            fail("Не больше 5 открытых предложений")
        if not add_pet(uid, give, -1):
            fail("У вас нет этого питомца")
        conn.execute("INSERT INTO offers(uid,uname,give,want,created) VALUES(?,?,?,?,?)",
                     (uid, request["user"].get("username"), give, want, time.time()))
        conn.commit()
        return web.json_response({"state": state_of(uid), "offers": offers_json(uid)})

    async def offer_accept(request):
        uid = request["user"]["id"]
        o = conn.execute("SELECT * FROM offers WHERE id=?", ((await request.json()).get("id"),)).fetchone()
        if not o:
            fail("Предложение уже недоступно")
        if o["uid"] == uid:
            fail("Это ваше предложение")
        if not add_pet(uid, o["want"], -1):
            fail("У вас нет нужного питомца")
        add_pet(uid, o["give"])
        add_pet(o["uid"], o["want"])
        conn.execute("DELETE FROM offers WHERE id=?", (o["id"],))
        conn.execute("INSERT INTO trade_log(a_uid,a_name,b_uid,b_name,give,want,created) VALUES(?,?,?,?,?,?,?)",
                     (o["uid"], o["uname"], uid, request["user"].get("username"), o["give"], o["want"], time.time()))
        bump(uid, trades=1)
        bump(o["uid"], trades=1)
        conn.commit()
        return web.json_response({"state": state_of(uid), "offers": offers_json(uid)})

    async def offer_cancel(request):
        uid = request["user"]["id"]
        o = conn.execute("SELECT * FROM offers WHERE id=? AND uid=?", ((await request.json()).get("id"), uid)).fetchone()
        if not o:
            fail("Предложение не найдено")
        add_pet(uid, o["give"])
        conn.execute("DELETE FROM offers WHERE id=?", (o["id"],))
        conn.commit()
        return web.json_response({"state": state_of(uid), "offers": offers_json(uid)})

    async def sell(request):
        uid = request["user"]["id"]
        b = await request.json()
        name = b.get("name")
        inv = json.loads(conn.execute("SELECT inv FROM users WHERE id=?", (uid,)).fetchone()["inv"])
        if name:
            n = max(1, int(b.get("n", 1)))
            if name not in ALL_NAMES or inv.get(name, 0) < n:
                fail("Нет столько копий")
            items = {name: n}
        else:
            items = {k: v - 1 for k, v in inv.items() if v > 1 and k in ALL_NAMES}
        if not items:
            fail("Нет дублей для продажи")
        total = sum(SELL[ALL_NAMES[k]] * n for k, n in items.items())
        for k, n in items.items():
            add_pet(uid, k, -n)
        conn.execute("UPDATE users SET petals=petals+? WHERE id=?", (total, uid))
        bump(uid, sells=sum(items.values()))
        conn.commit()
        return web.json_response({"gain": total, "state": state_of(uid)})

    async def upgrade(request):
        uid = request["user"]["id"]
        b = await request.json()
        give, target = str(b.get("give", "")), str(b.get("target", ""))
        try:
            n = int(b.get("n", 1))
        except (TypeError, ValueError):
            n = 0
        if give not in ALL_NAMES or target not in ALL_NAMES or not 1 <= n <= 10:
            fail("Неверные данные")
        if VAL[target] <= VAL[give]:
            fail("Цель должна быть ценнее ставки")
        chance = min(0.75, n * VAL[give] / VAL[target] * 0.9)
        if chance < 0.01:
            fail("Шанс меньше 1%: добавьте копий или выберите цель ближе")
        if not add_pet(uid, give, -n):
            fail("Не хватает копий")
        win = random.random() < chance
        if win:
            add_pet(uid, target)
        bump(uid, upgrades=1)
        conn.commit()
        return web.json_response({"win": win, "chance": chance, "state": state_of(uid)})

    async def top(request):
        rows = []
        for r in conn.execute("SELECT username, inv FROM users").fetchall():
            inv = json.loads(r["inv"])
            rows.append((len(inv), sum(inv.values()), r["username"] or "игрок"))
        rows.sort(reverse=True)
        return web.json_response({"top": [{"name": r[2], "uniq": r[0], "total": r[1]} for r in rows[:10]]})

    async def pet_request(request):
        uid = request["user"]["id"]
        b = await request.json()
        pet, rb = str(b.get("pet", "")), str(b.get("roblox", "")).strip()
        if pet not in ALL_NAMES or not re.match(r"^[A-Za-z0-9_]{3,20}$", rb):
            fail("Выберите питомца и введите ник Roblox (латиница, цифры, _)")
        if conn.execute("SELECT COUNT(*) c FROM requests WHERE uid=? AND status='open'", (uid,)).fetchone()["c"] >= 3:
            fail("Не больше 3 открытых запросов")
        if not add_pet(uid, pet, -1):
            fail("У вас нет этого питомца")
        uname = request["user"].get("username")
        cur = conn.execute("INSERT INTO requests(uid,uname,roblox,pet,created) VALUES(?,?,?,?,?)",
                           (uid, uname, rb, pet, time.time()))
        conn.commit()
        rid = cur.lastrowid
        if notify:
            text = f"📨 Запрос #{rid}\nИгрок: @{uname or uid}\nПитомец: {pet} ({ALL_NAMES[pet]})\nНик Roblox: {rb}"
            if not await notify(rid, uid, text):
                resolve_request(rid, False)
                fail("Не удалось отправить запрос админу")
        return web.json_response({"state": state_of(uid)})

    async def trade_post(request):
        uid = request["user"]["id"]
        b = await request.json()
        give, want, note = str(b.get("give", "")), str(b.get("want", "")), str(b.get("note", ""))[:80]
        if give not in ALL_NAMES or want not in ALL_NAMES or give == want:
            fail("Выберите двух разных питомцев из каталога")
        inv = json.loads(conn.execute("SELECT inv FROM users WHERE id=?", (uid,)).fetchone()["inv"])
        if inv.get(give, 0) < 1:
            fail("У вас нет питомца, которого вы предлагаете")
        if conn.execute("SELECT COUNT(*) c FROM trade_posts WHERE uid=? AND created>?", (uid, time.time() - 86400)).fetchone()["c"] >= 3:
            fail("Не больше 3 публикаций в сутки")
        uname = request["user"].get("username")
        cur = conn.execute("INSERT INTO trade_posts(uid,uname,give,want,note,created) VALUES(?,?,?,?,?,?)",
                           (uid, uname, give, want, note, time.time()))
        conn.commit()
        pid = cur.lastrowid
        text = f"🔁 Обмен #{pid}\nОтдаёт: {give} ({ALL_NAMES[give]})\nХочет: {want} ({ALL_NAMES[want]})" + (f"\n💬 {note}" if note else "")
        if not notify_post or not await notify_post(pid, text):
            conn.execute("DELETE FROM trade_posts WHERE id=?", (pid,))
            conn.commit()
            fail("Канал для обменов пока не подключён")
        return web.json_response({"ok": True})

    def need_admin(request):
        if not admin_id or request["user"]["id"] != admin_id:
            raise web.HTTPForbidden(text=json.dumps({"error": "Только для администратора"}))

    async def admin_theme(request):
        need_admin(request)
        t = (await request.json()).get("theme")
        if t is not None and not (isinstance(t, list) and len(t) in (3, 4) and all(isinstance(x, int) for x in t)):
            fail("Неверная тема")
        set_setting("theme", json.dumps(t))
        log_admin("Тема для всех: " + (str(t) if t else "выключена"))
        return web.json_response({"ok": True})

    async def admin_theme_pick(request):
        need_admin(request)
        on = bool((await request.json()).get("on"))
        set_setting("theme_pick", "1" if on else "0")
        log_admin("Выбор тем игроками: " + ("включён" if on else "выключен"))
        return web.json_response({"ok": True})

    async def admin_log_view(request):
        need_admin(request)
        rows = conn.execute("SELECT text FROM admin_log ORDER BY id DESC LIMIT 20").fetchall()
        return web.json_response({"log": [{"text": r["text"]} for r in rows]})

    async def remind(request):
        uid = request["user"]["id"]
        step = {"seed": 300, "egg": 1800}.get((await request.json()).get("kind"))
        if not step:
            fail("Неверный тип")
        if conn.execute("SELECT COUNT(*) c FROM reminders WHERE uid=?", (uid,)).fetchone()["c"] >= 3:
            fail("Не больше 3 напоминаний сразу")
        at = (int(time.time() // step) + 1) * step
        conn.execute("INSERT INTO reminders(uid,at,kind) VALUES(?,?,?)", (uid, at, "seed" if step == 300 else "egg"))
        conn.commit()
        return web.json_response({"at": int(at * 1000)})

    async def index(request):
        return web.FileResponse(BASE / "index.html")

    async def use_ref(request):
        uid = request["user"]["id"]
        code = str((await request.json()).get("code", "")).strip().upper()
        me_row = conn.execute("SELECT referred, ref_code FROM users WHERE id=?", (uid,)).fetchone()
        if me_row["referred"]:
            fail("Вы уже вводили реферальный код. Повторно нельзя.")
        inv = conn.execute("SELECT id, referred FROM users WHERE ref_code=?", (code,)).fetchone()
        if not inv or inv["id"] == uid or inv["referred"] == uid:
            fail("Неверный код")
        conn.execute("UPDATE users SET referred=?, free_cases=free_cases+1 WHERE id=?", (inv["id"], uid))
        conn.execute("UPDATE users SET free_cases=free_cases+1, bonus_spins=bonus_spins+1 WHERE id=?", (inv["id"],))
        conn.commit()
        return web.json_response({"state": state_of(uid)})

    async def history(request):
        uid = request["user"]["id"]
        out = []
        for t in conn.execute("SELECT * FROM trade_log WHERE a_uid=? OR b_uid=? ORDER BY id DESC LIMIT 20", (uid, uid)).fetchall():
            mine_a = t["a_uid"] == uid
            rv = conn.execute("SELECT 1 FROM reviews WHERE tid=? AND from_uid=?", (t["id"], uid)).fetchone()
            out.append({"id": t["id"], "other": (t["b_name"] if mine_a else t["a_name"]) or "игрок",
                        "gave": t["give"] if mine_a else t["want"], "got": t["want"] if mine_a else t["give"], "reviewed": bool(rv)})
        return web.json_response({"history": out})

    async def review(request):
        uid = request["user"]["id"]
        b = await request.json()
        t = conn.execute("SELECT * FROM trade_log WHERE id=?", (int(b.get("tid", 0)),)).fetchone()
        if not t or uid not in (t["a_uid"], t["b_uid"]):
            fail("Обмен не найден")
        if conn.execute("SELECT 1 FROM reviews WHERE tid=? AND from_uid=?", (t["id"], uid)).fetchone():
            fail("Вы уже оставили отзыв")
        other = t["b_uid"] if uid == t["a_uid"] else t["a_uid"]
        conn.execute("INSERT INTO reviews VALUES(?,?,?,?)", (t["id"], uid, other, 1 if b.get("up") else 0))
        conn.commit()
        return web.json_response({"ok": True})

    async def complain(request):
        uid = request["user"]["id"]
        b = await request.json()
        target, text = str(b.get("target", ""))[:40], str(b.get("text", ""))[:200]
        if not text.strip():
            fail("Опишите жалобу")
        if conn.execute("SELECT COUNT(*) c FROM complaints WHERE uid=? AND created>?", (uid, time.time() - 86400)).fetchone()["c"] >= 5:
            fail("Не больше 5 жалоб в сутки")
        conn.execute("INSERT INTO complaints(uid,uname,target,text,created) VALUES(?,?,?,?,?)",
                     (uid, request["user"].get("username"), target, text, time.time()))
        conn.commit()
        return web.json_response({"ok": True})

    async def admin_ban(request):
        need_admin(request)
        b = await request.json()
        if not set_ban(str(b.get("username", "")), bool(b.get("ban"))):
            fail("Игрок не найден")
        log_admin(("Бан " if b.get("ban") else "Разбан ") + "@" + str(b.get("username")))
        return web.json_response({"ok": True})

    async def admin_complaints(request):
        need_admin(request)
        rows = conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 15").fetchall()
        return web.json_response({"items": [{"from": r["uname"] or r["uid"], "target": r["target"], "text": r["text"]} for r in rows]})

    async def admin_broadcast(request):
        need_admin(request)
        text = str((await request.json()).get("text", "")).strip()[:500]
        if not text or not broadcast:
            fail("Нет текста или рассылка недоступна")
        n = await broadcast(text)
        log_admin(f"Рассылка ({n} получателей): {text[:40]}")
        return web.json_response({"sent": n})

    async def admin_days(request):
        need_admin(request)
        res = []
        for i in range(13, -1, -1):
            d = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * i))
            acts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, n FROM actions WHERE day=?", (d,)).fetchall()}
            res.append({"day": d[5:], "dau": conn.execute("SELECT COUNT(*) c FROM activity WHERE day=?", (d,)).fetchone()["c"],
                        "spins": acts.get("spins", 0), "cases": acts.get("cases", 0), "trades": acts.get("trades", 0)})
        return web.json_response({"days": res})

    async def admin_suspicious(request):
        need_admin(request)
        rows = []
        for r in conn.execute("SELECT username, petals, stats FROM users").fetchall():
            st = json.loads(r["stats"] or "{}")
            rows.append((r["petals"] / (st.get("spins", 0) * 20 + 50), r["username"] or "—", r["petals"], st.get("spins", 0)))
        rows.sort(reverse=True)
        return web.json_response({"items": [{"name": x[1], "petals": x[2], "spins": x[3]} for x in rows[:10]]})

    async def admin_promo(request):
        need_admin(request)
        b = await request.json()
        mult, hours = int(b.get("mult", 1)), float(b.get("hours", 0))
        if mult <= 1 or not 0 < hours <= 24 * 14 or mult > 10:
            set_setting("promo", "null")
            log_admin("Акция выключена")
        else:
            set_setting("promo", json.dumps({"mult": mult, "until": time.time() + hours * 3600}))
            log_admin(f"Акция ×{mult} на {hours} ч")
        return web.json_response({"ok": True})

    async def admin_schedule(request):
        need_admin(request)
        b = await request.json()
        lst = json.loads(get_setting("schedule") or "[]")
        act = b.get("action")
        if act == "clear":
            lst = []
        elif act == "add":
            ok = all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(b.get(k, ""))) for k in ("from", "to"))
            t = b.get("theme")
            if not ok or not (isinstance(t, list) and len(t) in (3, 4) and all(isinstance(x, int) for x in t)) or len(lst) >= 10:
                fail("Неверные даты, тема или слишком много записей")
            lst.append({"from": b["from"], "to": b["to"], "theme": t})
        set_setting("schedule", json.dumps(lst))
        if act in ("add", "clear"):
            log_admin("Расписание тем: " + act)
        return web.json_response({"items": lst})

    async def admin_stats(request):
        if not admin_id or request["user"]["id"] != admin_id:
            raise web.HTTPForbidden(text=json.dumps({"error": "Только для администратора"}))
        rows = conn.execute("SELECT petals, stats FROM users").fetchall()
        st = [json.loads(r["stats"] or "{}") for r in rows]
        return web.json_response({
            "players": len(rows), "petals": sum(r["petals"] for r in rows),
            "spins": sum(x.get("spins", 0) for x in st), "cases": sum(x.get("cases", 0) for x in st),
            "trades": sum(x.get("trades", 0) for x in st),
            "offers": conn.execute("SELECT COUNT(*) c FROM offers").fetchone()["c"],
            "requests": conn.execute("SELECT COUNT(*) c FROM requests WHERE status='open'").fetchone()["c"]})

    async def admin_give(request):
        if not admin_id or request["user"]["id"] != admin_id:
            raise web.HTTPForbidden(text=json.dumps({"error": "Только для администратора"}))
        b = await request.json()
        n = int(b.get("amount", 0))
        if not 1 <= n <= 100000 or not credit(str(b.get("username", "")), petals=n):
            fail("Игрок не найден (он должен открыть приложение) или неверная сумма")
        log_admin(f"Выдано {n} Petals игроку @{b.get('username')}")
        return web.json_response({"ok": True, "state": state_of(admin_id)})

    app = web.Application(middlewares=[mw])
    app.add_routes([web.get("/api/me", me), web.post("/api/spin", spin), web.post("/api/case", case),
                    web.get("/api/offers", offers), web.post("/api/offers/new", offer_new),
                    web.post("/api/offers/accept", offer_accept), web.post("/api/offers/cancel", offer_cancel),
                    web.post("/api/sell", sell), web.post("/api/upgrade", upgrade), web.get("/api/top", top),
                    web.post("/api/ref", use_ref), web.get("/api/history", history), web.post("/api/review", review),
                    web.post("/api/complain", complain), web.post("/api/admin/ban", admin_ban), web.get("/api/admin/complaints", admin_complaints),
                    web.post("/api/admin/broadcast", admin_broadcast), web.get("/api/admin/days", admin_days),
                    web.get("/api/admin/suspicious", admin_suspicious), web.post("/api/admin/promo", admin_promo),
                    web.post("/api/admin/schedule", admin_schedule), web.post("/api/request", pet_request), web.post("/api/post", trade_post), web.post("/api/admin/theme", admin_theme), web.post("/api/admin/themepick", admin_theme_pick), web.get("/api/admin/log", admin_log_view),
                    web.post("/api/remind", remind), web.get("/", index), web.get("/api/admin/stats", admin_stats),
                    web.post("/api/admin/give", admin_give), web.options("/{p:.*}", lambda r: web.Response())])
    return app