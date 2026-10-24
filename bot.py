import logging
logging.basicConfig(level=logging.INFO)
import asyncio, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo

from server import billing, settings

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher()
URL = os.environ["WEBAPP_URL"]  # публичный https-адрес сервера
DB = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "app.db"))  # тот же файл, что у сервера
ADMIN = int(os.getenv("ADMIN_ID", "0"))  # ваш Telegram id: команда /refund


def db():
    c = sqlite3.connect(DB, isolation_level=None)
    return c


@dp.message(CommandStart())
async def start(m: Message):
    arg = (m.text or "").partition(" ")[2]
    if arg.startswith("ref") and arg[3:].isdigit():  # пришёл по ссылке друга: бонус обоим, если это новый пользователь
        c = db()
        try:
            with billing.transaction(c):
                isnew = c.execute("SELECT 1 FROM users WHERE id=?", (m.from_user.id,)).fetchone() is None
                referred = False
                if isnew:
                    now = int(time.time())
                    c.execute("INSERT INTO users(id, name, hearts_ts, created_ts) VALUES(?,?,?,?)",
                              (m.from_user.id, m.from_user.first_name or "Ученик", now, now))
                    referred = billing.referral(c, m.from_user.id, int(arg[3:]))
            if isnew:
                billing.notify_new_user(os.environ["BOT_TOKEN"], ADMIN, m.from_user.id,
                                        m.from_user.first_name, m.from_user.username)
            if referred:
                await m.answer(f"Вы пришли по приглашению: +{settings.cfg('referral_bonus_days')} дн. доступа вам и вашему другу.")
        finally:
            c.close()
    await bot.set_chat_menu_button(chat_id=m.chat.id, menu_button=MenuButtonWebApp(text="Учиться", web_app=WebAppInfo(url=URL)))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Начать учиться", web_app=WebAppInfo(url=URL))]])
    await m.answer("Готовься к ЦТ и ЦЭ по 10 минут в день: задания с разбором, варианты прошлых лет, работа над ошибками.", reply_markup=kb)


@dp.message(Command("paysupport"))
async def paysupport(m: Message):  # Telegram требует эту команду у ботов, принимающих оплату
    await m.answer("Вопросы по оплате и возврату звёзд: напишите сюда сообщением с описанием проблемы, мы ответим. "
                   "Если оплата прошла, а доступа нет, пришлите номер транзакции из чека Telegram.")


@dp.message(Command("myid"))
async def my_id(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    payload = billing.parse_invoice_payload(q.invoice_payload)
    ok = bool(payload and payload[0] == q.from_user.id and payload[1] == q.total_amount
              and payload[1] == int(settings.cfg("price_stars"))
              and payload[2] == int(settings.cfg("paid_days")) and q.currency == "XTR")
    await q.answer(ok=ok, error_message=None if ok else "Счёт не подходит. Откройте оплату заново из приложения.")


@dp.message(F.successful_payment)
async def paid(m: Message):
    p = m.successful_payment
    payload = billing.parse_invoice_payload(p.invoice_payload)
    if not payload or payload[0] != m.from_user.id or payload[1] != p.total_amount or p.currency != "XTR":
        logging.error("отклонено платежное уведомление с несовпадающими данными для user %s", m.from_user.id)
        await m.answer("Не удалось подтвердить параметры платежа. Напишите в поддержку /paysupport.")
        return
    c = db()
    try:
        until = billing.grant(c, m.from_user.id, p.total_amount, p.telegram_payment_charge_id,
                              m.from_user.first_name or "Ученик", days=payload[2])
    finally:
        c.close()
    logging.info("оплата %s: %s звёзд, charge %s, срок до %s", m.from_user.id, p.total_amount,
                 p.telegram_payment_charge_id, until)
    await m.answer("Оплата получена, спасибо! Доступ открыт. Возвращайтесь в приложение.")


@dp.message(Command("refund"))
async def refund(m: Message):  # /refund <id пользователя> <charge_id>
    if not ADMIN or m.from_user.id != ADMIN:
        return
    try:
        uid, charge = m.text.split()[1:3]
        c = db()
        try:
            payment = c.execute("SELECT user_id FROM payments WHERE charge_id=?", (charge,)).fetchone()
            if not payment or payment[0] != int(uid):
                raise ValueError("Платёж не найден или принадлежит другому пользователю")
        finally:
            c.close()
        await bot.refund_star_payment(user_id=int(uid), telegram_payment_charge_id=charge)
        c = db()
        try:
            billing.revoke(c, charge)
        finally:
            c.close()
        await m.answer("Звёзды возвращены, доступ отозван.")
    except Exception as e:
        await m.answer(f"Не вышло: {e}")


async def reminders():
    """Раз в час: в заданный час (по Минску) напоминает о непроработанных ошибках. Не чаще раза в сутки."""
    while True:
        await asyncio.sleep(600)
        try:
            s = settings.get()
            minsk = datetime.now(timezone.utc) + timedelta(hours=3)
            if not s["reminder_enabled"] or minsk.hour != int(s["reminder_hour"]):
                continue
            day0 = int(minsk.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()) - 3 * 3600
            c = db()
            try:
                now = int(time.time())
                rows = c.execute("""SELECT u.id, COUNT(*) FROM users u JOIN errors e ON e.user_id=u.id
                                    WHERE (e.status!='mastered' OR e.next_review<=?) AND u.last_remind<?
                                    GROUP BY u.id""", (now, day0)).fetchall()
                for uid, n in rows:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Проработать ошибки", web_app=WebAppInfo(url=URL))]])
                    try:
                        await bot.send_message(uid, f"У вас непроработанных ошибок: {n}. Семь минут, и они закрыты.", reply_markup=kb)
                    except Exception as e:  # пользователь заблокировал бота
                        logging.info("напоминание %s: %s", uid, e)
                    c.execute("UPDATE users SET last_remind=? WHERE id=?", (int(time.time()), uid))
            finally:
                c.close()
        except Exception:
            logging.exception("reminders")


async def main():
    asyncio.create_task(reminders())
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Учиться", web_app=WebAppInfo(url=URL)))
    await dp.start_polling(bot)


asyncio.run(main())
