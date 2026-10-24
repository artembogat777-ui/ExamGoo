"""Доступ и оплата. Общий для сервера и бота: оба работают с одним файлом app.db.
Условия (пробный срок, цена, срок оплаты) берутся из settings.json на лету.
Оплата записывается ТОЛЬКО ботом после successful_payment: клиент сам доступ себе выдать не может."""
import json, math, time, urllib.request
from contextlib import contextmanager

from server import settings

FOREVER = 4102444800  # 2100-01-01: «оплачено навсегда»
PAYMENTS = """CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INT, stars INT, ts INT,
  since INT, until INT, refunded INT DEFAULT 0)"""


@contextmanager
def transaction(c):
    savepoint = "billing_operation"
    nested = c.in_transaction
    c.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        c.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
    except Exception:
        if nested:
            c.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            c.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            c.execute("ROLLBACK")
        raise


def _days(sec):
    return max(1, math.ceil(sec / 86400))


def invoice_payload(uid, stars, days):
    return f"sub:{int(uid)}:{int(stars)}:{int(days)}"


def parse_invoice_payload(payload):
    try:
        product, uid, stars, days = payload.split(":")
        values = int(uid), int(stars), int(days)
    except (AttributeError, TypeError, ValueError):
        return None
    if product != "sub" or values[0] <= 0 or values[1] <= 0 or values[2] < 0:
        return None
    return values


FEATURES = ("practice", "sborniki", "exam", "errors", "hints", "score")


def access(c, uid, now=None):
    """state: free|paid|trial|expired. ok: полная версия. features: что открыто. daily_limit: заданий в день (None = без лимита)."""
    s, now = settings.get(), now or int(time.time())
    price = s["price_stars"]
    full = {f: True for f in FEATURES}
    res = lambda state, days=None: {"state": state, "ok": state != "expired", "days_left": days, "price": price,
                                    "features": full if state != "expired" else {**{f: bool(s["free_plan"].get(f)) for f in FEATURES}, "practice": True},
                                    "daily_limit": None if state != "expired" else (int(s["free_plan"].get("daily_tasks") or 0) or None)}
    if not s["paywall_enabled"]:
        return res("free")
    u = c.execute("SELECT created_ts, trial_until, paid_until, bonus_until FROM users WHERE id=?", (uid,)).fetchone()
    created, trial, paid, bonus = (u[0] or now, u[1] or 0, u[2] or 0, u[3] or 0) if u else (now, 0, 0, 0)
    if paid > now:
        return res("paid", None if paid >= FOREVER else _days(paid - now))
    if not trial:
        trial = created + int(s["trial_days"]) * 86400
        c.execute("UPDATE users SET trial_until=? WHERE id=? AND trial_until=0", (trial, uid))
    end = max(trial, bonus)
    if end > now:
        return res("trial", _days(end - now))
    return res("expired", 0)


def add_days(c, uid, days):
    """Бонусные дни (например, за приглашённого друга): продлевают пробный период."""
    now = int(time.time())
    row = c.execute("SELECT created_ts, trial_until, bonus_until FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return
    trial = row[1] or (row[0] or now) + int(settings.cfg("trial_days")) * 86400
    base = max(now, row[2] or 0, trial)
    c.execute("UPDATE users SET bonus_until=? WHERE id=?", (base + days * 86400, uid))


def referral(c, new_uid, ref_uid):
    """Новый пользователь пришёл по ссылке друга: бонус обоим, один раз. -> True, если начислено."""
    days = int(settings.cfg("referral_bonus_days"))
    with transaction(c):
        c.execute("CREATE TABLE IF NOT EXISTS referrals(user_id INTEGER PRIMARY KEY, referrer INT, ts INT)")
        if days <= 0 or new_uid == ref_uid or c.execute("SELECT 1 FROM referrals WHERE user_id=?", (new_uid,)).fetchone():
            return False
        if not c.execute("SELECT 1 FROM users WHERE id=?", (ref_uid,)).fetchone():
            return False
        c.execute("INSERT INTO referrals VALUES(?,?,?)", (new_uid, ref_uid, int(time.time())))
        add_days(c, new_uid, days)
        add_days(c, ref_uid, days)
        return True


def notify(token, chat_id, text):
    """Сообщение от бота (жалобы на задания администратору). Ошибки не критичны."""
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", json.dumps({"chat_id": chat_id, "text": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        print("notify:", e)


def notify_new_user(token, admin_id, uid, name, username=None):
    """Reports a newly registered Telegram user to the configured app owner."""
    if not token or int(admin_id or 0) <= 0:
        return
    safe_name = " ".join(str(name or "Ученик").split())[:80]
    safe_username = " ".join(str(username or "").split())[:64]
    handle = f"@{safe_username}" if safe_username else "не указан"
    notify(token, int(admin_id), f"Новый пользователь Mini App\nИмя: {safe_name}\nНик: {handle}\nTelegram ID: {int(uid)}")


def grant(c, uid, stars, charge_id, name="Ученик", days=None):
    """Продлевает доступ. Повторное уведомление с тем же charge_id ничего не меняет (возвращает None)."""
    if int(uid) <= 0 or int(stars) <= 0 or not charge_id:
        raise ValueError("Некорректные данные платежа")
    with transaction(c):
        c.execute(PAYMENTS)
        if c.execute("SELECT 1 FROM payments WHERE charge_id=?", (charge_id,)).fetchone():
            return None
        now = int(time.time())
        days = int(settings.cfg("paid_days")) if days is None else int(days)
        if days < 0:
            raise ValueError("Срок доступа не может быть отрицательным")
        c.execute("INSERT OR IGNORE INTO users(id, name, hearts_ts, created_ts) VALUES(?,?,?,?)", (uid, name, now, now))
        cur = c.execute("SELECT paid_until FROM users WHERE id=?", (uid,)).fetchone()[0] or 0
        since = max(now, cur)
        until = FOREVER if days <= 0 else since + days * 86400
        c.execute("INSERT INTO payments(charge_id, user_id, stars, ts, since, until) VALUES(?,?,?,?,?,?)",
                  (charge_id, uid, stars, now, since, until))
        c.execute("UPDATE users SET paid_until=? WHERE id=?", (until, uid))
        return until


def revoke(c, charge_id):
    """Возврат звёзд: убирает оплаченный период. -> id пользователя или None."""
    with transaction(c):
        p = c.execute("SELECT user_id, since, until, refunded FROM payments WHERE charge_id=?", (charge_id,)).fetchone()
        if not p or p[3]:
            return None
        updated = c.execute("UPDATE payments SET refunded=1 WHERE charge_id=? AND refunded=0", (charge_id,))
        if updated.rowcount != 1:
            return None
        if p[2] >= FOREVER:
            c.execute("UPDATE users SET paid_until=? WHERE id=?", (int(time.time()), p[0]))
        else:
            c.execute("UPDATE users SET paid_until=MAX(0, paid_until-?) WHERE id=?", (p[2] - p[1], p[0]))
        return p[0]


def create_invoice_link(token, uid):
    """Ссылка на счёт в звёздах (XTR) для WebApp.openInvoice. Для цифровых товаров Telegram требует именно звёзды."""
    s = settings.get()
    days, stars = int(s["paid_days"]), int(s["price_stars"])
    body = {"title": "Доступ к тренажёру", "description": f"Все задания, экзамены и разбор ошибок: {days} дн." if days > 0 else "Все задания, экзамены и разбор ошибок без ограничения по времени",
            "payload": invoice_payload(uid, stars, days), "provider_token": "", "currency": "XTR",
            "prices": [{"label": "Доступ", "amount": stars}]}
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/createInvoiceLink", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "Telegram отклонил запрос"))
    return d["result"]
