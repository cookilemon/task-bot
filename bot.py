"""
Telegram Task Manager Bot — v2
────────────────────────────────
• Задачи с дедлайнами и проектами
• Повторяющиеся задачи (ежедневно / еженедельно / каждые N дней / вручную)
• Напоминания: за 1ч, утром в день дедлайна, в настраиваемое время
• Группировка по проектам
• Настройки пользователя (timezone, время утреннего дайджеста)
"""

import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH         = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ      = "Europe/Moscow"
DEFAULT_MORNING = "09:00"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── БД ──────────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            title           TEXT    NOT NULL,
            project         TEXT    DEFAULT 'Без проекта',
            deadline        TEXT,
            priority        TEXT    DEFAULT 'normal',
            repeat_type     TEXT    DEFAULT 'none',
            repeat_value    TEXT,
            done            INTEGER DEFAULT 0,
            remind_custom   TEXT,
            created_at      TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id         INTEGER PRIMARY KEY,
            timezone        TEXT    DEFAULT 'Europe/Moscow',
            morning_time    TEXT    DEFAULT '09:00',
            remind_1h       INTEGER DEFAULT 1,
            remind_morning  INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS sent_reminders (
            task_id     INTEGER,
            remind_type TEXT,
            sent_date   TEXT,
            PRIMARY KEY (task_id, remind_type, sent_date)
        );
        """)

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_settings(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {"user_id": user_id, "timezone": DEFAULT_TZ,
            "morning_time": DEFAULT_MORNING, "remind_1h": 1, "remind_morning": 1}

def save_settings(user_id: int, **kwargs):
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        if not exists:
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(
            f"UPDATE user_settings SET {sets} WHERE user_id=?",
            (*kwargs.values(), user_id)
        )

def user_now(user_id: int) -> datetime:
    tz_str = get_settings(user_id).get("timezone", DEFAULT_TZ)
    try:
        return datetime.now(ZoneInfo(tz_str))
    except Exception:
        return datetime.now(ZoneInfo(DEFAULT_TZ))

# ─── Форматирование ───────────────────────────────────────────────────────────
PRI_EMOJI  = {"high": "🔴", "normal": "🟡", "low": "🟢"}
WEEK_DAYS  = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def repeat_label(rtype: str, rvalue: str) -> str:
    if rtype == "none":   return ""
    if rtype == "daily":  return f"🔁 ежедневно в {rvalue}"
    if rtype == "weekly":
        try:
            wd, t = rvalue.split("/")
            return f"🔁 каждый {WEEK_DAYS[int(wd)]} в {t}"
        except Exception:
            return "🔁 еженедельно"
    if rtype == "interval":
        try:
            n, t = rvalue.split("/")
            return f"🔁 каждые {n} дн. в {t}"
        except Exception:
            return f"🔁 каждые {rvalue} дн."
    return ""

def format_task(row: dict, now: datetime = None) -> str:
    if now is None:
        now = datetime.now()
    status = "✅" if row["done"] else "⬜"
    pri    = PRI_EMOJI.get(row["priority"], "🟡")
    dl_str = ""
    if row["deadline"]:
        dl   = datetime.fromisoformat(row["deadline"])
        diff = (dl.date() - now.date()).days
        t    = dl.strftime("%H:%M")
        if   diff < 0:  dl_str = f"\n    ⚠️ <i>просрочено {dl.strftime('%d.%m')} {t}</i>"
        elif diff == 0: dl_str = f"\n    🔥 <i>сегодня до {t}</i>"
        elif diff == 1: dl_str = f"\n    📅 <i>завтра в {t}</i>"
        else:           dl_str = f"\n    📅 <i>{dl.strftime('%d.%m.%Y')} {t}</i>"
    rep = repeat_label(row.get("repeat_type", "none"), row.get("repeat_value") or "")
    rep_str    = f"\n    {rep}" if rep else ""
    custom_str = ""
    if row.get("remind_custom"):
        rc = datetime.fromisoformat(row["remind_custom"])
        custom_str = f"\n    🔔 напомнить {rc.strftime('%d.%m %H:%M')}"
    return (
        f"{status} {pri} <b>[{row['id']}]</b> {row['title']}"
        f"\n    📁 {row['project']}{dl_str}{rep_str}{custom_str}"
    )

# ─── Парсинг ──────────────────────────────────────────────────────────────────
def parse_deadline(text: str, tz: str = DEFAULT_TZ) -> str | None:
    text = text.strip()
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo(DEFAULT_TZ)
    now = datetime.now(zone)

    m = re.match(r"^\+(\d+)[дdD]?$", text)
    if m:
        dt = (now + timedelta(days=int(m.group(1)))).replace(
            hour=9, minute=0, second=0, microsecond=0)
        return dt.isoformat()

    ltext = text.lower()
    rel = {"сегодня": 0, "today": 0, "завтра": 1, "tomorrow": 1, "послезавтра": 2}
    if ltext in rel:
        dt = (now + timedelta(days=rel[ltext])).replace(
            hour=9, minute=0, second=0, microsecond=0)
        return dt.isoformat()

    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%d.%m.%Y", "%d.%m"):
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt: dt = dt.replace(year=now.year)
            if "%H" not in fmt: dt = dt.replace(hour=9, minute=0)
            dt = dt.replace(second=0, microsecond=0, tzinfo=zone)
            return dt.isoformat()
        except ValueError:
            continue
    return None

def parse_repeat(text: str) -> tuple[str, str] | None:
    t = text.strip().lower()
    m = re.match(r"^daily\s+(\d{1,2}:\d{2})$", t)
    if m: return ("daily", m.group(1))
    m = re.match(r"^weekly\s+(\d)\s+(\d{1,2}:\d{2})$", t)
    if m: return ("weekly", f"{m.group(1)}/{m.group(2)}")
    m = re.match(r"^(?:every|каждые?)\s+(\d+)\s+(\d{1,2}:\d{2})$", t)
    if m: return ("interval", f"{m.group(1)}/{m.group(2)}")
    return None

# ─── Команды ─────────────────────────────────────────────────────────────────
HELP_TEXT = """👋 <b>Task Bot v2</b>

<b>➕ Добавить задачу:</b>
<code>/add Название | Проект | Дедлайн | Приоритет | Повтор</code>

Примеры:
<code>/add Сдать отчёт | Работа | 20.03 18:00 | high</code>
<code>/add Зарядка | Здоровье | завтра 07:30 | normal | daily 07:30</code>
<code>/add Планёрка | Работа | 17.03 10:00 | high | weekly 0 10:00</code>
<code>/add Платёж ЖКХ | Финансы | +30д | normal | every 30 09:00</code>

<b>Дедлайн:</b> <code>20.03</code> · <code>20.03 18:00</code> · <code>+5д</code> · <code>завтра</code>
<b>Приоритет:</b> <code>high</code>🔴 · <code>normal</code>🟡 · <code>low</code>🟢
<b>Повтор:</b> <code>daily HH:MM</code> · <code>weekly N HH:MM</code> · <code>every N HH:MM</code>
(N для weekly: 0=Пн, 1=Вт ... 6=Вс)

<b>📋 Списки:</b>
/list — все задачи по проектам
/list Работа — задачи одного проекта
/today — только сегодня
/projects — обзор всех проектов

<b>✅ Управление:</b>
/done 3 — выполнить (повтор создаст следующую)
/del 3 — удалить
/remind 3 25.03 15:00 — своё напоминание

<b>⚙️ Настройки:</b>
/settings — все настройки
/settz Europe/Moscow — часовой пояс
/setmorning 08:30 — время утреннего дайджеста

Или просто напиши задачу без команды!
"""

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw = " ".join(ctx.args).strip() if ctx.args else ""
    if not raw:
        await update.message.reply_text(
            "Формат: <code>/add Название | Проект | Дедлайн | Приоритет | Повтор</code>\n"
            "Напиши /help для примеров.", parse_mode="HTML")
        return
    await _save_task(update, user_id, raw)

async def _save_task(update: Update, user_id: int, raw: str):
    parts    = [p.strip() for p in raw.split("|")]
    title    = parts[0] if parts else raw
    project  = parts[1] if len(parts) > 1 and parts[1] else "Без проекта"
    dl_raw   = parts[2] if len(parts) > 2 and parts[2] else None
    pri_raw  = (parts[3] if len(parts) > 3 else "normal").lower()
    rep_raw  = parts[4] if len(parts) > 4 and parts[4] else None

    priority = pri_raw if pri_raw in ("high", "normal", "low") else "normal"
    settings = get_settings(user_id)
    deadline = parse_deadline(dl_raw, settings["timezone"]) if dl_raw else None

    repeat_type, repeat_value = "none", None
    if rep_raw:
        parsed = parse_repeat(rep_raw)
        if parsed:
            repeat_type, repeat_value = parsed

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id,title,project,deadline,priority,repeat_type,repeat_value)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, title, project, deadline, priority, repeat_type, repeat_value)
        )
        task_id = cur.lastrowid

    dl_str  = datetime.fromisoformat(deadline).strftime("%d.%m.%Y %H:%M") if deadline else "без дедлайна"
    rep_str = repeat_label(repeat_type, repeat_value or "")
    msg = (
        f"✅ <b>#{task_id}</b> добавлена!\n"
        f"{PRI_EMOJI.get(priority,'🟡')} <b>{title}</b>\n"
        f"📁 {project}   📅 {dl_str}"
    )
    if rep_str:
        msg += f"\n{rep_str}"
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    proj_filter = " ".join(ctx.args).strip() if ctx.args else None
    now = user_now(user_id)

    with get_conn() as conn:
        if proj_filter:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND done=0 AND project LIKE ?"
                " ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline",
                (user_id, f"%{proj_filter}%")
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND done=0"
                " ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline",
                (user_id,)
            ).fetchall()

    if not rows:
        await update.message.reply_text("📭 Задач нет! Можно отдыхать 🎉")
        return

    projects: dict[str, list] = {}
    for r in rows:
        projects.setdefault(r["project"], []).append(dict(r))

    parts = [f"📋 <b>Задачи{' / ' + proj_filter if proj_filter else ''}</b> ({len(rows)})\n"]
    for proj, tasks in projects.items():
        block = f"\n<b>📁 {proj}</b>\n"
        block += "\n".join(format_task(t, now) for t in tasks)
        parts.append(block)

    text = "\n".join(parts)
    keyboard = [[
        InlineKeyboardButton("✅ /done N",  callback_data="hint_done"),
        InlineKeyboardButton("🗑 /del N",   callback_data="hint_del"),
        InlineKeyboardButton("📁 проекты", callback_data="hint_proj"),
    ]]
    # Telegram limit — split if needed
    if len(text) > 4000:
        await update.message.reply_text(text[:4000] + "\n…", parse_mode="HTML")
        await update.message.reply_text(
            text[4000:], parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now   = user_now(user_id)
    start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    end   = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND done=0"
            " AND deadline BETWEEN ? AND ? ORDER BY deadline",
            (user_id, start, end)
        ).fetchall()

    if not rows:
        await update.message.reply_text("🎉 На сегодня задач нет!")
        return

    text = f"🔥 <b>Сегодня, {now.strftime('%d.%m')}:</b>\n\n"
    text += "\n\n".join(format_task(dict(r), now) for r in rows)
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) cnt,"
            " SUM(CASE WHEN priority='high' THEN 1 ELSE 0 END) high_cnt"
            " FROM tasks WHERE user_id=? AND done=0"
            " GROUP BY project ORDER BY cnt DESC",
            (user_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("Проектов нет.")
        return
    lines = []
    for r in rows:
        hi = f"  🔴×{r['high_cnt']}" if r["high_cnt"] else ""
        lines.append(f"📁 <b>{r['project']}</b> — {r['cnt']} задач{hi}")
    await update.message.reply_text(
        "📂 <b>Проекты:</b>\n\n" + "\n".join(lines), parse_mode="HTML")

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Укажи номер: /done 3")
        return
    task_id = ctx.args[0]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
        ).fetchone()
        if not row:
            await update.message.reply_text(f"Задача #{task_id} не найдена.")
            return
        row = dict(row)
        if row["repeat_type"] != "none" and row["deadline"]:
            next_dl = _next_deadline(row)
            if next_dl:
                conn.execute(
                    "INSERT INTO tasks (user_id,title,project,deadline,priority,repeat_type,repeat_value)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (user_id, row["title"], row["project"], next_dl,
                     row["priority"], row["repeat_type"], row["repeat_value"])
                )
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))

    reply = f"✅ <b>#{task_id}</b> выполнена! 🎉"
    if row["repeat_type"] != "none":
        reply += "\n🔁 Создана следующая итерация."
    await update.message.reply_text(reply, parse_mode="HTML")

def _next_deadline(row: dict) -> str | None:
    try:
        cur    = datetime.fromisoformat(row["deadline"])
        rtype  = row["repeat_type"]
        rvalue = row["repeat_value"] or ""
        if rtype == "daily":
            h, m = map(int, rvalue.split(":"))
            return (cur + timedelta(days=1)).replace(hour=h, minute=m, second=0).isoformat()
        if rtype == "weekly":
            wd, ts = rvalue.split("/")
            h, m   = map(int, ts.split(":"))
            target = int(wd)
            delta  = (target - cur.weekday() + 7) % 7 or 7
            return (cur + timedelta(days=delta)).replace(hour=h, minute=m, second=0).isoformat()
        if rtype == "interval":
            n, ts = rvalue.split("/")
            h, m  = map(int, ts.split(":"))
            return (cur + timedelta(days=int(n))).replace(hour=h, minute=m, second=0).isoformat()
    except Exception as e:
        log.warning(f"_next_deadline: {e}")
    return None

async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Укажи номер: /del 3")
        return
    task_id = ctx.args[0]
    with get_conn() as conn:
        affected = conn.execute(
            "DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
        ).rowcount
    if affected:
        await update.message.reply_text(f"🗑 Задача #{task_id} удалена.")
    else:
        await update.message.reply_text(f"Задача #{task_id} не найдена.")

async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Формат: /remind <номер> <дата> <время>\n"
            "Пример: <code>/remind 3 25.03 15:00</code>", parse_mode="HTML")
        return
    task_id  = ctx.args[0]
    date_str = " ".join(ctx.args[1:])
    settings = get_settings(user_id)
    dl = parse_deadline(date_str, settings["timezone"])
    if not dl:
        await update.message.reply_text(
            "Не смог распознать дату. Пример: <code>25.03 15:00</code>", parse_mode="HTML")
        return
    with get_conn() as conn:
        affected = conn.execute(
            "UPDATE tasks SET remind_custom=? WHERE id=? AND user_id=?",
            (dl, task_id, user_id)
        ).rowcount
    if affected:
        dt = datetime.fromisoformat(dl)
        await update.message.reply_text(
            f"🔔 Напомню о задаче #{task_id} в <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>",
            parse_mode="HTML")
    else:
        await update.message.reply_text(f"Задача #{task_id} не найдена.")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    r1h  = "✅" if s["remind_1h"]      else "❌"
    rmrn = "✅" if s["remind_morning"] else "❌"
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"🌍 Часовой пояс: <code>{s['timezone']}</code>\n"
        f"⏰ Утренний дайджест: <code>{s['morning_time']}</code>\n"
        f"🔔 Напоминание за 1ч: {r1h}\n"
        f"🌅 Напоминание утром: {rmrn}\n\n"
        f"<i>/settz Europe/Moscow — изменить TZ\n"
        f"/setmorning 08:30 — изменить время дайджеста</i>"
    )
    keyboard = [[
        InlineKeyboardButton(
            f"{'Выкл' if s['remind_1h'] else 'Вкл'} напом. за 1ч",
            callback_data="toggle_1h"),
        InlineKeyboardButton(
            f"{'Выкл' if s['remind_morning'] else 'Вкл'} утреннее",
            callback_data="toggle_morning"),
    ]]
    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_settz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Формат: /settz Europe/Moscow")
        return
    tz = ctx.args[0]
    try:
        ZoneInfo(tz)
    except Exception:
        await update.message.reply_text(f"❌ Неверный часовой пояс: {tz}")
        return
    save_settings(user_id, timezone=tz)
    await update.message.reply_text(f"✅ Часовой пояс: {tz}")

async def cmd_setmorning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args or not re.match(r"^\d{1,2}:\d{2}$", ctx.args[0]):
        await update.message.reply_text("Формат: /setmorning 08:30")
        return
    save_settings(user_id, morning_time=ctx.args[0])
    await update.message.reply_text(f"✅ Утренний дайджест в {ctx.args[0]}")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data

    if data == "hint_done":
        await query.message.reply_text("Напиши: /done <номер>")
    elif data == "hint_del":
        await query.message.reply_text("Напиши: /del <номер>")
    elif data == "hint_proj":
        await query.message.reply_text("Напиши: /list <название проекта>")
    elif data == "toggle_1h":
        s = get_settings(user_id)
        save_settings(user_id, remind_1h=0 if s["remind_1h"] else 1)
        # Обновляем сообщение настроек
        s2 = get_settings(user_id)
        r1h  = "✅" if s2["remind_1h"]      else "❌"
        rmrn = "✅" if s2["remind_morning"] else "❌"
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"🌍 Часовой пояс: <code>{s2['timezone']}</code>\n"
            f"⏰ Утренний дайджест: <code>{s2['morning_time']}</code>\n"
            f"🔔 Напоминание за 1ч: {r1h}\n"
            f"🌅 Напоминание утром: {rmrn}"
        )
        keyboard = [[
            InlineKeyboardButton(
                f"{'Выкл' if s2['remind_1h'] else 'Вкл'} напом. за 1ч",
                callback_data="toggle_1h"),
            InlineKeyboardButton(
                f"{'Выкл' if s2['remind_morning'] else 'Вкл'} утреннее",
                callback_data="toggle_morning"),
        ]]
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "toggle_morning":
        s = get_settings(user_id)
        save_settings(user_id, remind_morning=0 if s["remind_morning"] else 1)
        s2 = get_settings(user_id)
        r1h  = "✅" if s2["remind_1h"]      else "❌"
        rmrn = "✅" if s2["remind_morning"] else "❌"
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"🌍 Часовой пояс: <code>{s2['timezone']}</code>\n"
            f"⏰ Утренний дайджест: <code>{s2['morning_time']}</code>\n"
            f"🔔 Напоминание за 1ч: {r1h}\n"
            f"🌅 Напоминание утром: {rmrn}"
        )
        keyboard = [[
            InlineKeyboardButton(
                f"{'Выкл' if s2['remind_1h'] else 'Вкл'} напом. за 1ч",
                callback_data="toggle_1h"),
            InlineKeyboardButton(
                f"{'Выкл' if s2['remind_morning'] else 'Вкл'} утреннее",
                callback_data="toggle_morning"),
        ]]
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    await _save_task(update, user_id, text)

# ─── Планировщик ─────────────────────────────────────────────────────────────
async def job_reminders(app: Application):
    with get_conn() as conn:
        tasks = conn.execute(
            "SELECT t.*, s.timezone, s.remind_1h, s.remind_morning, s.morning_time"
            " FROM tasks t"
            " LEFT JOIN user_settings s ON t.user_id = s.user_id"
            " WHERE t.done=0"
        ).fetchall()

    for raw in tasks:
        row = dict(raw)
        tz_str = row.get("timezone") or DEFAULT_TZ
        try:
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = ZoneInfo(DEFAULT_TZ)
        now_local  = datetime.now(tz)
        today_str  = now_local.strftime("%Y-%m-%d")

        if row["deadline"]:
            dl = datetime.fromisoformat(row["deadline"])
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=tz)

            # Напоминание за 1 час
            if row.get("remind_1h", 1):
                diff = (dl - now_local).total_seconds()
                if 3000 <= diff <= 3900:  # 50–65 минут
                    await _send_if_not_sent(app, row, "1h", today_str,
                        f"🔥 До дедлайна <b>1 час!</b>\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}")

            # Утреннее напоминание
            if row.get("remind_morning", 1) and dl.date() == now_local.date():
                mt = row.get("morning_time") or DEFAULT_MORNING
                mh, mm = map(int, mt.split(":"))
                morning = now_local.replace(hour=mh, minute=mm, second=0, microsecond=0)
                if now_local >= morning:
                    await _send_if_not_sent(app, row, "morning", today_str,
                        f"🌅 Сегодня дедлайн!\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}\n🕐 до {dl.strftime('%H:%M')}")

        # Кастомное напоминание
        if row.get("remind_custom"):
            try:
                rc = datetime.fromisoformat(row["remind_custom"])
                if rc.tzinfo is None:
                    rc = rc.replace(tzinfo=tz)
                diff = abs((rc - now_local).total_seconds())
                if diff <= 300:
                    sent = await _send_if_not_sent(app, row, "custom", today_str,
                        f"🔔 Напоминание!\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}")
                    if sent:
                        with get_conn() as conn:
                            conn.execute("UPDATE tasks SET remind_custom=NULL WHERE id=?", (row["id"],))
            except Exception as e:
                log.warning(f"custom remind error: {e}")

async def _send_if_not_sent(app, row, rtype, today_str, text) -> bool:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sent_reminders WHERE task_id=? AND remind_type=? AND sent_date=?",
            (row["id"], rtype, today_str)
        ).fetchone()
    if exists:
        return False
    try:
        await app.bot.send_message(row["user_id"], text, parse_mode="HTML")
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent_reminders VALUES (?,?,?)",
                (row["id"], rtype, today_str)
            )
        return True
    except Exception as e:
        log.warning(f"send_message failed uid={row['user_id']}: {e}")
        return False

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_start))
    app.add_handler(CommandHandler("add",        cmd_add))
    app.add_handler(CommandHandler("list",       cmd_list))
    app.add_handler(CommandHandler("today",      cmd_today))
    app.add_handler(CommandHandler("projects",   cmd_projects))
    app.add_handler(CommandHandler("done",       cmd_done))
    app.add_handler(CommandHandler("del",        cmd_del))
    app.add_handler(CommandHandler("remind",     cmd_remind))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("settz",      cmd_settz))
    app.add_handler(CommandHandler("setmorning", cmd_setmorning))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_reminders, "interval", minutes=5, args=[app])
    scheduler.start()

    log.info("🤖 Task Bot v2 запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
