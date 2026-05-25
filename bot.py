import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "planbot.db"
TZ_NAME = os.environ.get("PLANBOT_TZ", "Asia/Almaty")
TZ = ZoneInfo(TZ_NAME)
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "BOT_TOKEN env var not set. Get a token from @BotFather and run:\n"
        '  PowerShell: $env:BOT_TOKEN = "123456:ABC..."; python bot.py\n'
        '  cmd.exe:    set BOT_TOKEN=123456:ABC... && python bot.py'
    )

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("planbot")


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
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_user_date ON tasks(user_id, task_date);
            CREATE INDEX IF NOT EXISTS idx_remind ON tasks(remind_at);
            """
        )


def db_add_task(user_id, chat_id, task_date, text, remind_at=None):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, chat_id, task_date, remind_at, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                chat_id,
                task_date.isoformat(),
                remind_at.isoformat() if remind_at else None,
                text,
                datetime.now(TZ).isoformat(),
            ),
        )
        return cur.lastrowid


def db_list_tasks(user_id, day=None, days_ahead=None):
    with db_connect() as conn:
        if day is not None:
            return conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND task_date = ? "
                "ORDER BY done, remind_at IS NULL, remind_at, id",
                (user_id, day.isoformat()),
            ).fetchall()
        today = datetime.now(TZ).date()
        if days_ahead is not None:
            end = today + timedelta(days=days_ahead)
            return conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND task_date BETWEEN ? AND ? "
                "ORDER BY task_date, done, remind_at IS NULL, remind_at, id",
                (user_id, today.isoformat(), end.isoformat()),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND task_date >= ? "
            "ORDER BY task_date, done, remind_at IS NULL, remind_at, id",
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
    mark = "✅" if row["done"] else "▫️"
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
        kb.append([
            InlineKeyboardButton(
                f"{flip} #{r['id']}",
                callback_data=f"done:{r['id']}:{view}",
            ),
            InlineKeyboardButton(
                f"🗑 #{r['id']}",
                callback_data=f"del:{r['id']}:{view}",
            ),
        ])
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
    if view_code.startswith("d"):
        try:
            d = date.fromisoformat(view_code[1:])
            return db_list_tasks(user_id, day=d), f"Дела на {fmt_date(d)}"
        except ValueError:
            pass
    return [], "Дела"


# ---------- Handlers ----------

HELP = (
    "Привет! Я помогу планировать дела по датам.\n\n"
    "<b>Как добавить задачу — просто напиши:</b>\n"
    "• <code>сегодня позвонить маме</code>\n"
    "• <code>завтра в 09:00 утренняя пробежка</code>\n"
    "• <code>25.05 купить молоко</code>\n"
    "• <code>25.05.2026 14:30 встреча с командой</code>\n"
    "• <code>2026-05-25 сдать отчёт</code>\n\n"
    "Если указано время (<code>HH:MM</code>) — пришлю напоминание в это время.\n\n"
    "<b>Команды:</b>\n"
    "/today — дела на сегодня\n"
    "/tomorrow — на завтра\n"
    "/week — на ближайшие 7 дней\n"
    "/all — все будущие дела\n"
    "/day 25.05.2026 — на конкретную дату\n"
    "/done 12 — переключить «сделано» для задачи #12\n"
    "/del 12 — удалить задачу #12\n"
    "/help — эта подсказка\n\n"
    "Под каждым списком — кнопки ✅/↩️ (готово / вернуть) и 🗑 (удалить)."
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP)


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP)


async def reply_view(update: Update, view_code: str):
    rows, header = view_for(update.effective_user.id, view_code)
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


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /done 12")
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    if not db_get_task(tid, update.effective_user.id):
        await update.message.reply_text("Такой задачи нет")
        return
    db_toggle_done(tid, update.effective_user.id)
    await update.message.reply_text(f"Переключил статус #{tid}")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /del 12")
        return
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом")
        return
    db_delete_task(tid, update.effective_user.id)
    for job in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
        job.schedule_removal()
    await update.message.reply_text(f"Удалил #{tid}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":", 2)
    if len(parts) < 3:
        return
    action, sid, view = parts
    try:
        tid = int(sid)
    except ValueError:
        return
    user_id = q.from_user.id

    if action == "done":
        if db_get_task(tid, user_id):
            db_toggle_done(tid, user_id)
    elif action == "del":
        db_delete_task(tid, user_id)
        for job in context.job_queue.get_jobs_by_name(f"remind:{tid}"):
            job.schedule_removal()

    rows, header = view_for(user_id, view)
    text = render_tasks(rows, header)
    kb = tasks_keyboard(rows, view)
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.debug("edit_message_text failed: %s", e)


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
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data=f"done:{row['id']}:t"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{row['id']}:t"),
        ]]),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    lower = text.lower()

    if lower in ("сегодня", "today"):
        return await cmd_today(update, context)
    if lower in ("завтра", "tomorrow"):
        return await cmd_tomorrow(update, context)
    if lower in ("неделя", "week"):
        return await cmd_week(update, context)
    if lower in ("все", "всё", "all"):
        return await cmd_all(update, context)

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
    await update.message.reply_html(
        f"Добавил <b>#{tid}</b> на <b>{when}</b>:\n<i>{txt}</i>{tail}"
    )


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


def main():
    db_init()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(restore_reminders)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Starting bot... TZ=%s", TZ_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
