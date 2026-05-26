import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "planbot.db"
MEDIA_DIR = BASE_DIR / "media"
PHOTO_ADD = MEDIA_DIR / "add.jpg"
PHOTO_DONE = MEDIA_DIR / "done.jpg"
PHOTO_DELETE = MEDIA_DIR / "delete.jpg"
TZ_NAME = os.environ.get("PLANBOT_TZ", "Asia/Almaty")
TZ = ZoneInfo(TZ_NAME)
TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("planbot")


# ---------- UI constants ----------

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📅 Сегодня", "📆 Завтра"],
        ["🗓 Неделя", "📊 Матрица"],
        ["⚠️ Просрочено", "📈 Стата"],
        ["❓ Помощь"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

BOT_COMMANDS = [
    ("today", "📅 Дела на сегодня"),
    ("tomorrow", "📆 Дела на завтра"),
    ("week", "🗓 На ближайшую неделю"),
    ("matrix", "📊 Матрица Эйзенхауэра"),
    ("overdue", "⚠️ Просроченные дела"),
    ("all", "📋 Все будущие дела"),
    ("stats", "📈 Статистика"),
    ("find", "🔍 Поиск по тексту"),
    ("morning", "🌅 Утренний дайджест"),
    ("remind", "⏰ Поставить напоминание"),
    ("snooze", "⏭ Отложить задачу"),
    ("prio", "🎯 Сменить приоритет"),
    ("edit", "✏️ Изменить текст задачи"),
    ("done", "✅ Сделано / вернуть"),
    ("del", "🗑 Удалить задачу"),
    ("clear", "🧹 Удалить все выполненные"),
    ("reset", "🔥 Удалить ВСЁ (полная очистка)"),
    ("help", "❓ Как пользоваться"),
]


# ---------- DB ----------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                task_date TEXT NOT NULL,
                remind_at TEXT,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_date ON tasks(user_id, task_date);
            CREATE INDEX IF NOT EXISTS idx_remind ON tasks(remind_at);
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                morning_time TEXT
            );
"""
        )
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "priority" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
            )


def db_add_task(user_id, chat_id, task_date, text, remind_at=None, priority=0):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, chat_id, task_date, remind_at, text, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                chat_id,
                task_date.isoformat(),
                remind_at.isoformat() if remind_at else None,
                text,
                priority,
                datetime.now(TZ).isoformat(),
            ),
        )
        return cur.lastrowid


def db_set_priority(task_id, user_id, priority):
    with db_connect() as conn:
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ? AND user_id = ?",
            (priority, task_id, user_id),
        )


def db_list_tasks(user_id, day=None, days_ahead=None):
    with db_connect() as conn:
        order = f"done, {PRIO_SORT_CASE}, remind_at IS NULL, remind_at, id"
        if day is not None:
            return conn.execute(
                f"SELECT * FROM tasks WHERE user_id = ? AND task_date = ? "
                f"ORDER BY {order}",
                (user_id, day.isoformat()),
            ).fetchall()
        today = datetime.now(TZ).date()
        if days_ahead is not None:
            end = today + timedelta(days=days_ahead)
            return conn.execute(
                f"SELECT * FROM tasks WHERE user_id = ? AND task_date BETWEEN ? AND ? "
                f"ORDER BY task_date, {order}",
                (user_id, today.isoformat(), end.isoformat()),
            ).fetchall()
        return conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND task_date >= ? "
            f"ORDER BY task_date, {order}",
            (user_id, today.isoformat()),
        ).fetchall()


def db_get_task(task_id, user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()


def db_toggle_done(task_id, user_id):
    with db_connect() as conn:
        conn.execute(
            "UPDATE tasks SET done = 1 - done WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def db_delete_task(task_id, user_id):
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def db_pending_reminders():
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE remind_at IS NOT NULL AND done = 0"
        ).fetchall()


def db_list_matrix(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND done = 0 "
            "ORDER BY task_date, remind_at IS NULL, remind_at, id",
            (user_id,),
        ).fetchall()


def db_list_overdue(user_id):
    today = datetime.now(TZ).date().isoformat()
    with db_connect() as conn:
        return conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND done = 0 AND task_date < ? "
            f"ORDER BY task_date, {PRIO_SORT_CASE}, remind_at IS NULL, remind_at, id",
            (user_id, today),
        ).fetchall()


def db_update_text(task_id, user_id, text, priority=None):
    with db_connect() as conn:
        if priority is None:
            conn.execute(
                "UPDATE tasks SET text = ? WHERE id = ? AND user_id = ?",
                (text, task_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET text = ?, priority = ? WHERE id = ? AND user_id = ?",
                (text, priority, task_id, user_id),
            )


def db_update_schedule(task_id, user_id, new_date, new_remind_at):
    with db_connect() as conn:
        conn.execute(
            "UPDATE tasks SET task_date = ?, remind_at = ? WHERE id = ? AND user_id = ?",
            (
                new_date.isoformat(),
                new_remind_at.isoformat() if new_remind_at else None,
                task_id,
                user_id,
            ),
        )


def db_search(user_id, term):
    needle = term.lower()
    with db_connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? "
            f"ORDER BY done, task_date, {PRIO_SORT_CASE}, remind_at IS NULL, remind_at, id",
            (user_id,),
        ).fetchall()
    return [r for r in rows if needle in r["text"].lower()]


def db_clear_done(user_id):
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE user_id = ? AND done = 1",
            (user_id,),
        )
        return cur.rowcount


def db_get_user_task_ids(user_id):
    with db_connect() as conn:
        return [
            r[0] for r in conn.execute(
                "SELECT id FROM tasks WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]


def db_delete_all_user_data(user_id):
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        return cur.rowcount


def db_count_done(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND done = 1",
            (user_id,),
        ).fetchone()[0]


def db_update_remind(task_id, user_id, remind_at):
    with db_connect() as conn:
        conn.execute(
            "UPDATE tasks SET remind_at = ? WHERE id = ? AND user_id = ?",
            (remind_at.isoformat() if remind_at else None, task_id, user_id),
        )


def db_get_morning(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, chat_id, morning_time FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def db_set_morning(user_id, chat_id, hhmm):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, chat_id, morning_time) "
            "VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "chat_id = excluded.chat_id, morning_time = excluded.morning_time",
            (user_id, chat_id, hhmm),
        )


def db_all_morning():
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, chat_id, morning_time FROM user_settings "
            "WHERE morning_time IS NOT NULL"
        ).fetchall()


def db_stats(user_id):
    today = datetime.now(TZ).date()
    tom = today + timedelta(days=1)
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT done, task_date, priority FROM tasks WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["done"])
    pending = total - done
    overdue = sum(
        1 for r in rows
        if not r["done"] and date.fromisoformat(r["task_date"]) < today
    )
    today_n = sum(
        1 for r in rows
        if not r["done"] and date.fromisoformat(r["task_date"]) == today
    )
    tomorrow_n = sum(
        1 for r in rows
        if not r["done"] and date.fromisoformat(r["task_date"]) == tom
    )
    urgent = sum(1 for r in rows if not r["done"] and r["priority"] == 2)
    important = sum(1 for r in rows if not r["done"] and r["priority"] == 1)
    normal = sum(1 for r in rows if not r["done"] and r["priority"] == 0)
    return {
        "total": total, "done": done, "pending": pending,
        "overdue": overdue, "today": today_n, "tomorrow": tomorrow_n,
        "urgent": urgent, "important": important, "normal": normal,
    }


# ---------- Parsing ----------

DATE_KEYWORDS = {
    "сегодня": 0, "today": 0,
    "завтра": 1, "tomorrow": 1,
    "послезавтра": 2,
}

WEEKDAY_RU = ["понедельник", "вторник", "среда", "четверг",
              "пятница", "суббота", "воскресенье"]


def parse_date_only(s: str):
    today = datetime.now(TZ).date()
    s = s.strip().lower()
    if s in DATE_KEYWORDS:
        return today + timedelta(days=DATE_KEYWORDS[s])
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", s)
    if m:
        day_v, month_v = int(m.group(1)), int(m.group(2))
        year_raw = m.group(3)
        if year_raw:
            year_v = int(year_raw)
            if year_v < 100:
                year_v += 2000
        else:
            year_v = today.year
            try:
                if date(year_v, month_v, day_v) < today:
                    year_v += 1
            except ValueError:
                pass
        try:
            return date(year_v, month_v, day_v)
        except ValueError:
            return None
    return None


PRIO_ICONS = {0: "▫️", 1: "🟡", 2: "🔴", 3: "🟣"}
PRIO_NAMES = {
    0: "обычная",
    1: "важно (запланировать)",
    2: "срочно+важно (сделать сейчас)",
    3: "срочно (делегировать)",
}
# Sort order across priorities: do-now (2) > schedule (1) > delegate (3) > normal (0).
# Used in SQL ORDER BY as a CASE expression.
PRIO_SORT_CASE = (
    "CASE priority "
    "WHEN 2 THEN 0 "
    "WHEN 1 THEN 1 "
    "WHEN 3 THEN 2 "
    "ELSE 3 END"
)


def parse_hhmm(s: str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
    if not m:
        return None
    try:
        return dtime(int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def compute_snooze_date(row, arg=None):
    """For /snooze: returns new date or None if arg unparseable.
    No arg → +1 day from task date, or today if overdue."""
    today = datetime.now(TZ).date()
    cur = date.fromisoformat(row["task_date"])
    if not arg:
        return today if cur < today else cur + timedelta(days=1)
    arg = arg.strip().lower()
    m = re.match(r"^\+(\d+)$", arg)
    if m:
        base = today if cur < today else cur
        return base + timedelta(days=int(m.group(1)))
    return parse_date_only(arg)


def reschedule_task_reminder(context, row, new_date):
    """Move remind_at to new_date keeping the original HH:MM. Returns new remind_at or None."""
    tid = row["id"]
    for job in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
        job.schedule_removal()
    if not row["remind_at"]:
        return None
    try:
        old = datetime.fromisoformat(row["remind_at"])
    except ValueError:
        return None
    if old.tzinfo is None:
        old = old.replace(tzinfo=TZ)
    new_remind = datetime.combine(new_date, old.timetz())
    if new_remind.tzinfo is None:
        new_remind = new_remind.replace(tzinfo=TZ)
    now = datetime.now(TZ)
    if new_remind > now:
        context.job_queue.run_once(
            send_reminder,
            when=(new_remind - now).total_seconds(),
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            name=f"remind:{tid}",
            data={"task_id": tid},
        )
    return new_remind


async def send_reaction(bot, chat_id: int, photo_path: Path, caption: str):
    """Send a photo reaction with caption; fall back to text if photo missing."""
    if photo_path.exists():
        try:
            with open(photo_path, "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
            return
        except Exception as e:
            log.warning("send_photo %s failed: %s", photo_path, e)
    await bot.send_message(
        chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML
    )


def extract_priority(text: str):
    """Pull standalone !/!! tokens out of text → (priority, cleaned)."""
    prio = 0

    def repl(m):
        nonlocal prio
        prio = max(prio, min(2, len(m.group(1))))
        return " "

    cleaned = re.sub(r"(?:^|\s)(!{1,3})(?=\s|$)", repl, text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return prio, cleaned


def parse_input(text: str):
    """Parse 'date [time] task text' → (date, time|None, text) or None."""
    s = text.strip()
    today = datetime.now(TZ).date()

    d = None
    rest = s

    m = re.match(
        r"^(сегодня|завтра|послезавтра|today|tomorrow)\b\s*(.*)$",
        rest, re.IGNORECASE,
    )
    if m:
        d = today + timedelta(days=DATE_KEYWORDS[m.group(1).lower()])
        rest = m.group(2)
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(.*)$", rest)
        if m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                rest = m.group(4)
            except ValueError:
                return None
        else:
            m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(.*)$", rest)
            if m:
                day_v, month_v = int(m.group(1)), int(m.group(2))
                year_raw = m.group(3)
                if year_raw:
                    year_v = int(year_raw)
                    if year_v < 100:
                        year_v += 2000
                else:
                    year_v = today.year
                    try:
                        if date(year_v, month_v, day_v) < today:
                            year_v += 1
                    except ValueError:
                        pass
                try:
                    d = date(year_v, month_v, day_v)
                    rest = m.group(4)
                except ValueError:
                    return None

    if d is None:
        return None

    t = None
    m = re.match(r"^(?:в\s+)?(\d{1,2}):(\d{2})\s+(.*)$", rest, re.IGNORECASE)
    if m:
        try:
            t = dtime(int(m.group(1)), int(m.group(2)))
            rest = m.group(3)
        except ValueError:
            pass

    rest = rest.strip()
    if not rest:
        return None
    return d, t, rest


# ---------- Formatting ----------

def fmt_date(d: date) -> str:
    today = datetime.now(TZ).date()
    delta = (d - today).days
    if delta == 0:
        label = "сегодня"
    elif delta == 1:
        label = "завтра"
    elif delta == -1:
        label = "вчера"
    else:
        label = WEEKDAY_RU[d.weekday()]
    return f"{d.strftime('%d.%m.%Y')} ({label})"


def task_line(row) -> str:
    if row["done"]:
        mark = "✅"
    else:
        mark = PRIO_ICONS.get(row["priority"], "▫️")
    suffix = ""
    if row["remind_at"]:
        try:
            ra = datetime.fromisoformat(row["remind_at"])
            suffix = f" ⏰ {ra.strftime('%H:%M')}"
        except ValueError:
            pass
    return f"{mark} <b>#{row['id']}</b> {row['text']}{suffix}"


def tasks_keyboard(rows, view: str):
    kb = []
    for r in rows:
        flip = "↩️" if r["done"] else "✅"
        prio_icon = PRIO_ICONS.get(r["priority"], "▫️")
        row_btns = [
            InlineKeyboardButton(
                f"{flip} #{r['id']}",
                callback_data=f"done:{r['id']}:{view}",
            ),
            InlineKeyboardButton(
                f"{prio_icon} #{r['id']}",
                callback_data=f"prio:{r['id']}:{view}",
            ),
        ]
        if not r["done"]:
            row_btns.append(
                InlineKeyboardButton(
                    f"⏭ #{r['id']}",
                    callback_data=f"snz:{r['id']}:{view}",
                )
            )
        row_btns.append(
            InlineKeyboardButton(
                f"🗑 #{r['id']}",
                callback_data=f"del:{r['id']}:{view}",
            )
        )
        kb.append(row_btns)
    return InlineKeyboardMarkup(kb) if kb else None


def render_tasks(rows, header: str) -> str:
    if not rows:
        return f"<b>{header}</b>\n\nПусто. Можно отдохнуть 🙂"
    out = [f"<b>{header}</b>"]
    cur_date = None
    for r in rows:
        d = date.fromisoformat(r["task_date"])
        if d != cur_date:
            out.append(f"\n📅 <i>{fmt_date(d)}</i>")
            cur_date = d
        out.append(task_line(r))
    done = sum(1 for r in rows if r["done"])
    out.append(f"\n— Сделано: {done} из {len(rows)} —")
    return "\n".join(out)


MATRIX_SECTIONS = [
    (2, "🔴 Сделать сейчас", "важно и срочно"),
    (1, "🟡 Запланировать", "важно, не срочно"),
    (3, "🟣 Делегировать", "срочно, не важно"),
    (0, "▫️ Не классифицировано", "ни важно, ни срочно — может удалить?"),
]


def render_matrix(rows) -> str:
    groups = {0: [], 1: [], 2: [], 3: []}
    for r in rows:
        groups.setdefault(r["priority"], []).append(r)
    today = datetime.now(TZ).date()
    out = ["<b>📊 Матрица Эйзенхауэра</b>"]
    for p, title, hint in MATRIX_SECTIONS:
        items = groups.get(p, [])
        out.append(f"\n<b>{title}</b> · <i>{hint}</i>")
        if not items:
            out.append("  — пусто")
            continue
        for r in items:
            d = date.fromisoformat(r["task_date"])
            overdue_mark = "⚠️ " if d < today else ""
            time_mark = ""
            if r["remind_at"]:
                try:
                    ra = datetime.fromisoformat(r["remind_at"])
                    time_mark = f" ⏰{ra.strftime('%H:%M')}"
                except ValueError:
                    pass
            out.append(
                f"  <b>#{r['id']}</b> <i>{overdue_mark}{fmt_date(d)}{time_mark}</i>\n"
                f"     {r['text']}"
            )
    out.append(f"\n— Всего в работе: {len(rows)} —")
    out.append(
        "💡 Кнопка цвета под задачей циклит квадрант: ▫️ → 🟡 → 🔴 → 🟣."
    )
    return "\n".join(out)


def view_for(user_id, view_code: str):
    """Return (rows, header) for a callback view code."""
    if view_code == "t":
        d = datetime.now(TZ).date()
        return db_list_tasks(user_id, day=d), f"Дела на {fmt_date(d)}"
    if view_code == "tm":
        d = datetime.now(TZ).date() + timedelta(days=1)
        return db_list_tasks(user_id, day=d), f"Дела на {fmt_date(d)}"
    if view_code == "w":
        return db_list_tasks(user_id, days_ahead=6), "Дела на ближайшие 7 дней"
    if view_code == "a":
        return db_list_tasks(user_id), "Все будущие дела"
    if view_code == "o":
        return db_list_overdue(user_id), "Просроченные дела"
    if view_code.startswith("d"):
        try:
            d = date.fromisoformat(view_code[1:])
            return db_list_tasks(user_id, day=d), f"Дела на {fmt_date(d)}"
        except ValueError:
            pass
    return [], "Дела"


# ---------- Handlers ----------

WELCOME = (
    "👋 <b>Привет! Я планировщик дел.</b>\n\n"
    "<b>Просто напиши, что и когда:</b>\n"
    "• <code>сегодня позвонить маме</code>\n"
    "• <code>завтра 18:00 встреча</code> — пришлю напоминание в 18:00\n"
    "• <code>25.05 !! сдать отчёт</code> — срочно и важно\n\n"
    "Внизу — быстрые кнопки (📅 Сегодня · 📆 Завтра · 🗓 Неделя · 📊 Матрица…).\n"
    "Рядом со скрепкой есть кнопка «☰» — там список всех команд.\n\n"
    "Подробная справка: /help"
)


HELP = (
    "📖 <b>Как пользоваться</b>\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✍️ <b>1. Как добавить задачу</b>\n"
    "Просто напиши <i>дату [время] текст</i>:\n"
    "• <code>сегодня позвонить маме</code>\n"
    "• <code>завтра 09:00 пробежка</code>\n"
    "• <code>25.05 купить молоко</code>\n"
    "• <code>25.05.2026 14:30 встреча</code>\n"
    "• <code>2026-05-25 отчёт</code>\n\n"
    "Дата: <code>сегодня/завтра/послезавтра</code>, <code>ДД.ММ</code>, "
    "<code>ДД.ММ.ГГГГ</code> или <code>ГГГГ-ММ-ДД</code>.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⏰ <b>2. Как сделать напоминание</b>\n"
    "<b>Способ 1.</b> Указать время прямо в задаче:\n"
    "• <code>завтра 18:00 встреча</code> → напомню в 18:00.\n\n"
    "<b>Способ 2.</b> Поставить позже на готовую задачу:\n"
    "• <code>/remind 12 18:00</code> — добавить напоминание к #12\n"
    "• <code>/remind 12 off</code> — снять напоминание\n\n"
    "<b>Когда напоминание сработает</b>, под ним будут кнопки:\n"
    "• ⏰ +15м / ⏰ +1ч / ⏰ +1д — отложить напоминание\n"
    "• ✅ Готово · 🗑 Удалить\n\n"
    "<b>Утренний дайджест</b> (план на день каждое утро):\n"
    "• <code>/morning 09:00</code> — включить · <code>/morning off</code> — выключить\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📊 <b>3. Матрица Эйзенхауэра</b>\n"
    "Каждая задача попадает в один из 4 квадрантов:\n"
    "🔴 <b>Сделать сейчас</b> — важно <i>и</i> срочно\n"
    "🟡 <b>Запланировать</b> — важно, не срочно\n"
    "🟣 <b>Делегировать</b> — срочно, не важно\n"
    "▫️ <b>Обычная</b> / «удалить» — ни важно, ни срочно\n\n"
    "<b>Как поставить квадрант:</b>\n"
    "• В тексте: <code>!</code> = 🟡 важно, <code>!!</code> = 🔴 сделать сейчас.\n"
    "  Пример: <code>завтра !! сдать отчёт</code>\n"
    "• Кнопкой под задачей — циклит ▫️ → 🟡 → 🔴 → 🟣.\n"
    "• Командой: <code>/prio 12 !!</code> или <code>/prio 12 делегировать</code>.\n\n"
    "Открыть матрицу: /matrix\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📋 <b>4. Списки задач</b>\n"
    "/today — на сегодня · /tomorrow — на завтра\n"
    "/week — на 7 дней · /all — все будущие\n"
    "/overdue — просроченные · /matrix — матрица\n"
    "/day 25.05.2026 — конкретная дата\n"
    "/find слово — поиск по тексту\n"
    "/stats — статистика\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "✏️ <b>5. Кнопки под каждой задачей</b>\n"
    "✅/↩️ — сделано / вернуть в работу\n"
    "🟡 🔴 🟣 ▫️ — циклить квадрант приоритета\n"
    "⏭ — отложить на день\n"
    "🗑 — удалить\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚙️ <b>6. Команды для конкретной задачи</b>\n"
    "<code>/done 12</code> — переключить «сделано»\n"
    "<code>/edit 12 новый текст</code> — изменить текст\n"
    "<code>/prio 12 !</code> — приоритет (0/1/2/3 или !, !!, делегировать)\n"
    "<code>/snooze 12</code> — на завтра · <code>/snooze 12 +3</code> · <code>/snooze 12 25.05</code>\n"
    "<code>/remind 12 18:00</code> — напоминание · <code>/remind 12 off</code> — снять\n"
    "<code>/del 12</code> — удалить · <code>/clear</code> — удалить все выполненные\n"
    "<code>/reset</code> — 🔥 удалить ВСЁ (задачи + дайджест + напоминания)\n"
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(WELCOME, reply_markup=MAIN_KB)


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP, reply_markup=MAIN_KB)


async def reply_view(update: Update, view_code: str):
    user_id = update.effective_user.id
    if view_code == "m":
        rows = db_list_matrix(user_id)
        await update.message.reply_html(
            render_matrix(rows),
            reply_markup=tasks_keyboard(rows, "m"),
        )
        return
    rows, header = view_for(user_id, view_code)
    await update.message.reply_html(
        render_tasks(rows, header),
        reply_markup=tasks_keyboard(rows, view_code),
    )


async def cmd_today(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "t")


async def cmd_tomorrow(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "tm")


async def cmd_week(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "w")


async def cmd_all(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "a")


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /day 25.05.2026 или /day 2026-05-25"
        )
        return
    d = parse_date_only(context.args[0])
    if not d:
        await update.message.reply_text("Не понял дату. Пример: /day 25.05.2026")
        return
    await reply_view(update, f"d{d.isoformat()}")


async def cmd_overdue(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "o")


async def cmd_matrix(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await reply_view(update, "m")


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /find слово")
        return
    term = " ".join(context.args).strip()
    if not term:
        await update.message.reply_text("Использование: /find слово")
        return
    rows = db_search(update.effective_user.id, term)
    header = f"Найдено по «{term}»: {len(rows)}"
    await update.message.reply_html(
        render_tasks(rows, header),
        reply_markup=tasks_keyboard(rows, "a"),
    )


async def cmd_stats(update: Update, _: ContextTypes.DEFAULT_TYPE):
    s = db_stats(update.effective_user.id)
    if s["total"] == 0:
        await update.message.reply_text("Пока ничего нет. Добавь первую задачу 🙂")
        return
    pct = round(100 * s["done"] / s["total"]) if s["total"] else 0
    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"Всего задач: <b>{s['total']}</b>\n"
        f"✅ Сделано: <b>{s['done']}</b> ({pct}%)\n"
        f"📝 В работе: <b>{s['pending']}</b>\n\n"
        f"<b>Среди невыполненных:</b>\n"
        f"⚠️ Просрочено: <b>{s['overdue']}</b>\n"
        f"📅 На сегодня: <b>{s['today']}</b>\n"
        f"📅 На завтра: <b>{s['tomorrow']}</b>\n\n"
        f"<b>По приоритету (в работе):</b>\n"
        f"🔴 Срочно: <b>{s['urgent']}</b>\n"
        f"🟡 Важно: <b>{s['important']}</b>\n"
        f"▫️ Обычные: <b>{s['normal']}</b>"
    )
    await update.message.reply_html(text)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /edit 12 новый текст задачи"
        )
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    new_text = " ".join(context.args[1:]).strip()
    if not new_text:
        await update.message.reply_text("Пустой текст. Добавь описание.")
        return
    row = db_get_task(tid, update.effective_user.id)
    if not row:
        await update.message.reply_text("Такой задачи нет")
        return
    prio, cleaned = extract_priority(new_text)
    if not cleaned:
        await update.message.reply_text("Пустой текст после удаления !-меток.")
        return
    if prio:
        db_update_text(tid, update.effective_user.id, cleaned, priority=prio)
    else:
        db_update_text(tid, update.effective_user.id, cleaned)
    await update.message.reply_html(
        f"Обновил <b>#{tid}</b>:\n<i>{cleaned}</i>"
    )


async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /snooze 12 [дата|+N]\n"
            "Без аргумента — на завтра (или на сегодня, если просрочено)."
        )
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    row = db_get_task(tid, update.effective_user.id)
    if not row:
        await update.message.reply_text("Такой задачи нет")
        return
    arg = " ".join(context.args[1:]).strip() if len(context.args) > 1 else None
    new_date = compute_snooze_date(row, arg)
    if not new_date:
        await update.message.reply_text(
            "Не понял дату. Примеры: /snooze 12 · /snooze 12 +3 · /snooze 12 25.05"
        )
        return
    new_remind = reschedule_task_reminder(context, row, new_date)
    db_update_schedule(tid, update.effective_user.id, new_date, new_remind)
    tail = ""
    if row["remind_at"] and new_remind and new_remind > datetime.now(TZ):
        tail = f"\n⏰ Напоминание на {new_remind.strftime('%H:%M')}."
    elif row["remind_at"] and not (new_remind and new_remind > datetime.now(TZ)):
        tail = "\n⚠️ Время напоминания уже прошло — не ставлю."
    await update.message.reply_html(
        f"Отложил <b>#{tid}</b> на <b>{fmt_date(new_date)}</b>:\n"
        f"<i>{row['text']}</i>{tail}"
    )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /remind 12 18:00\n"
            "или /remind 12 off — снять напоминание"
        )
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    row = db_get_task(tid, update.effective_user.id)
    if not row:
        await update.message.reply_text("Такой задачи нет")
        return
    arg = " ".join(context.args[1:]).strip().lower()
    for j in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
        j.schedule_removal()
    if arg in ("off", "выкл", "выключить", "0", "no", "нет"):
        db_update_remind(tid, update.effective_user.id, None)
        await update.message.reply_html(f"⏰ Снял напоминание с <b>#{tid}</b>.")
        return
    t = parse_hhmm(arg)
    if not t:
        await update.message.reply_text(
            "Не понял время. Пример: /remind 12 18:00 или /remind 12 off"
        )
        return
    task_date = date.fromisoformat(row["task_date"])
    new_remind = datetime.combine(task_date, t, tzinfo=TZ)
    now = datetime.now(TZ)
    if new_remind <= now:
        await update.message.reply_text(
            "Это время уже прошло. Сначала /snooze задачу или поставь будущее время."
        )
        return
    db_update_remind(tid, update.effective_user.id, new_remind)
    context.job_queue.run_once(
        send_reminder,
        when=(new_remind - now).total_seconds(),
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        name=f"remind:{tid}",
        data={"task_id": tid},
    )
    await update.message.reply_html(
        f"⏰ Напомню по <b>#{tid}</b> в <b>{new_remind.strftime('%H:%M')}</b> "
        f"({fmt_date(task_date)}):\n<i>{row['text']}</i>"
    )


async def cmd_morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not context.args:
        cur = db_get_morning(user_id)
        if cur and cur["morning_time"]:
            await update.message.reply_html(
                f"🌅 Утренний дайджест включён на <b>{cur['morning_time']}</b>.\n"
                f"Выключить: /morning off · Сменить время: /morning 08:30"
            )
        else:
            await update.message.reply_text(
                "Утренний дайджест выключен.\n"
                "Включить: /morning 09:00"
            )
        return
    arg = context.args[0].strip().lower()
    if arg in ("off", "выкл", "выключить", "0", "no", "нет"):
        for j in context.job_queue.get_jobs_by_name(f"morning:{user_id}"):
            j.schedule_removal()
        db_set_morning(user_id, chat_id, None)
        await update.message.reply_text("🌅 Утренний дайджест выключен.")
        return
    t = parse_hhmm(arg)
    if not t:
        await update.message.reply_text(
            "Использование: /morning 09:00 или /morning off"
        )
        return
    hhmm = f"{t.hour:02d}:{t.minute:02d}"
    db_set_morning(user_id, chat_id, hhmm)
    schedule_morning(context, user_id, chat_id, hhmm)
    await update.message.reply_html(
        f"🌅 Буду присылать список дел каждое утро в <b>{hhmm}</b>."
    )


async def cmd_clear(update: Update, _: ContextTypes.DEFAULT_TYPE):
    n = db_count_done(update.effective_user.id)
    if n == 0:
        await update.message.reply_text("Выполненных задач нет.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🗑 Удалить ({n})", callback_data="clear:yes"),
        InlineKeyboardButton("Отмена", callback_data="clear:no"),
    ]])
    await update.message.reply_html(
        f"Удалить <b>{n}</b> выполненных задач?",
        reply_markup=kb,
    )


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = db_stats(user_id)
    morning = db_get_morning(user_id)
    has_morning = bool(morning and morning["morning_time"])
    if s["total"] == 0 and not has_morning:
        await update.message.reply_text("Нечего удалять — у тебя пока пусто 🙂")
        return
    lines = ["🔥 <b>Удалить ВСЕ твои данные?</b>", ""]
    lines.append(f"• Задач: <b>{s['total']}</b> (выполнено: {s['done']}, в работе: {s['pending']})")
    if has_morning:
        lines.append(
            f"• Утренний дайджест: <b>{morning['morning_time']}</b> — будет выключен"
        )
    lines.append("• Все напоминания будут сняты")
    lines.append("")
    lines.append("⚠️ Это <b>необратимо</b>.")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔥 Удалить всё", callback_data="reset:yes"),
        InlineKeyboardButton("Отмена", callback_data="reset:no"),
    ]])
    await update.message.reply_html("\n".join(lines), reply_markup=kb)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /done 12")
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    row = db_get_task(tid, update.effective_user.id)
    if not row:
        await update.message.reply_text("Такой задачи нет")
        return
    was_done = bool(row["done"])
    db_toggle_done(tid, update.effective_user.id)
    if not was_done:
        await send_reaction(
            context.bot, update.effective_chat.id, PHOTO_DONE,
            f"<b>Молодец!</b> 💪\nЗадача <b>#{tid}</b> выполнена:\n<i>{row['text']}</i>",
        )
    else:
        await update.message.reply_html(
            f"Вернул #{tid} в работу:\n<i>{row['text']}</i>"
        )


async def cmd_prio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /prio 12 <0|1|2|3>\n"
            "  0 ▫️ — обычная\n"
            "  1 🟡 — важно / запланировать (можно «!»)\n"
            "  2 🔴 — срочно+важно / сделать сейчас (можно «!!»)\n"
            "  3 🟣 — срочно / делегировать"
        )
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    raw = " ".join(context.args[1:]).strip().lower()
    aliases = {
        "0": 0, "обычная": 0, "обычный": 0, "обычно": 0, "norm": 0, "normal": 0,
        "1": 1, "!": 1, "важно": 1, "важная": 1, "high": 1,
        "запланировать": 1, "план": 1, "plan": 1, "schedule": 1,
        "2": 2, "!!": 2, "срочно": 2, "срочная": 2, "urgent": 2,
        "сделать": 2, "сейчас": 2, "do": 2, "now": 2,
        "3": 3, "делегировать": 3, "deleg": 3, "delegate": 3, "перепоручить": 3,
    }
    if raw not in aliases:
        await update.message.reply_text(
            "Не понял уровень. Используй 0, 1, 2, 3 или !, !!, делегировать"
        )
        return
    if not db_get_task(tid, update.effective_user.id):
        await update.message.reply_text("Такой задачи нет")
        return
    p = aliases[raw]
    db_set_priority(tid, update.effective_user.id, p)
    await update.message.reply_html(
        f"#{tid} → {PRIO_ICONS[p]} <i>{PRIO_NAMES[p]}</i>"
    )


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /del 12")
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    row = db_get_task(tid, update.effective_user.id)
    db_delete_task(tid, update.effective_user.id)
    for job in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
        job.schedule_removal()
    if row:
        await send_reaction(
            context.bot, update.effective_chat.id, PHOTO_DELETE,
            f"<b>ОКЕЙ</b>\nУдалил <b>#{tid}</b>:\n<i>{row['text']}</i>",
        )
    else:
        await update.message.reply_text(f"Удалил #{tid}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user_id = q.from_user.id
    chat_id = q.message.chat_id if q.message else None

    # 2-part callbacks (confirmation flows)
    if data == "clear:yes":
        n = db_clear_done(user_id)
        try:
            await q.edit_message_text(f"🗑 Удалил выполненных задач: <b>{n}</b>.",
                                      parse_mode=ParseMode.HTML)
        except Exception as e:
            log.debug("edit_message_text failed: %s", e)
        return
    if data == "clear:no":
        try:
            await q.edit_message_text("Отменено.")
        except Exception as e:
            log.debug("edit_message_text failed: %s", e)
        return
    if data == "reset:yes":
        task_ids = db_get_user_task_ids(user_id)
        for tid in task_ids:
            for j in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
                j.schedule_removal()
        for j in context.job_queue.get_jobs_by_name(f"morning:{user_id}"):
            j.schedule_removal()
        n = db_delete_all_user_data(user_id)
        try:
            await q.edit_message_text(
                f"🔥 Готово. Удалено задач: <b>{n}</b>.\n"
                f"Утренний дайджест выключен, напоминания сняты. Чистый лист.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.debug("edit_message_text failed: %s", e)
        return
    if data == "reset:no":
        try:
            await q.edit_message_text("Отменено. Ничего не удалил.")
        except Exception as e:
            log.debug("edit_message_text failed: %s", e)
        return

    parts = data.split(":", 2)
    if len(parts) < 3:
        return
    action, sid, view = parts
    try:
        tid = int(sid)
    except ValueError:
        return

    # rsnz: snooze the reminder itself by N minutes (third part is minutes, not a view)
    if action == "rsnz":
        try:
            minutes = int(view)
        except ValueError:
            return
        row = db_get_task(tid, user_id)
        if not row:
            return
        now = datetime.now(TZ)
        new_remind = now + timedelta(minutes=minutes)
        cur_date = date.fromisoformat(row["task_date"])
        new_date = max(cur_date, new_remind.date())
        db_update_schedule(tid, user_id, new_date, new_remind)
        for j in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
            j.schedule_removal()
        context.job_queue.run_once(
            send_reminder,
            when=(new_remind - now).total_seconds(),
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            name=f"remind:{tid}",
            data={"task_id": tid},
        )
        when_label = new_remind.strftime("%H:%M")
        if new_date != date.fromisoformat(row["task_date"]) or new_date != now.date():
            when_label = f"{fmt_date(new_date)} в {when_label}"
        try:
            await q.edit_message_text(
                f"⏰ Перенёс на <b>{when_label}</b>:\n<i>{row['text']}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.debug("edit_message_text failed: %s", e)
        return

    reaction = None  # (photo_path, caption) to send after refresh
    notice = None  # plain text to send as separate message

    if action == "done":
        row = db_get_task(tid, user_id)
        if row:
            was_done = bool(row["done"])
            db_toggle_done(tid, user_id)
            if not was_done and chat_id is not None:
                reaction = (
                    PHOTO_DONE,
                    f"<b>Молодец!</b> 💪\nЗадача <b>#{tid}</b> выполнена:\n<i>{row['text']}</i>",
                )
    elif action == "del":
        row = db_get_task(tid, user_id)
        db_delete_task(tid, user_id)
        for job in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
            job.schedule_removal()
        if row and chat_id is not None:
            reaction = (
                PHOTO_DELETE,
                f"<b>ОКЕЙ</b>\nУдалил <b>#{tid}</b>:\n<i>{row['text']}</i>",
            )
    elif action == "prio":
        row = db_get_task(tid, user_id)
        if row:
            db_set_priority(tid, user_id, (row["priority"] + 1) % 4)
    elif action == "snz":
        row = db_get_task(tid, user_id)
        if row:
            new_date = compute_snooze_date(row)
            new_remind = reschedule_task_reminder(context, row, new_date)
            db_update_schedule(tid, user_id, new_date, new_remind)
            notice = (
                f"⏭ <b>#{tid}</b> отложена на <b>{fmt_date(new_date)}</b>"
            )

    if view == "m":
        rows = db_list_matrix(user_id)
        text = render_matrix(rows)
        kb = tasks_keyboard(rows, "m")
    else:
        rows, header = view_for(user_id, view)
        text = render_tasks(rows, header)
        kb = tasks_keyboard(rows, view)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug("edit_message_text failed: %s", e)

    if reaction and chat_id is not None:
        photo_path, caption = reaction
        await send_reaction(context.bot, chat_id, photo_path, caption)
    if notice and chat_id is not None:
        await context.bot.send_message(
            chat_id=chat_id, text=notice, parse_mode=ParseMode.HTML
        )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    tid = data.get("task_id")
    if tid is None:
        return
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if not row or row["done"]:
        return
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Напоминание <b>#{row['id']}</b>\n<i>{row['text']}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏰ +15м", callback_data=f"rsnz:{row['id']}:15"),
                InlineKeyboardButton("⏰ +1ч", callback_data=f"rsnz:{row['id']}:60"),
                InlineKeyboardButton("⏰ +1д", callback_data=f"rsnz:{row['id']}:1440"),
            ],
            [
                InlineKeyboardButton("✅ Готово", callback_data=f"done:{row['id']}:t"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{row['id']}:t"),
            ],
        ]),
    )


async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    user_id = data.get("user_id")
    if user_id is None:
        return
    today = datetime.now(TZ).date()
    rows = db_list_tasks(user_id, day=today)
    if not rows:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text="<b>🌅 Доброе утро!</b>\nСегодня дел нет — отдыхай 🙂",
            parse_mode=ParseMode.HTML,
        )
        return
    text = render_tasks(rows, "🌅 Доброе утро! План на сегодня")
    kb = tasks_keyboard(rows, "t")
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


def schedule_morning(app, user_id, chat_id, hhmm_str):
    job_name = f"morning:{user_id}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()
    t = parse_hhmm(hhmm_str)
    if not t:
        return False
    app.job_queue.run_daily(
        send_morning_digest,
        time=t.replace(tzinfo=TZ),
        chat_id=chat_id,
        user_id=user_id,
        name=job_name,
        data={"user_id": user_id},
    )
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    lower = text.lower()

    if lower in ("сегодня", "today", "📅 сегодня"):
        return await cmd_today(update, context)
    if lower in ("завтра", "tomorrow", "📆 завтра"):
        return await cmd_tomorrow(update, context)
    if lower in ("неделя", "week", "🗓 неделя"):
        return await cmd_week(update, context)
    if lower in ("все", "всё", "all"):
        return await cmd_all(update, context)
    if lower in ("просрочено", "просроченные", "overdue", "⚠️ просрочено"):
        return await cmd_overdue(update, context)
    if lower in ("стата", "статистика", "stats", "📈 стата"):
        return await cmd_stats(update, context)
    if lower in ("матрица", "матрица эйзенхауэра", "matrix", "eisenhower", "📊 матрица"):
        return await cmd_matrix(update, context)
    if lower in ("помощь", "help", "❓ помощь"):
        return await cmd_help(update, context)

    parsed = parse_input(text)
    if not parsed:
        await update.message.reply_text(
            "Не разобрал. Пример:\n"
            "  • сегодня позвонить маме\n"
            "  • 25.05 14:30 встреча\n"
            "Подсказка: /help"
        )
        return

    d, t, txt = parsed
    prio, txt = extract_priority(txt)
    if not txt:
        await update.message.reply_text("Пустой текст задачи. Добавь описание.")
        return
    remind_at = None
    schedule = False
    if t is not None:
        remind_at = datetime.combine(d, t, tzinfo=TZ)
        schedule = remind_at > datetime.now(TZ)

    tid = db_add_task(
        update.effective_user.id,
        update.effective_chat.id,
        d, txt,
        remind_at=remind_at,
        priority=prio,
    )

    if schedule:
        delay = (remind_at - datetime.now(TZ)).total_seconds()
        context.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=update.effective_chat.id,
            user_id=update.effective_user.id,
            name=f"remind:{tid}",
            data={"task_id": tid},
        )

    when = fmt_date(d) + (f" в {t.strftime('%H:%M')}" if t else "")
    tail = ""
    if t and schedule:
        tail = "\n⏰ Напомню в указанное время."
    elif t and not schedule:
        tail = "\n⚠️ Время уже прошло — напоминание не ставлю."
    else:
        tail = (
            f"\n💡 Хочешь напоминание? <code>/remind {tid} 18:00</code> "
            f"или добавь время в текст задачи."
        )
    prio_label = ""
    if prio:
        prio_label = f" {PRIO_ICONS[prio]} <i>({PRIO_NAMES[prio]})</i>"
    caption = (
        f"<b>Умничка!</b> 🎉\n"
        f"Добавил <b>#{tid}</b>{prio_label} на <b>{when}</b>:\n<i>{txt}</i>{tail}"
    )
    await send_reaction(context.bot, update.effective_chat.id, PHOTO_ADD, caption)


async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([BotCommand(*c) for c in BOT_COMMANDS])
    except Exception as e:
        log.warning("set_my_commands failed: %s", e)
    await restore_reminders(app)


async def restore_reminders(app: Application):
    rows = db_pending_reminders()
    now = datetime.now(TZ)
    restored = 0
    for r in rows:
        try:
            ra = datetime.fromisoformat(r["remind_at"])
        except ValueError:
            continue
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=TZ)
        delay = (ra - now).total_seconds()
        if delay <= 0:
            continue
        app.job_queue.run_once(
            send_reminder,
            when=delay,
            chat_id=r["chat_id"],
            user_id=r["user_id"],
            name=f"remind:{r['id']}",
            data={"task_id": r["id"]},
        )
        restored += 1
    log.info("Restored %d pending reminders (of %d on disk)", restored, len(rows))

    morning_n = 0
    for s in db_all_morning():
        if schedule_morning(app, s["user_id"], s["chat_id"], s["morning_time"]):
            morning_n += 1
    log.info("Scheduled %d morning digests", morning_n)


def main():
    if not TOKEN:
        raise SystemExit(
            "BOT_TOKEN env var not set. Get a token from @BotFather and run:\n"
            '  PowerShell: $env:BOT_TOKEN = "123456:ABC..."; python bot.py\n'
            "  cmd.exe:    set BOT_TOKEN=123456:ABC... && python bot.py"
        )
    db_init()
    app = (
        Application.builder()
        .token(TOKEN)
        .defaults(Defaults(tzinfo=TZ))
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("overdue", cmd_overdue))
    app.add_handler(CommandHandler("matrix", cmd_matrix))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("prio", cmd_prio))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Starting bot... TZ=%s", TZ_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
