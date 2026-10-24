import hashlib, hmac, json, os, random, re, sqlite3, time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Union
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from server import billing, sborniki
from server.settings import cfg

ROOT = Path(__file__).resolve().parent.parent
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV = os.getenv("DEV") == "1"  # без Telegram: пользователь №1
DB = os.getenv("DB_PATH", str(ROOT / "app.db"))
COIN_OK, SHIELD_PRICE = 1, 50
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # ваш Telegram id: сюда приходят жалобы на задания
ADMIN_PANEL_TOKEN = os.getenv("ADMIN_PANEL_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")  # для ссылки «поделиться»
DAY = 60 if DEV else 86400  # в DEV «день» = минута, чтобы проверять архив ошибок
ERR_SQL = """SELECT t.* FROM errors e JOIN tasks t ON t.id=e.task_id WHERE e.user_id=? AND e.status!='mastered'
              AND e.next_review<=? ORDER BY e.next_review LIMIT ?"""

BADGES = [  # code, название, описание, условие(user, верных_всего, идеальная_сессия)
    ("first", "Первый шаг", "Верно решите первое задание", lambda *values: values[1] >= 1),
    ("streak3", "Три дня подряд", "Серия 3 дня", lambda *values: values[0]["best_streak"] >= 3),
    ("streak7", "Неделя без пропусков", "Серия 7 дней", lambda *values: values[0]["best_streak"] >= 7),
    ("streak30", "Месяц упорства", "Серия 30 дней", lambda *values: values[0]["best_streak"] >= 30),
    ("xp500", "500 XP", "Наберите 500 XP", lambda *values: values[0]["xp"] >= 500),
    ("solved100", "Сотня", "100 верных ответов", lambda *values: values[1] >= 100),
    ("perfect", "Без ошибок", "Сессия из 5+ заданий без ошибок", lambda *values: values[2]),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, xp INT DEFAULT 0, coins INT DEFAULT 0,
  streak INT DEFAULT 0, best_streak INT DEFAULT 0, last_day TEXT, shields INT DEFAULT 1,
  hearts INT DEFAULT 5, hearts_ts INT, goal INT DEFAULT 30);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, exam TEXT, subject TEXT, topic TEXT, level INT, type TEXT,
  question TEXT, options TEXT, answer TEXT, explanation TEXT, source TEXT, verified INT DEFAULT 0, distractors TEXT DEFAULT '{}', passage TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS attempts(user_id INT, task_id TEXT, correct INT, ts INT);
CREATE TABLE IF NOT EXISTS revealed_answers(user_id INT, task_id TEXT, ts INT, PRIMARY KEY(user_id, task_id));
CREATE TABLE IF NOT EXISTS xp_log(user_id INT, day TEXT, xp INT);
CREATE TABLE IF NOT EXISTS badges(user_id INT, code TEXT, ts INT, PRIMARY KEY(user_id, code));
CREATE TABLE IF NOT EXISTS errors(user_id INT, task_id TEXT, mistakes INT DEFAULT 1, stage INT DEFAULT 0,
  next_review INT, status TEXT DEFAULT 'failed', PRIMARY KEY(user_id, task_id));
CREATE TABLE IF NOT EXISTS exams(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, year INT, variant INT, started INT,
  deadline INT DEFAULT 0, finished INT DEFAULT 0, correct INT DEFAULT 0, total INT DEFAULT 0, points REAL DEFAULT 0,
  max_points REAL DEFAULT 0, score INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, task_id TEXT, note TEXT, ts INT);
CREATE TABLE IF NOT EXISTS exam_answers(exam_id INT, task_id TEXT, answer TEXT, correct INT, PRIMARY KEY(exam_id, task_id));
CREATE TABLE IF NOT EXISTS xp_claims(user_id INT, task_id TEXT, day TEXT, PRIMARY KEY(user_id, task_id, day));
CREATE TABLE IF NOT EXISTS perfect_claims(user_id INT, day TEXT, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS subject_scores(user_id INT, subject TEXT, score INT, attempts INT DEFAULT 0,
    correct INT DEFAULT 0, updated_ts INT, PRIMARY KEY(user_id, subject));
CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, subject TEXT,
    task_ids TEXT, attempt_start_id INT, started INT, finished INT DEFAULT 0, score INT);
""" + billing.PAYMENTS + ";"

ADD = {  # колонки, появившиеся позже: добавляются в уже существующую app.db без потери данных
    "tasks": [("year", "INT DEFAULT 0"), ("variant", "INT DEFAULT 0"), ("num", "TEXT DEFAULT ''"), ("part", "TEXT DEFAULT ''"),
              ("hints", "TEXT DEFAULT '[]'"), ("note", "TEXT DEFAULT ''")],
    "users": [("created_ts", "INT DEFAULT 0"), ("trial_until", "INT DEFAULT 0"), ("paid_until", "INT DEFAULT 0"), ("bonus_until", "INT DEFAULT 0"), ("last_remind", "INT DEFAULT 0")],
    "exams": [("subject", "TEXT DEFAULT 'rus'")],
}


def today():  # день по Минску (UTC+3)
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def migrate(c):
    for tbl, cols in ADD.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {name} {decl}")
    c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_attempts_user_ts ON attempts(user_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_attempts_user_task_correct ON attempts(user_id, task_id, correct);
        CREATE INDEX IF NOT EXISTS idx_tasks_subject_variant ON tasks(subject, year, variant, verified, part, num);
        CREATE INDEX IF NOT EXISTS idx_errors_user_review ON errors(user_id, status, next_review);
        CREATE INDEX IF NOT EXISTS idx_exams_user_subject ON exams(user_id, subject, year, variant, finished);
        CREATE INDEX IF NOT EXISTS idx_reports_user_task_ts ON reports(user_id, task_id, ts DESC);
    """)
    c.execute("UPDATE users SET created_ts=? WHERE created_ts=0 OR created_ts IS NULL", (int(time.time()),))  # пробный срок старым пользователям: с момента обновления
    c.execute("UPDATE users SET trial_until=created_ts+? WHERE trial_until=0", (int(cfg("trial_days")) * 86400,))


def put_task(c, t):
    j = lambda v: json.dumps(v, ensure_ascii=False)
    c.execute("""INSERT OR REPLACE INTO tasks(id, exam, subject, topic, level, type, question, options, answer, explanation, source,
                 verified, distractors, passage, year, variant, num, part, hints, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        t["id"], t.get("exam", "ЦТ"), t["subject"], t.get("topic", ""), t.get("level", 1), t["type"],
        t["question"], j(t.get("options", [])), j(t["answer"]), j(t.get("explanation", [])),
        t.get("source", ""), int(t.get("verified", False)), j(t.get("distractors", {})), t.get("passage", ""),
        t.get("year", 0), t.get("variant", 0), t.get("num", ""), t.get("part", ""), j(t.get("hints", [])), t.get("note", "")))


def prune_missing_tasks(c, keep, sources_valid):
    if not sources_valid:
        return
    for (tid,) in c.execute("SELECT id FROM tasks WHERE year>0").fetchall():
        if tid not in keep:
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))


def init_db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)  # на Render БД лежит на диске /var/data
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    migrate(c)
    keep = set()
    source_files = sorted((ROOT / "tasks").rglob("*.json"))
    sources_valid = bool(source_files)
    for f in source_files:
        txt = f.read_text("utf-8-sig")
        if not txt.strip():
            sources_valid = False
            print(f"{f.name}: пустой JSON-файл; удаление заданий из базы пропущено")
            continue
        if txt.lstrip().startswith("["):  # готовый плоский список заданий (формат tools/import_json.py)
            for t in json.loads(txt):
                put_task(c, t)
                keep.add(t["id"])
    rows, notes = sborniki.load_all(ROOT / "tasks")  # файл-объект {year, variants:[...]}: один файл на год, внутри все варианты
    for t in rows:
        put_task(c, t)
        keep.add(t["id"])
    for m in notes:
        print("сборники:", m)
        if "ошибка JSON" in m or "файл не прочитан" in m or "пустой JSON-файл" in m:
            sources_valid = False
    if not sources_valid:
        print("Синхронизация заданий пропущена: есть пустые/ошибочные JSON или нет файлов заданий")
    prune_missing_tasks(c, keep, sources_valid)
    c.commit()
    c.close()


@asynccontextmanager
async def lifespan(_):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def get_db():
    # FastAPI гоняет зависимости и эндпоинты по разным потокам, поэтому check_same_thread=False;
    # autocommit (isolation_level=None), чтобы запись не отставала от ответа клиенту
    c = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
    finally:
        c.close()


@app.get("/health")
def health(c=Depends(get_db)):
    c.execute("SELECT 1")
    return {"status": "ok"}


def is_local_admin_host(request: Request):
    host = (request.url.hostname or "").lower().strip("[]")
    return host in {"localhost", "127.0.0.1", "::1"}


def require_admin(request: Request, x_admin_token: str = Header("")):
    if not is_local_admin_host(request):
        raise HTTPException(404, "Not found")
    if not ADMIN_PANEL_TOKEN or not hmac.compare_digest(x_admin_token, ADMIN_PANEL_TOKEN):
        raise HTTPException(404, "Not found")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request):
    if not is_local_admin_host(request):
        raise HTTPException(404, "Not found")
    return FileResponse(ROOT / "webapp" / "admin.html")


@app.middleware("http")
async def prevent_admin_caching(request: Request, call_next):
    if request.url.path in {"/admin", "/admin.html"} and not is_local_admin_host(request):
        return Response(status_code=404)
    response = await call_next(request)
    if request.url.path == "/admin" or request.url.path.startswith("/api/admin/"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def admin_period(from_date, to_date, default_days=30):
    try:
        end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1) if to_date else datetime.now(timezone.utc) + timedelta(days=1)
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if from_date else end - timedelta(days=default_days)
    except ValueError:
        raise HTTPException(400, "Дата должна быть в формате ГГГГ-ММ-ДД")
    if start >= end:
        raise HTTPException(400, "Начало периода должно быть раньше конца")
    return int(start.timestamp()), int(end.timestamp())


@app.get("/api/admin/overview", dependencies=[Depends(require_admin)])
def admin_overview(from_date: str = Query("", alias="from", max_length=10), to_date: str = Query("", alias="to", max_length=10),
                   subject: str = "", c=Depends(get_db)):
    now, day = int(time.time()), 86400
    start, end = admin_period(from_date, to_date)
    task_filter, task_args = ("AND subject=?", (subject,)) if subject else ("", ())
    user_count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    period_subject = "JOIN tasks pt ON pt.id=pa.task_id" if subject else ""
    period_subject_filter = "AND pt.subject=?" if subject else ""
    period_args = (start, end, subject) if subject else (start, end)
    period = c.execute(f"""SELECT COUNT(*) attempts, COUNT(DISTINCT pa.user_id) active,
        COALESCE(SUM(pa.correct),0) correct FROM attempts pa {period_subject}
        WHERE pa.ts>=? AND pa.ts<? {period_subject_filter}""", period_args).fetchone()
    all_attempts = c.execute("SELECT COUNT(*), COALESCE(SUM(correct),0) FROM attempts").fetchone()
    paid_users = c.execute("SELECT COUNT(*) FROM users WHERE paid_until>?", (now,)).fetchone()[0]
    trial_users = c.execute("SELECT COUNT(*) FROM users WHERE paid_until<=? AND MAX(trial_until, bonus_until)>?", (now, now)).fetchone()[0]
    expired_users = c.execute("SELECT COUNT(*) FROM users WHERE paid_until<=? AND MAX(trial_until, bonus_until)<=?", (now, now)).fetchone()[0]
    task_stats = c.execute(f"SELECT COUNT(*), COALESCE(SUM(verified),0) FROM tasks WHERE 1=1 {task_filter}", task_args).fetchone()
    revenue = c.execute("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=0").fetchone()[0]
    refunded = c.execute("SELECT COALESCE(SUM(stars),0) FROM payments WHERE refunded=1").fetchone()[0]
    trend = c.execute(f"""SELECT date(pa.ts,'unixepoch') day, COUNT(*) attempts,
        COUNT(DISTINCT pa.user_id) users, COALESCE(SUM(pa.correct),0) correct
        FROM attempts pa {period_subject} WHERE pa.ts>=? AND pa.ts<? {period_subject_filter}
        GROUP BY date(pa.ts,'unixepoch') ORDER BY day""", period_args).fetchall()
    subjects = c.execute("""SELECT t.subject, COUNT(DISTINCT t.id) tasks,
        COUNT(DISTINCT CASE WHEN t.verified=1 THEN t.id END) verified,
        COUNT(a.ts) attempts, COALESCE(SUM(a.correct),0) correct,
        COUNT(DISTINCT a.user_id) active_users
        FROM tasks t LEFT JOIN attempts a ON a.task_id=t.id GROUP BY t.subject ORDER BY tasks DESC""").fetchall()
    recent = c.execute("""SELECT u.id, u.name, MAX(a.ts) last_seen, COUNT(a.ts) attempts
        FROM users u LEFT JOIN attempts a ON a.user_id=u.id GROUP BY u.id
        ORDER BY last_seen DESC LIMIT 8""").fetchall()
    daily = c.execute("SELECT COUNT(DISTINCT user_id) FROM attempts WHERE ts>=?", (now - day,)).fetchone()[0]
    weekly = c.execute("SELECT COUNT(DISTINCT user_id) FROM attempts WHERE ts>=?", (now - 7 * day,)).fetchone()[0]
    payments = c.execute("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments WHERE refunded=0").fetchone()
    exams = c.execute("SELECT COUNT(*), COALESCE(SUM(finished>0),0), COALESCE(AVG(CASE WHEN finished>0 THEN score END),0) FROM exams").fetchone()
    lifetime = c.execute("SELECT COALESCE(SUM(xp),0), COALESCE(SUM(coins),0), COALESCE(AVG(streak),0), COALESCE(MAX(best_streak),0) FROM users").fetchone()
    errors = c.execute("""SELECT COUNT(*), COALESCE(SUM(status!='mastered'),0),
        COALESCE(SUM(status='mastered' AND next_review<=?),0), COALESCE(SUM(status='mastered' AND next_review>?),0)
        FROM errors""", (now, now)).fetchone()
    diagnostics = c.execute("SELECT COUNT(*), COALESCE(SUM(finished>0),0), COALESCE(AVG(CASE WHEN finished>0 THEN score END),0) FROM diagnostics").fetchone()
    task_types = c.execute("SELECT type, COUNT(*) count FROM tasks GROUP BY type ORDER BY count DESC").fetchall()
    return {
        "generated_at": now, "period": {"from": start, "to": end}, "users": user_count,
        "active_today": daily, "active_week": weekly, "active_period": period["active"],
        "attempts_today": c.execute("SELECT COUNT(*) FROM attempts WHERE ts>=?", (now - day,)).fetchone()[0],
        "attempts_period": period["attempts"], "accuracy_period": round(100 * period["correct"] / period["attempts"]) if period["attempts"] else 0,
        "attempts_all": all_attempts[0], "accuracy_all": round(100 * all_attempts[1] / all_attempts[0]) if all_attempts[0] else 0,
        "paid_users": paid_users, "trial_users": trial_users, "expired_users": expired_users,
        "pending_reports": c.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "tasks": task_stats[0], "verified_tasks": task_stats[1], "revenue_stars": revenue, "refunded_stars": refunded,
        "payments_count": payments[0], "exams_count": exams[0], "finished_exams": exams[1], "average_exam_score": round(exams[2], 1),
        "lifetime_xp": lifetime[0], "lifetime_coins": lifetime[1], "average_streak": round(lifetime[2], 1), "best_streak": lifetime[3],
        "errors_total": errors[0], "errors_open": errors[1], "reinforcement_due": errors[2], "reinforcement_scheduled": errors[3],
        "diagnostics_count": diagnostics[0], "diagnostics_finished": diagnostics[1], "average_diagnostic_score": round(diagnostics[2], 1),
        "task_types": [dict(row) for row in task_types],
        "trend": [dict(row) for row in trend], "subjects": [dict(row) for row in subjects],
        "recent_users": [dict(row) for row in recent],
    }


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
def admin_users(q: str = Query("", max_length=100), page: int = Query(1, ge=1),
                status: str = "all", sort: str = "last_seen", direction: str = "desc",
                subject: str = "", from_date: str = Query("", alias="from", max_length=10),
                to_date: str = Query("", alias="to", max_length=10), c=Depends(get_db)):
    now = int(time.time())
    term = f"%{q.strip()}%"
    start, end = admin_period(from_date, to_date, 36500)
    where = ["(CAST(u.id AS TEXT) LIKE ? OR u.name LIKE ?)"]
    args = [term, term]
    if status == "paid":
        where.append("u.paid_until>?"); args.append(now)
    elif status == "trial":
        where.append("u.paid_until<=? AND MAX(u.trial_until,u.bonus_until)>?"); args.extend([now, now])
    elif status == "expired":
        where.append("u.paid_until<=? AND MAX(u.trial_until,u.bonus_until)<=?"); args.extend([now, now])
    elif status != "all":
        raise HTTPException(400, "Неизвестный статус доступа")
    if subject:
        where.append("EXISTS(SELECT 1 FROM attempts ax JOIN tasks tx ON tx.id=ax.task_id WHERE ax.user_id=u.id AND tx.subject=?)")
        args.append(subject)
    if from_date or to_date:
        where.append("EXISTS(SELECT 1 FROM attempts ad WHERE ad.user_id=u.id AND ad.ts>=? AND ad.ts<?)")
        args.extend([start, end])
    where_sql = " AND ".join(where)
    sort_columns = {"last_seen": "last_seen", "created": "u.created_ts", "xp": "u.xp", "attempts": "attempts", "accuracy": "accuracy", "streak": "u.streak", "id": "u.id"}
    if sort not in sort_columns or direction not in ("asc", "desc"):
        raise HTTPException(400, "Недопустимая сортировка")
    total = c.execute(f"SELECT COUNT(*) FROM users u WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT u.id, u.name, u.xp, u.coins, u.streak, u.best_streak, u.shields, u.hearts,
        u.goal, u.created_ts, u.trial_until, u.paid_until, u.bonus_until,
        MAX(a.ts) last_seen, COUNT(a.ts) attempts, COALESCE(SUM(a.correct),0) correct,
        ROUND(100.0*COALESCE(SUM(a.correct),0)/NULLIF(COUNT(a.ts),0),1) accuracy,
        (SELECT COUNT(*) FROM errors er WHERE er.user_id=u.id AND er.status!='mastered') open_errors,
        (SELECT COUNT(*) FROM exams ex WHERE ex.user_id=u.id) exams
        FROM users u LEFT JOIN attempts a ON a.user_id=u.id WHERE {where_sql} GROUP BY u.id
        ORDER BY {sort_columns[sort]} {direction.upper()}, u.id DESC LIMIT 25 OFFSET ?""", (*args, (page - 1) * 25)).fetchall()
    users = []
    for row in rows:
        item = dict(row)
        item["access"] = "paid" if item["paid_until"] > now else "trial" if max(item["trial_until"], item["bonus_until"]) > now else "expired"
        item["paid_until"] = item["paid_until"] or 0
        users.append(item)
    return {"total": total, "page": page, "page_size": 25, "users": users}


@app.get("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def admin_user_detail(user_id: int, c=Depends(get_db)):
    user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    now = int(time.time())
    stats = c.execute("""SELECT COUNT(*) attempts, COALESCE(SUM(correct),0) correct,
        MIN(ts) first_attempt, MAX(ts) last_attempt FROM attempts WHERE user_id=?""", (user_id,)).fetchone()
    subjects = c.execute("""SELECT t.subject, COUNT(*) attempts, SUM(a.correct) correct,
        COUNT(DISTINCT t.topic) topics, MAX(a.ts) last_seen FROM attempts a JOIN tasks t ON t.id=a.task_id
        WHERE a.user_id=? GROUP BY t.subject ORDER BY attempts DESC""", (user_id,)).fetchall()
    exams = c.execute("SELECT * FROM exams WHERE user_id=? ORDER BY started DESC LIMIT 50", (user_id,)).fetchall()
    payments = c.execute("SELECT charge_id,stars,ts,since,until,refunded FROM payments WHERE user_id=? ORDER BY ts DESC", (user_id,)).fetchall()
    errors = c.execute("""SELECT e.task_id,e.mistakes,e.stage,e.next_review,e.status,t.subject,t.topic,t.year,t.variant,t.num
        FROM errors e LEFT JOIN tasks t ON t.id=e.task_id WHERE e.user_id=? ORDER BY e.next_review""", (user_id,)).fetchall()
    badges = c.execute("SELECT code,ts FROM badges WHERE user_id=? ORDER BY ts DESC", (user_id,)).fetchall()
    xp = c.execute("SELECT day,SUM(xp) xp FROM xp_log WHERE user_id=? GROUP BY day ORDER BY day DESC LIMIT 90", (user_id,)).fetchall()
    diagnostics = c.execute("SELECT id,subject,started,finished,score FROM diagnostics WHERE user_id=? ORDER BY started DESC LIMIT 50", (user_id,)).fetchall()
    item = dict(user)
    item["access"] = "paid" if item["paid_until"] > now else "trial" if max(item["trial_until"], item["bonus_until"]) > now else "expired"
    return {"user": item, "stats": dict(stats), "subjects": [dict(row) for row in subjects],
            "exams": [dict(row) for row in exams], "payments": [dict(row) for row in payments],
            "errors": [dict(row) for row in errors], "badges": [dict(row) for row in badges],
            "xp_history": [dict(row) for row in xp], "diagnostics": [dict(row) for row in diagnostics]}


@app.get("/api/admin/users/{user_id}/attempts", dependencies=[Depends(require_admin)])
def admin_user_attempts(user_id: int, page: int = Query(1, ge=1), subject: str = "",
                        result: str = "all", c=Depends(get_db)):
    if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise HTTPException(404, "Пользователь не найден")
    where, args = ["a.user_id=?"], [user_id]
    if subject:
        where.append("t.subject=?"); args.append(subject)
    if result == "correct": where.append("a.correct=1")
    elif result == "wrong": where.append("a.correct=0")
    elif result != "all": raise HTTPException(400, "Неизвестный результат")
    where_sql = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM attempts a JOIN tasks t ON t.id=a.task_id WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT a.ts, a.correct, t.id task_id, t.subject, t.topic, t.year,
        t.variant, t.num, t.type, t.question, t.answer FROM attempts a
        JOIN tasks t ON t.id=a.task_id WHERE {where_sql} ORDER BY a.ts DESC LIMIT 25 OFFSET ?""",
        (*args, (page - 1) * 25)).fetchall()
    return {"total": total, "page": page, "attempts": [dict(row) for row in rows]}


@app.get("/api/admin/reports", dependencies=[Depends(require_admin)])
def admin_reports(q: str = Query("", max_length=100), subject: str = "", page: int = Query(1, ge=1),
                  sort: str = "ts", direction: str = "desc", from_date: str = Query("", alias="from", max_length=10),
                  to_date: str = Query("", alias="to", max_length=10), c=Depends(get_db)):
    where, args = ["(r.note LIKE ? OR r.task_id LIKE ? OR CAST(r.user_id AS TEXT) LIKE ?)"], [f"%{q}%"] * 3
    if subject:
        where.append("t.subject=?"); args.append(subject)
    if from_date or to_date:
        start, end = admin_period(from_date, to_date, 36500)
        where.append("r.ts>=? AND r.ts<?"); args.extend([start, end])
    sort_columns = {"ts": "r.ts", "task_id": "r.task_id", "user_id": "r.user_id"}
    if sort not in sort_columns or direction not in ("asc", "desc"): raise HTTPException(400, "Недопустимая сортировка жалоб")
    where_sql = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM reports r LEFT JOIN tasks t ON t.id=r.task_id WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT r.id, r.user_id, u.name, r.task_id, t.subject, t.year, t.variant,
        t.num, t.topic, t.question, r.note, r.ts FROM reports r LEFT JOIN users u ON u.id=r.user_id
        LEFT JOIN tasks t ON t.id=r.task_id WHERE {where_sql} ORDER BY {sort_columns[sort]} {direction.upper()} LIMIT 25 OFFSET ?""",
        (*args, (page - 1) * 25)).fetchall()
    return {"total": total, "page": page, "reports": [dict(row) for row in rows]}


@app.get("/api/admin/payments", dependencies=[Depends(require_admin)])
def admin_payments(q: str = Query("", max_length=100), status: str = "all", page: int = Query(1, ge=1),
                   sort: str = "ts", direction: str = "desc", from_date: str = Query("", alias="from", max_length=10),
                   to_date: str = Query("", alias="to", max_length=10), c=Depends(get_db)):
    where, args = ["(CAST(p.user_id AS TEXT) LIKE ? OR u.name LIKE ? OR p.charge_id LIKE ?)"], [f"%{q}%"] * 3
    if status == "refunded": where.append("p.refunded=1")
    elif status == "active": where.append("p.refunded=0 AND p.until>?"); args.append(int(time.time()))
    elif status == "expired": where.append("p.refunded=0 AND p.until<=?"); args.append(int(time.time()))
    elif status != "all": raise HTTPException(400, "Неизвестный статус платежа")
    if from_date or to_date:
        start, end = admin_period(from_date, to_date, 36500)
        where.append("p.ts>=? AND p.ts<?"); args.extend([start, end])
    sort_columns = {"ts": "p.ts", "stars": "p.stars", "user_id": "p.user_id", "until": "p.until"}
    if sort not in sort_columns or direction not in ("asc", "desc"): raise HTTPException(400, "Недопустимая сортировка платежей")
    where_sql = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT p.charge_id, p.user_id, u.name, p.stars, p.ts, p.since, p.until, p.refunded
        FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE {where_sql}
        ORDER BY {sort_columns[sort]} {direction.upper()} LIMIT 25 OFFSET ?""", (*args, (page - 1) * 25)).fetchall()
    return {"total": total, "page": page, "payments": [dict(row) for row in rows]}


@app.get("/api/admin/exams", dependencies=[Depends(require_admin)])
def admin_exams(q: str = Query("", max_length=100), subject: str = "", status: str = "all", page: int = Query(1, ge=1),
                sort: str = "started", direction: str = "desc", from_date: str = Query("", alias="from", max_length=10),
                to_date: str = Query("", alias="to", max_length=10), c=Depends(get_db)):
    where, args = ["(CAST(e.user_id AS TEXT) LIKE ? OR u.name LIKE ? OR CAST(e.year AS TEXT) LIKE ?)"], [f"%{q}%"] * 3
    if subject: where.append("e.subject=?"); args.append(subject)
    if status == "finished": where.append("e.finished>0")
    elif status == "ongoing": where.append("e.finished=0")
    elif status != "all": raise HTTPException(400, "Неизвестный статус экзамена")
    if from_date or to_date:
        start, end = admin_period(from_date, to_date, 36500)
        where.append("e.started>=? AND e.started<?"); args.extend([start, end])
    sort_columns = {"started": "e.started", "score": "e.score", "subject": "e.subject", "year": "e.year", "user_id": "e.user_id"}
    if sort not in sort_columns or direction not in ("asc", "desc"): raise HTTPException(400, "Недопустимая сортировка экзаменов")
    where_sql = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM exams e LEFT JOIN users u ON u.id=e.user_id WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT e.id, e.user_id, u.name, e.subject, e.year, e.variant, e.started,
        e.deadline, e.finished, e.correct, e.total, e.points, e.max_points, e.score,
        (SELECT COUNT(*) FROM exam_answers ea WHERE ea.exam_id=e.id) saved_answers
        FROM exams e LEFT JOIN users u ON u.id=e.user_id WHERE {where_sql}
        ORDER BY {sort_columns[sort]} {direction.upper()} LIMIT 25 OFFSET ?""", (*args, (page - 1) * 25)).fetchall()
    return {"total": total, "page": page, "exams": [dict(row) for row in rows]}


@app.get("/api/admin/tasks", dependencies=[Depends(require_admin)])
def admin_tasks(q: str = Query("", max_length=100), subject: str = "", verified: str = "all",
                task_type: str = "", year: int = Query(0, ge=0), page: int = Query(1, ge=1),
                sort: str = "year", direction: str = "desc", c=Depends(get_db)):
    where, args = ["(id LIKE ? OR question LIKE ? OR topic LIKE ? OR source LIKE ?)"], [f"%{q}%"] * 4
    if subject: where.append("subject=?"); args.append(subject)
    if verified in ("yes", "no"): where.append("verified=?"); args.append(int(verified == "yes"))
    elif verified != "all": raise HTTPException(400, "Неизвестный фильтр проверки")
    if task_type: where.append("type=?"); args.append(task_type)
    if year: where.append("year=?"); args.append(year)
    sort_columns = {"year": "year", "subject": "subject", "variant": "variant", "num": "num", "verified": "verified", "topic": "topic", "type": "type"}
    if sort not in sort_columns or direction not in ("asc", "desc"): raise HTTPException(400, "Недопустимая сортировка заданий")
    where_sql = " AND ".join(where)
    total = c.execute(f"SELECT COUNT(*) FROM tasks WHERE {where_sql}", args).fetchone()[0]
    rows = c.execute(f"""SELECT id, exam, subject, year, variant, num, part, topic, type, verified,
        question, options, answer, explanation, source, note FROM tasks WHERE {where_sql}
        ORDER BY {sort_columns[sort]} {direction.upper()}, year DESC, variant DESC, subject, part, num LIMIT 25 OFFSET ?""", (*args, (page - 1) * 25)).fetchall()
    tasks = []
    for row in rows:
        item = dict(row)
        for key in ("options", "answer", "explanation"):
            try: item[key] = json.loads(item[key])
            except (TypeError, ValueError): pass
        tasks.append(item)
    return {"total": total, "page": page, "tasks": tasks}


@contextmanager
def write_transaction(c):
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.commit()
    except Exception:
        c.rollback()
        raise


def verify(init: str):
    try:
        pairs = parse_qsl(init, keep_blank_values=True, strict_parsing=True, max_num_fields=20)
    except ValueError:
        raise HTTPException(401, "Откройте приложение через Telegram")
    data = dict(pairs)
    if len(data) != len(pairs):
        raise HTTPException(401, "Откройте приложение через Telegram")
    got = data.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", got):
        raise HTTPException(401, "Откройте приложение через Telegram")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got)
    try:
        auth_date = int(data.get("auth_date", ""))
        user = json.loads(data.get("user", ""))
    except (TypeError, ValueError):
        raise HTTPException(401, "Откройте приложение через Telegram")
    age = time.time() - auth_date
    if (not BOT_TOKEN or not ok or age > 86400 or age < -30 or not isinstance(user, dict)
            or type(user.get("id")) is not int or user["id"] <= 0):
        raise HTTPException(401, "Откройте приложение через Telegram")
    return user


def heart_sec():
    return 60 if DEV else int(cfg("heart_minutes")) * 60  # в DEV сердце возвращается за минуту


def refill(c, uid):
    if not cfg("hearts_enabled"):
        return
    mx, sec = int(cfg("max_hearts")), heart_sec()
    u = c.execute("SELECT hearts, hearts_ts FROM users WHERE id=?", (uid,)).fetchone()
    now = int(time.time())
    if u["hearts"] >= mx:
        c.execute("UPDATE users SET hearts_ts=? WHERE id=?", (now, uid))
        return
    n = (now - u["hearts_ts"]) // sec
    if n:
        c.execute("UPDATE users SET hearts=MIN(?, hearts+?), hearts_ts=hearts_ts+? WHERE id=?", (mx, n, n * sec, uid))


def auth(x_init_data: str = Header(""), c=Depends(get_db)):
    tu = {"id": 1, "first_name": "Dev"} if (DEV and not x_init_data) else verify(x_init_data)
    now = int(time.time())
    created = c.execute("INSERT OR IGNORE INTO users(id, name, hearts_ts, created_ts) VALUES(?,?,?,?)",
                         (tu["id"], tu.get("first_name", "Ученик"), now, now)).rowcount == 1
    if created and not DEV:
        billing.notify_new_user(BOT_TOKEN, ADMIN_ID, tu["id"], tu.get("first_name"), tu.get("username"))
    refill(c, tu["id"])
    return c.execute("SELECT * FROM users WHERE id=?", (tu["id"],)).fetchone()


def day_start():
    return int(datetime.combine(today(), datetime.min.time(), tzinfo=timezone(timedelta(hours=3))).timestamp())


def daily_left(c, uid, a):
    """Сколько заданий осталось сегодня в бесплатной версии (None = без лимита)."""
    if a["daily_limit"] is None:
        return None
    n = c.execute("SELECT COUNT(*) FROM attempts WHERE user_id=? AND ts>=?", (uid, day_start())).fetchone()[0]
    return max(0, a["daily_limit"] - n)


def locked(msg="Эта функция есть в полной версии"):
    return HTTPException(402, msg)


def need(feature):
    """Зависимость: пропускает, только если функция открыта текущим тарифом; иначе 402 и экран оплаты."""
    def dep(u=Depends(auth), c=Depends(get_db)):
        if not billing.access(c, u["id"])["features"].get(feature):
            raise locked()
        return u
    return dep


def state(c, uid):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    gap = (today() - date.fromisoformat(u["last_day"])).days if u["last_day"] else 99
    streak = u["streak"] if gap <= 1 or (gap == 2 and u["shields"] > 0) else 0
    tx = c.execute("SELECT COALESCE(SUM(xp),0) FROM xp_log WHERE user_id=? AND day=?",
                   (uid, today().isoformat())).fetchone()[0]
    return {"name": u["name"], "xp": u["xp"], "coins": u["coins"], "streak": streak,
            "best_streak": u["best_streak"], "shields": u["shields"], "hearts": u["hearts"],
            "hearts_enabled": bool(cfg("hearts_enabled")), "goal": u["goal"], "today_xp": tx,
            "needs_diag": c.execute("SELECT COUNT(*) FROM attempts WHERE user_id=?", (uid,)).fetchone()[0] == 0,
            "open_errors": c.execute("SELECT COUNT(*) FROM errors WHERE user_id=? AND status!='mastered'", (uid,)).fetchone()[0],
            "reinforcement_due": c.execute("SELECT COUNT(*) FROM errors WHERE user_id=? AND status='mastered' AND next_review<=?",
                                            (uid, int(time.time()))).fetchone()[0]}


def add_xp(c, u, xp, coins):
    iso = today().isoformat()
    if u["last_day"] != iso:  # первая активность за день: обновляем серию
        gap = (today() - date.fromisoformat(u["last_day"])).days if u["last_day"] else 99
        used = 0
        if gap == 1:
            s = u["streak"] + 1
        elif gap == 2 and u["shields"] > 0:  # щит закрывает один пропущенный день
            s, used = u["streak"] + 1, 1
        else:
            s = 1
        c.execute("UPDATE users SET streak=?, best_streak=MAX(best_streak,?), last_day=?, shields=shields-? WHERE id=?",
                  (s, s, iso, used, u["id"]))
    c.execute("UPDATE users SET xp=xp+?, coins=coins+? WHERE id=?", (xp, coins, u["id"]))
    c.execute("INSERT INTO xp_log(user_id, day, xp) VALUES(?,?,?)", (u["id"], iso, xp))


def check_badges(c, uid, perfect=False):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    n = c.execute("SELECT COUNT(*) FROM attempts WHERE user_id=? AND correct=1", (uid,)).fetchone()[0]
    have = {r[0] for r in c.execute("SELECT code FROM badges WHERE user_id=?", (uid,))}
    new = []
    for code, title, _, fn in BADGES:
        if code not in have and fn(u, n, perfect):
            c.execute("INSERT INTO badges(user_id, code, ts) VALUES(?,?,?)", (uid, code, int(time.time())))
            c.execute("UPDATE users SET coins=coins+10 WHERE id=?", (uid,))
            new.append(title)
    return new


def is_right(kind, right, got):
    try:
        if kind == "choice":
            return str(got) == str(right)
        if kind in ("multi", "nums"):  # несколько верных номеров: список или строка «2, 5»
            got = got if isinstance(got, list) else re.findall(r"\d", str(got))
            return sorted(map(int, got)) == sorted(right)
        lat = str.maketrans("abcekmhoptxy", "авсекмнортху")  # латинские двойники русских букв в ключе ответов
        norm = lambda s: " ".join(str(s).lower().replace("ё", "е").translate(lat).split())  # е и ё считаем одинаковыми
        if kind == "match":  # «А2 Б3, В1-Г4» = «А2Б3В1Г4»
            return re.sub(r"[\W_]", "", norm(got)) == re.sub(r"[\W_]", "", norm(right))
        try:
            return abs(float(str(got).replace(",", ".").strip()) - float(right)) < 1e-6
        except ValueError:
            return norm(got) == norm(right)
    except (ValueError, TypeError):
        return False


def level(c, uid, subject):
    """Ориентировочный уровень 0-100: до 80 растёт быстро (60% освоенных тем), дальше медленно."""
    rows = c.execute("""SELECT t.topic, COUNT(*) n, SUM(a.correct) k FROM attempts a JOIN tasks t ON t.id=a.task_id
                        WHERE a.user_id=? AND t.subject=? GROUP BY t.topic""", (uid, subject)).fetchall()
    total = c.execute("SELECT COUNT(DISTINCT topic) FROM tasks WHERE subject=? AND verified=1", (subject,)).fetchone()[0]
    done = sum(1 for r in rows if r["n"] >= 2 and r["k"] / r["n"] >= 0.8)  # тема освоена: 2+ ответа, 80%+ верных
    p = done / total if total else 0
    return round(80 * p / 0.6 if p <= 0.6 else 80 + 20 * ((p - 0.6) / 0.4) ** 2), done, total


def update_subject_score(c, uid, subject, correct):
    target = 100 if correct else 0
    c.execute("""UPDATE subject_scores
                  SET score=MAX(0, MIN(100, ROUND(score + (? - score) * 0.025))),
                      attempts=attempts+1, correct=correct+?, updated_ts=?
                  WHERE user_id=? AND subject=?""",
              (target, int(correct), int(time.time()), uid, subject))


def steps():
    return [max(1, int(x)) for x in cfg("error_steps_days")] or [1]


def reinforcement_steps():
    return [max(1, int(x)) for x in cfg("error_reinforcement_days") if int(x) > 0] or [30]


def review_error(c, uid, tid, ok):
    """Ошибки проходят короткий цикл, затем возвращаются на долгосрочное закрепление."""
    now, e = int(time.time()), c.execute("SELECT * FROM errors WHERE user_id=? AND task_id=?", (uid, tid)).fetchone()
    if not ok:
        nxt = now + steps()[0] * DAY
        if e:
            c.execute("UPDATE errors SET mistakes=mistakes+1, stage=0, status='failed', next_review=? WHERE user_id=? AND task_id=?", (nxt, uid, tid))
        else:
            c.execute("INSERT INTO errors(user_id, task_id, next_review) VALUES(?,?,?)", (uid, tid, nxt))
    elif e and e["next_review"] <= now:
        st = e["stage"] + 1
        if e["status"] == "mastered":
            intervals = reinforcement_steps()
            index = min(st - len(steps()), len(intervals) - 1)
            c.execute("UPDATE errors SET stage=?, next_review=? WHERE user_id=? AND task_id=?",
                      (st, now + intervals[index] * DAY, uid, tid))
        elif st >= len(steps()):
            c.execute("UPDATE errors SET status='mastered', stage=?, next_review=? WHERE user_id=? AND task_id=?",
                      (st, now + reinforcement_steps()[0] * DAY, uid, tid))
        else:
            c.execute("UPDATE errors SET status='reviewing', stage=?, next_review=? WHERE user_id=? AND task_id=?", (st, now + steps()[st] * DAY, uid, tid))


Given = Union[str, int, float, list]


class Ans(BaseModel):
    task_id: str = Field(max_length=200)
    answer: Given
    revealed: bool = False  # перед ответом был открыт разбор

    @field_validator("answer")
    @classmethod
    def validate_answer_size(cls, value):
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError("Ответ слишком длинный")
        if isinstance(value, list) and len(value) > 100:
            raise ValueError("Слишком много элементов в ответе")
        return value


class Fin(BaseModel):
    total: int


class ExamStart(BaseModel):
    subject: str = "rus"
    year: int
    variant: int


class ExamSave(BaseModel):
    exam_id: int
    task_id: str = Field(max_length=200)
    answer: Given

    @field_validator("answer")
    @classmethod
    def validate_answer_size(cls, value):
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError("Ответ слишком длинный")
        if isinstance(value, list) and len(value) > 100:
            raise ValueError("Слишком много элементов в ответе")
        return value


class Report(BaseModel):
    task_id: str = Field(max_length=200)
    note: str = Field(default="", max_length=300)


class ExamFin(BaseModel):
    exam_id: int


class DiagStart(BaseModel):
    subject: str


class DiagFinish(BaseModel):
    diagnostic_id: int


def no_hearts():
    return HTTPException(403, "Сердца закончились. Одно возвращается каждые %d минут." % cfg("heart_minutes"))


ORDER = "ORDER BY part, CAST(substr(num, 2) AS INT)"


def hint_levels(r):
    """Подсказки к заданию по нарастанию: тема и формат ответа -> свои подсказки из JSON -> вычеркнуть лишнее -> разбор."""
    kind, ans, opts = r["type"], json.loads(r["answer"]), json.loads(r["options"])
    fmt = {"multi": f"Верных вариантов: {len(ans) if isinstance(ans, list) else 1}.", "nums": f"Верных номеров: {len(ans) if isinstance(ans, list) else 1}.",
           "match": "Нужно сопоставить все буквы с цифрами.", "text": f"Ответ: одно слово или короткая запись ({len(str(ans))} зн.).",
           "number": "Ответ: число."}.get(kind, "")
    out = [{"kind": "topic", "title": "Тема", "text": ((f"Тема: {r['topic']}. " if r["topic"] else "") + fmt).strip()}]
    out += [{"kind": "tip", "title": "Подсказка", "text": x} for x in json.loads(r["hints"] or "[]")]
    if kind in ("multi", "choice") and opts:
        right = set(ans if kind == "multi" else [ans + 1])
        wrong = [i for i in range(len(opts)) if i + 1 not in right]
        if len(wrong) >= 2:
            out.append({"kind": "cut", "title": "Уберём лишнее", "text": "Эти два варианта точно неверные.",
                        "cross": sorted(random.Random(r["id"]).sample(wrong, 2))})
    elif kind == "text":
        out.append({"kind": "start", "title": "Начало слова", "text": f"Первая буква: «{str(ans)[0]}»."})
    elif kind == "match":
        out.append({"kind": "start", "title": "Первая пара", "text": f"{str(ans)[0]} → {str(ans)[1]}"})
    ex = json.loads(r["explanation"] or "[]")
    if ex:
        out.append({"kind": "explain", "title": "Разбор", "text": "\n".join(ex), "lines": ex})
    return out


def pub(r, hints=True):
    return {"id": r["id"], "subject": r["subject"], "type": r["type"], "question": r["question"], "topic": r["topic"], "options": json.loads(r["options"]),
            "passage": r["passage"], "num": r["num"], "year": r["year"], "variant": r["variant"],
            "hints": len(hint_levels(r)) if hints and cfg("hints_enabled") else 0}


def me_cfg():
    return {k: cfg(k) for k in ("hints_enabled", "hint_cost_coins", "price_stars", "trial_days", "paid_days", "exam_minutes",
                                "hints_in_exam", "reveal_counts_as_miss", "error_steps_days", "error_reinforcement_days")}


@app.get("/api/me")
def me(u=Depends(auth), c=Depends(get_db)):
    subs = []
    for r in c.execute("SELECT subject, COUNT(*) n FROM tasks WHERE verified=1 GROUP BY subject").fetchall():
        _, done, total = level(c, u["id"], r["subject"])
        score = c.execute("SELECT score FROM subject_scores WHERE user_id=? AND subject=?", (u["id"], r["subject"])).fetchone()
        subs.append({"subject": r["subject"], "n": r["n"], "level": score[0] if score else None,
                     "diagnostic_done": bool(score), "mastered": done, "topics": total})
    due = c.execute("SELECT COUNT(*) FROM errors WHERE user_id=? AND status!='mastered' AND next_review<=?",
                    (u["id"], int(time.time()))).fetchone()[0]
    have = {r[0] for r in c.execute("SELECT code FROM badges WHERE user_id=?", (u["id"],))}
    acc = billing.access(c, u["id"])
    acc["daily_left"] = daily_left(c, u["id"], acc)
    return {**state(c, u["id"]), "subjects": subs, "due": due, "shield_price": SHIELD_PRICE,
            "access": acc, "cfg": me_cfg(), "dev": DEV, "bot_link": f"https://t.me/{BOT_USERNAME}?start=ref{u['id']}" if BOT_USERNAME else "",
            "share_text": cfg("share_text"),
            "badges": [{"code": b[0], "title": b[1], "desc": b[2], "earned": b[0] in have} for b in BADGES]}


@app.get("/api/session")
def session(subject: Annotated[str, Query(max_length=20)] = "", mode: Annotated[str, Query(max_length=20)] = "",
            topic: Annotated[str, Query(max_length=200)] = "", n: Annotated[int, Query(ge=1, le=20)] = 10,
            year: Annotated[int, Query(ge=0, le=2200)] = 0, variant: Annotated[int, Query(ge=0, le=1000)] = 0,
            todo: Annotated[int, Query(ge=0, le=1)] = 0,
            exam_id: int = 0, u=Depends(auth), c=Depends(get_db)):
    n = max(1, min(n, 20))
    if cfg("hearts_enabled") and u["hearts"] <= 0:
        raise no_hearts()
    uid = u["id"]
    acc = billing.access(c, uid)
    feat = {"variant": "sborniki", "errors": "errors", "errors_all": "errors", "exam_errors": "errors"}.get(mode, "practice")
    if not acc["features"][feat]:
        raise locked()
    left = daily_left(c, uid, acc)
    if left is not None:
        if left <= 0:
            raise locked("Дневной лимит бесплатной версии исчерпан. Полная версия снимает лимит.")
        n = min(n, left)
    if mode == "errors":
        rows = c.execute(ERR_SQL, (uid, int(time.time()), min(n, 8))).fetchall()
    elif mode == "errors_all":  # все непроработанные ошибки, не дожидаясь срока повторения
                now = int(time.time())
                rows = c.execute("""SELECT t.* FROM errors e JOIN tasks t ON t.id=e.task_id
                                                        WHERE e.user_id=? AND (e.status!='mastered' OR e.next_review<=?)
                                                            AND (?='' OR t.subject=?)
                                                        ORDER BY CASE WHEN e.next_review<=? THEN 0 ELSE 1 END,
                                                                         e.mistakes DESC, e.next_review LIMIT ?""",
                                                 (uid, now, subject, subject, now, max(1, min(n, 10)))).fetchall()
    elif mode == "variant":  # тренировка по варианту: по порядку, с подсказками; todo=1 — только ещё не решённые верно
        rows = c.execute(f"""SELECT * FROM tasks t WHERE subject=? AND year=? AND variant=? AND verified=1
              AND (?=0 OR NOT EXISTS(SELECT 1 FROM attempts a WHERE a.user_id=? AND a.task_id=t.id AND a.correct=1)) {ORDER}""",
                         (subject, year, variant, todo, uid)).fetchall()
        if not rows and todo:
            rows = c.execute(f"SELECT * FROM tasks WHERE subject=? AND year=? AND variant=? AND verified=1 {ORDER}", (subject, year, variant)).fetchall()
    elif mode == "exam_errors":  # тест по ошибкам сразу после экзамена
        rows = c.execute(f"""SELECT t.* FROM exam_answers x JOIN tasks t ON t.id=x.task_id JOIN exams e ON e.id=x.exam_id
              WHERE x.exam_id=? AND e.user_id=? AND x.correct=0 {ORDER.replace('part', 't.part').replace('num', 't.num')}""", (exam_id, uid)).fetchall()
    elif mode == "diag":  # проверка уровня: по одному заданию из каждой темы
        limit = max(1, min(n, 10))
        if subject:
            rows = c.execute("SELECT * FROM tasks WHERE subject=? AND verified=1 GROUP BY topic LIMIT ?", (subject, limit)).fetchall()
        else:
            rows = c.execute("""SELECT * FROM tasks WHERE verified=1 AND id IN (
                    SELECT MIN(id) FROM tasks WHERE verified=1 GROUP BY subject, topic
                ) ORDER BY RANDOM() LIMIT ?""", (limit,)).fetchall()
    else:
        rows = c.execute(
            """SELECT * FROM tasks t WHERE subject=? AND verified=1 AND (?='' OR topic=?)
               ORDER BY (SELECT COUNT(*) FROM attempts a WHERE a.task_id=t.id AND a.user_id=? AND a.correct=1), RANDOM()
               LIMIT ?""", (subject, topic, topic, uid, max(1, min(n, 20)))).fetchall()
    return {"tasks": [pub(r, hints=acc["features"]["hints"]) for r in rows]}


@app.post("/api/diagnostic/start")
def diagnostic_start(a: DiagStart, u=Depends(auth), c=Depends(get_db)):
    if cfg("hearts_enabled") and u["hearts"] <= 0:
        raise no_hearts()
    acc = billing.access(c, u["id"])
    if not acc["features"]["practice"]:
        raise locked()
    limit = 10
    left = daily_left(c, u["id"], acc)
    if left is not None:
        if left <= 0:
            raise locked("Дневной лимит бесплатной версии исчерпан. Полная версия снимает лимит.")
        limit = min(limit, left)
    rows = c.execute("""SELECT * FROM tasks WHERE id IN (
                 SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER(
                     PARTITION BY CASE WHEN topic='' THEN id ELSE topic END ORDER BY RANDOM()
                   ) AS topic_rank
                   FROM tasks WHERE subject=? AND verified=1
                 ) WHERE topic_rank=1
               ) ORDER BY RANDOM() LIMIT ?""", (a.subject, limit)).fetchall()
    if not rows:
        raise HTTPException(404, "Для этого предмета пока нет проверенных заданий")
    task_ids = [row["id"] for row in rows]
    now = int(time.time())
    attempt_start_id = c.execute("SELECT COALESCE(MAX(rowid),0) FROM attempts WHERE user_id=?", (u["id"],)).fetchone()[0]
    cur = c.execute("INSERT INTO diagnostics(user_id,subject,task_ids,attempt_start_id,started) VALUES(?,?,?,?,?)",
                    (u["id"], a.subject, json.dumps(task_ids), attempt_start_id, now))
    return {"diagnostic_id": cur.lastrowid, "subject": a.subject, "tasks": [pub(row, hints=False) for row in rows]}


@app.post("/api/diagnostic/finish")
def diagnostic_finish(a: DiagFinish, u=Depends(auth), c=Depends(get_db)):
    with write_transaction(c):
        diag = c.execute("SELECT * FROM diagnostics WHERE id=? AND user_id=?", (a.diagnostic_id, u["id"])).fetchone()
        if not diag:
            raise HTTPException(404, "Диагностический тест не найден")
        if diag["finished"]:
            raise HTTPException(409, "Этот тест уже завершён")
        task_ids = json.loads(diag["task_ids"])
        placeholders = ",".join("?" for _ in task_ids)
        rows = c.execute(f"""SELECT task_id, correct FROM (
                    SELECT task_id, correct, ROW_NUMBER() OVER(PARTITION BY task_id ORDER BY rowid DESC) AS attempt_rank
                    FROM attempts WHERE user_id=? AND rowid>? AND task_id IN ({placeholders})
                ) WHERE attempt_rank=1""", (u["id"], diag["attempt_start_id"], *task_ids)).fetchall()
        answered = len(rows)
        if not answered:
            raise HTTPException(400, "Сначала ответьте хотя бы на одно задание")
        correct = sum(row["correct"] for row in rows)
        accuracy = correct / answered * 100
        baseline_score = round(50 + (accuracy - 50) * 0.6)
        existing = c.execute("SELECT score FROM subject_scores WHERE user_id=? AND subject=?",
                             (u["id"], diag["subject"])).fetchone()
        score = round(existing["score"] * 0.75 + baseline_score * 0.25) if existing else baseline_score
        c.execute("""INSERT INTO subject_scores(user_id,subject,score,attempts,correct,updated_ts)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,subject) DO UPDATE SET
                     score=excluded.score, attempts=subject_scores.attempts+excluded.attempts,
                     correct=subject_scores.correct+excluded.correct, updated_ts=excluded.updated_ts""",
                  (u["id"], diag["subject"], score, answered, correct, int(time.time())))
        c.execute("UPDATE diagnostics SET finished=?,score=? WHERE id=?", (int(time.time()), score, a.diagnostic_id))
        return {"subject": diag["subject"], "score": score, "correct": correct, "answered": answered, "total": len(task_ids)}


@app.get("/api/hint")
def hint(task_id: str, level: int, u=Depends(need("hints")), c=Depends(get_db)):
    if not cfg("hints_enabled"):
        raise HTTPException(403, "Подсказки отключены")
    t = c.execute("SELECT * FROM tasks WHERE id=? AND verified=1", (task_id,)).fetchone()
    lv = hint_levels(t) if t else []
    if not 1 <= level <= len(lv):
        raise HTTPException(404, "Такой подсказки нет")
    cost = int(cfg("hint_cost_coins"))
    reveal = lv[level - 1]["kind"] == "explain" and bool(cfg("reveal_counts_as_miss"))
    if cost or reveal:
        with write_transaction(c):
            if cost:
                cur = c.execute("UPDATE users SET coins=coins-? WHERE id=? AND coins>=?", (cost, u["id"], cost))
                if cur.rowcount != 1:
                    raise HTTPException(400, f"Нужно {cost} монет")
            if reveal:
                c.execute("INSERT OR REPLACE INTO revealed_answers(user_id,task_id,ts) VALUES(?,?,?)",
                          (u["id"], task_id, int(time.time())))
    return {**lv[level - 1], "level": level, "reveal": lv[level - 1]["kind"] == "explain", **state(c, u["id"])}


@app.post("/api/answer")
def answer(a: Ans, u=Depends(auth), c=Depends(get_db)):
    with write_transaction(c):
        uid = u["id"]
        refill(c, uid)
        current = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        left = daily_left(c, uid, billing.access(c, uid))
        if left is not None and left <= 0:
            raise locked("Дневной лимит бесплатной версии исчерпан. Полная версия снимает лимит.")
        hearts = cfg("hearts_enabled")
        if hearts and current["hearts"] <= 0:
            raise no_hearts()
        t = c.execute("SELECT * FROM tasks WHERE id=? AND verified=1", (a.task_id,)).fetchone()
        if not t:
            raise HTTPException(404, "Задание не найдено")
        right = json.loads(t["answer"])
        ok = is_right(t["type"], right, a.answer)
        now = int(time.time())
        reveal_row = c.execute("SELECT ts FROM revealed_answers WHERE user_id=? AND task_id=?",
                               (uid, t["id"])).fetchone()
        server_revealed = bool(reveal_row and reveal_row["ts"] >= now - 1800)
        if reveal_row and not server_revealed:
            c.execute("DELETE FROM revealed_answers WHERE user_id=? AND task_id=?", (uid, t["id"]))
        helped = ok and server_revealed and bool(cfg("reveal_counts_as_miss"))
        if helped:
            c.execute("DELETE FROM revealed_answers WHERE user_id=? AND task_id=?", (uid, t["id"]))
        good = ok and not helped
        c.execute("INSERT INTO attempts(user_id, task_id, correct, ts) VALUES(?,?,?,?)", (uid, t["id"], int(good), now))
        update_subject_score(c, uid, t["subject"], good)
        xp = 0
        if good:
            claim = c.execute("INSERT OR IGNORE INTO xp_claims(user_id, task_id, day) VALUES(?,?,?)",
                              (uid, t["id"], today().isoformat()))
            if claim.rowcount:
                xp = int(cfg("xp_per_correct"))
                add_xp(c, current, xp, COIN_OK)
        elif not ok and hearts:
            c.execute("UPDATE users SET hearts=MAX(0, hearts-1) WHERE id=?", (uid,))
        review_error(c, uid, t["id"], good)
        trap = None if ok else json.loads(t["distractors"] or "{}").get(str(a.answer))
        return {"correct": ok, "helped": helped, "right": right, "explanation": json.loads(t["explanation"]), "xp_earned": xp, "trap": trap,
                "badges": check_badges(c, uid), **state(c, uid)}


@app.post("/api/finish")
def finish(f: Fin, u=Depends(auth), c=Depends(get_db)):
    with write_transaction(c):
        total = max(1, min(f.total, 20))
        last = c.execute(
            "SELECT task_id, correct FROM attempts WHERE user_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
            (u["id"], total)).fetchall()
        perfect = (total >= 5 and len(last) == total and all(row["correct"] for row in last)
                   and len({row["task_id"] for row in last}) == total)
        bonus = int(cfg("xp_perfect_bonus"))
        claim = c.execute("INSERT OR IGNORE INTO perfect_claims(user_id, day) VALUES(?,?)",
                          (u["id"], today().isoformat())) if perfect else None
        awarded = bool(claim and claim.rowcount)
        if awarded:
            fresh = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
            add_xp(c, fresh, bonus, 5)
        return {"bonus": bonus if awarded else 0, "badges": check_badges(c, u["id"], perfect), **state(c, u["id"])}


# ---------- сборники и экзамены ----------
@app.get("/api/sborniki")
def sborniki_list(subject: str = "rus", u=Depends(auth), c=Depends(get_db)):
    rows = c.execute("""SELECT t.year, t.variant, SUM(t.verified) total, COUNT(*)-SUM(t.verified) hidden,
        SUM(CASE WHEN t.verified=1 AND EXISTS(SELECT 1 FROM attempts a WHERE a.user_id=? AND a.task_id=t.id AND a.correct=1) THEN 1 ELSE 0 END) solved
        FROM tasks t WHERE t.subject=? AND t.year>0 GROUP BY t.year, t.variant ORDER BY t.year DESC, t.variant""", (u["id"], subject)).fetchall()
    ex = {}
    for e in c.execute("SELECT * FROM exams WHERE user_id=? AND subject=? ORDER BY id", (u["id"], subject)):
        d = ex.setdefault((e["year"], e["variant"]), {"state": "none", "best": None})
        if e["finished"]:
            d.update(state="done", id=e["id"], score=e["score"], correct=e["correct"], total=e["total"])
            d["best"] = max(d["best"] or 0, e["score"])
        elif not e["deadline"] or e["deadline"] > time.time():
            d.update(state="going", id=e["id"])
    years = {}
    for r in rows:
        years.setdefault(r["year"], []).append({"variant": r["variant"], "total": r["total"], "hidden": r["hidden"], "solved": r["solved"],
                                                "exam": ex.get((r["year"], r["variant"]), {"state": "none", "best": None})})
    f = billing.access(c, u["id"])["features"]
    return {"years": [{"year": y, "variants": v} for y, v in years.items()], "locked": not f["sborniki"], "exam_locked": not f["exam"]}


def exam_rows(c, subject, year, variant):
    return c.execute(f"SELECT * FROM tasks WHERE subject=? AND year=? AND variant=? AND verified=1 {ORDER}", (subject, year, variant)).fetchall()


def to_score(pts, mx):
    table = cfg("exam_scale")
    if table:  # [[первичный, тестовый], ...]: берём строку с наибольшим первичным баллом, не превышающим набранный
        return max([s for p, s in sorted(table) if pts >= p], default=0)
    return round(100 * pts / mx) if mx else 0


def exam_result(c, e):
    items, by_topic, parts = [], {}, {"A": [0, 0], "B": [0, 0]}
    for r in c.execute(f"""SELECT t.*, x.answer given, x.correct ok FROM exam_answers x JOIN tasks t ON t.id=x.task_id
                           WHERE x.exam_id=? {ORDER.replace('part', 't.part').replace('num', 't.num')}""", (e["id"],)):
        parts[r["part"]][1] += 1
        parts[r["part"]][0] += r["ok"]
        bt = by_topic.setdefault(r["topic"], [0, 0])
        bt[0] += r["ok"]
        bt[1] += 1
        items.append({"id": r["id"], "num": r["num"], "type": r["type"], "topic": r["topic"], "question": r["question"], "options": json.loads(r["options"]),
                      "passage": r["passage"], "ok": bool(r["ok"]), "given": json.loads(r["given"]), "right": json.loads(r["answer"]),
                      "explanation": json.loads(r["explanation"])})
    weak = sorted(({"topic": k, "ok": v[0], "total": v[1]} for k, v in by_topic.items() if v[0] < v[1]), key=lambda x: (x["ok"] / x["total"], -x["total"]))
    return {"exam_id": e["id"], "year": e["year"], "variant": e["variant"], "correct": e["correct"], "total": e["total"], "points": e["points"],
            "max_points": e["max_points"], "score": e["score"], "parts": parts, "weak": weak, "items": items,
            "wrong": sum(1 for i in items if not i["ok"])}


def grade_exam(c, e):
    """Проверяет все ответы экзамена. Каждая ошибка (и пропуск) уходит в архив ошибок. Повторный вызов ничего не меняет."""
    if e["finished"]:
        return c.execute("SELECT * FROM exams WHERE id=?", (e["id"],)).fetchone()
    uid, now, w = e["user_id"], int(time.time()), cfg("exam_points")
    saved = {r["task_id"]: json.loads(r["answer"]) for r in c.execute("SELECT * FROM exam_answers WHERE exam_id=?", (e["id"],))}
    pts = mx = cnt = earned = 0
    rows = exam_rows(c, e["subject"] or "rus", e["year"], e["variant"])
    for t in rows:
        got, wt = saved.get(t["id"]), w.get("A" if t["part"] == "A" else "B", 1)
        ok = got not in (None, "", []) and is_right(t["type"], json.loads(t["answer"]), got)
        mx += wt
        pts += wt if ok else 0
        cnt += ok
        c.execute("INSERT OR REPLACE INTO exam_answers VALUES(?,?,?,?)", (e["id"], t["id"], json.dumps(got, ensure_ascii=False), int(ok)))
        c.execute("INSERT INTO attempts(user_id, task_id, correct, ts) VALUES(?,?,?,?)", (uid, t["id"], int(ok), now))
        update_subject_score(c, uid, t["subject"], ok)
        if ok:
            claim = c.execute("INSERT OR IGNORE INTO xp_claims(user_id, task_id, day) VALUES(?,?,?)",
                              (uid, t["id"], today().isoformat()))
            earned += claim.rowcount
        review_error(c, uid, t["id"], ok)
    c.execute("UPDATE exams SET finished=?, correct=?, total=?, points=?, max_points=?, score=? WHERE id=?",
              (now, cnt, len(rows), pts, mx, to_score(pts, mx), e["id"]))
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if earned:
        add_xp(c, u, earned * int(cfg("exam_xp_per_correct")), earned * COIN_OK)
    return c.execute("SELECT * FROM exams WHERE id=?", (e["id"],)).fetchone()


def own_exam(c, uid, exam_id):
    e = c.execute("SELECT * FROM exams WHERE id=? AND user_id=?", (exam_id, uid)).fetchone()
    if not e:
        raise HTTPException(404, "Экзамен не найден")
    return e


@app.post("/api/exam/start")
def exam_start(a: ExamStart, u=Depends(need("exam")), c=Depends(get_db)):
    rows = exam_rows(c, a.subject, a.year, a.variant)
    if not rows:
        raise HTTPException(404, "Такого варианта нет")
    now = int(time.time())
    e = c.execute("SELECT * FROM exams WHERE user_id=? AND subject=? AND year=? AND variant=? AND finished=0 ORDER BY id DESC",
                  (u["id"], a.subject, a.year, a.variant)).fetchone()
    if e and e["deadline"] and e["deadline"] + 15 < now:  # время вышло, пока приложение было закрыто: проверяем как есть
        with write_transaction(c):
            grade_exam(c, e)
        e = None
    if not e:
        mins = int(cfg("exam_minutes"))
        cur = c.execute("INSERT INTO exams(user_id, subject, year, variant, started, deadline) VALUES(?,?,?,?,?,?)",
                        (u["id"], a.subject, a.year, a.variant, now, now + mins * 60 if mins else 0))
        e = c.execute("SELECT * FROM exams WHERE id=?", (cur.lastrowid,)).fetchone()
    saved = {r["task_id"]: json.loads(r["answer"]) for r in c.execute("SELECT * FROM exam_answers WHERE exam_id=?", (e["id"],))}
    show_hints = bool(cfg("hints_in_exam") and billing.access(c, u["id"])["features"].get("hints"))
    return {"exam_id": e["id"], "seconds_left": max(0, e["deadline"] - now) if e["deadline"] else None, "saved": saved,
            "tasks": [pub(r, hints=show_hints) for r in rows]}


@app.post("/api/exam/save")
def exam_save(a: ExamSave, u=Depends(need("exam")), c=Depends(get_db)):
    with write_transaction(c):
        e = own_exam(c, u["id"], a.exam_id)
        if e["finished"]:
            raise HTTPException(400, "Экзамен уже завершён")
        now = int(time.time())
        if e["deadline"] and now > e["deadline"]:
            raise HTTPException(400, "Время экзамена вышло")
        exists = c.execute("SELECT 1 FROM tasks WHERE id=? AND subject=? AND year=? AND variant=? AND verified=1",
                           (a.task_id, e["subject"], e["year"], e["variant"])).fetchone()
        if not exists:
            raise HTTPException(404, "Задание не найдено в этом варианте")
        c.execute("INSERT OR REPLACE INTO exam_answers VALUES(?,?,?,NULL)", (e["id"], a.task_id, json.dumps(a.answer, ensure_ascii=False)))
        return {"seconds_left": max(0, e["deadline"] - now) if e["deadline"] else None}


@app.post("/api/exam/finish")
def exam_finish(a: ExamFin, u=Depends(need("exam")), c=Depends(get_db)):
    with write_transaction(c):
        e = grade_exam(c, own_exam(c, u["id"], a.exam_id))
        return {**exam_result(c, e), "badges": check_badges(c, u["id"]), **state(c, u["id"])}


@app.get("/api/exam/result")
def exam_get(exam_id: int, u=Depends(need("exam")), c=Depends(get_db)):
    e = own_exam(c, u["id"], exam_id)
    if not e["finished"]:
        raise HTTPException(400, "Экзамен ещё не завершён")
    return exam_result(c, e)


# ---------- ошибки и жалобы ----------
@app.get("/api/errors")
def errors_list(u=Depends(auth), c=Depends(get_db)):
    """Вкладка «Ошибки»: всё, что ещё не проработано. Список виден и в бесплатной версии, работа с ним — в полной."""
    now = int(time.time())
    rows = c.execute("""SELECT t.subject, t.topic, COUNT(*) n,
        SUM(CASE WHEN e.status!='mastered' THEN 1 ELSE 0 END) active,
        SUM(CASE WHEN e.status!='mastered' AND e.next_review<=? THEN 1 ELSE 0 END) due,
        SUM(CASE WHEN e.status='mastered' AND e.next_review<=? THEN 1 ELSE 0 END) reinforce_due
        FROM errors e JOIN tasks t ON t.id=e.task_id WHERE e.user_id=?
          AND (e.status!='mastered' OR e.next_review<=?) GROUP BY t.subject, t.topic
        ORDER BY n DESC""", (now, now, u["id"], now)).fetchall()
    return {"open": c.execute("SELECT COUNT(*) FROM errors WHERE user_id=? AND status!='mastered'", (u["id"],)).fetchone()[0],
            "due": sum(r["due"] for r in rows), "reinforcement_due": sum(r["reinforce_due"] for r in rows),
            "reinforcement_scheduled": c.execute("SELECT COUNT(*) FROM errors WHERE user_id=? AND status='mastered' AND next_review>?",
                              (u["id"], now)).fetchone()[0],
            "next_reinforcement_at": c.execute("SELECT MIN(next_review) FROM errors WHERE user_id=? AND status='mastered' AND next_review>?",
                             (u["id"], now)).fetchone()[0],
            "topics": [{"subject": r["subject"], "topic": r["topic"], "n": r["n"],
                        "active": r["active"], "reinforcement_due": r["reinforce_due"]} for r in rows],
            "locked": not billing.access(c, u["id"])["features"]["errors"]}


@app.post("/api/report")
def report(r: Report, u=Depends(auth), c=Depends(get_db)):
    t = c.execute("SELECT id, num, year, variant FROM tasks WHERE id=?", (r.task_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Задание не найдено")
    now = int(time.time())
    if c.execute("SELECT 1 FROM reports WHERE user_id=? AND task_id=? AND ts>=? LIMIT 1",
                  (u["id"], t["id"], now - 86400)).fetchone():
        return {"ok": True, "duplicate": True}
    daily_reports = c.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND ts>=?",
                              (u["id"], now - 86400)).fetchone()[0]
    if daily_reports >= 20:
        raise HTTPException(429, "Лимит жалоб на сегодня исчерпан")
    c.execute("INSERT INTO reports(user_id, task_id, note, ts) VALUES(?,?,?,?)", (u["id"], t["id"], r.note[:300], now))
    if ADMIN_ID and BOT_TOKEN:
        billing.notify(BOT_TOKEN, ADMIN_ID, f"Жалоба на задание {t['id']} ({t['year']}, вариант {t['variant']}, {t['num']}) от {u['id']}. {r.note[:300]}")
    return {"ok": True}


# ---------- оплата ----------
@app.post("/api/pay")
def pay(u=Depends(auth), c=Depends(get_db)):
    if not cfg("paywall_enabled"):
        raise HTTPException(403, "Оплата отключена")
    if DEV and not BOT_TOKEN:  # локально без Telegram: «оплата» проходит сразу
        billing.grant(c, u["id"], int(cfg("price_stars")), f"dev-{time.time_ns()}")
        return {"paid": True}
    try:
        return {"link": billing.create_invoice_link(BOT_TOKEN, u["id"])}
    except Exception as e:
        print("Не удалось создать счёт:", e)
        raise HTTPException(502, "Не удалось создать счёт. Попробуйте чуть позже.")


@app.get("/api/top")
def top(u=Depends(auth), c=Depends(get_db)):
    monday = (today() - timedelta(days=today().weekday())).isoformat()
    rows = c.execute("""SELECT us.id, us.name, SUM(x.xp) xp FROM xp_log x JOIN users us ON us.id=x.user_id
                        WHERE x.day>=? GROUP BY us.id ORDER BY xp DESC LIMIT 20""", (monday,)).fetchall()
    return {"top": [{"name": r["name"], "xp": r["xp"], "me": r["id"] == u["id"]} for r in rows]}


@app.get("/api/topics")
def topics(subject: str, u=Depends(auth), c=Depends(get_db)):
    rows = c.execute("""SELECT t.topic, COUNT(*) n,
        (SELECT COUNT(*) FROM attempts a JOIN tasks x ON x.id=a.task_id
          WHERE a.user_id=? AND x.subject=t.subject AND x.topic=t.topic) att,
        (SELECT COALESCE(SUM(a.correct),0) FROM attempts a JOIN tasks x ON x.id=a.task_id
          WHERE a.user_id=? AND x.subject=t.subject AND x.topic=t.topic) ok
        FROM tasks t WHERE t.subject=? AND t.verified=1 GROUP BY t.topic""", (u["id"], u["id"], subject)).fetchall()
    out = []
    for r in rows:
        done = r["att"] >= 2 and r["ok"] / r["att"] >= 0.8  # то же правило освоения, что в level()
        out.append({"topic": r["topic"], "n": r["n"], "att": r["att"], "status": "mastered" if done else ("learning" if r["att"] else "new"),
                    "acc": round(100 * r["ok"] / r["att"]) if r["att"] else None})
    return {"topics": out}


@app.get("/api/daily", dependencies=[Depends(auth)])
def daily():
    cards = json.loads((ROOT / "content" / "cards.json").read_text("utf-8"))  # карточка дня: правило или факт
    return cards[today().toordinal() % len(cards)]


@app.post("/api/shield")
def shield(u=Depends(auth), c=Depends(get_db)):
    with write_transaction(c):
        cur = c.execute("UPDATE users SET coins=coins-?, shields=shields+1 WHERE id=? AND shields<2 AND coins>=?",
                        (SHIELD_PRICE, u["id"], SHIELD_PRICE))
        if not cur.rowcount:
            fresh = c.execute("SELECT shields, coins FROM users WHERE id=?", (u["id"],)).fetchone()
            if fresh["shields"] >= 2:
                raise HTTPException(400, "Можно хранить не больше двух щитов")
            raise HTTPException(400, f"Нужно {SHIELD_PRICE} монет")
        return state(c, u["id"])


app.mount("/", StaticFiles(directory=ROOT / "webapp", html=True), name="web")
