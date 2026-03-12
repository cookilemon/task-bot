"""
Telegram Task Manager Bot — v3
• Главное меню с кнопками
• Пошаговое добавление задачи через ConversationHandler
• Управление проектами кнопками
• Повторяющиеся задачи
• Умные напоминания
"""

import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Конфиг ──────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
DB_PATH         = os.getenv("DB_PATH", "tasks.db")
DEFAULT_TZ      = "Europe/Moscow"
DEFAULT_MORNING = "09:00"

logging.basicConfig(format="%(asctime)s %(levelname)s — %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ConversationHandler states
(
    ADD_TITLE, ADD_PROJECT, ADD_PROJECT_NEW, ADD_DEADLINE, ADD_PRIORITY, ADD_REPEAT,
    PROJ_ACTION, PROJ_RENAME,
) = range(8)

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

def get_settings(user_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else {"user_id": user_id, "timezone": DEFAULT_TZ,
                                   "morning_time": DEFAULT_MORNING, "remind_1h": 1, "remind_morning": 1}

def save_settings(user_id, **kwargs):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM user_settings WHERE user_id=?", (user_id,)).fetchone():
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
        sets = ", ".join(f"{k}=?" for k in kwargs)
        conn.execute(f"UPDATE user_settings SET {sets} WHERE user_id=?", (*kwargs.values(), user_id))

def user_now(user_id):
    tz = get_settings(user_id).get("timezone", DEFAULT_TZ)
    try: return datetime.now(ZoneInfo(tz))
    except: return datetime.now(ZoneInfo(DEFAULT_TZ))

def get_projects(user_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) cnt FROM tasks WHERE user_id=? AND done=0 GROUP BY project ORDER BY cnt DESC",
            (user_id,)
        ).fetchall()
    projects = [r["project"] for r in rows]
    if "Без проекта" not in projects:
        projects.append("Без проекта")
    return projects

# ─── Форматирование ───────────────────────────────────────────────────────────
PRI_EMOJI  = {"high": "🔴", "normal": "🟡", "low": "🟢"}
WEEK_DAYS  = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]

def repeat_label(rtype, rvalue):
    if rtype == "none": return ""
    if rtype == "daily": return f"🔁 ежедневно в {rvalue}"
    if rtype == "weekly":
        try:
            wd, t = rvalue.split("/")
            return f"🔁 каждый {WEEK_DAYS[int(wd)]} в {t}"
        except: return "🔁 еженедельно"
    if rtype == "interval":
        try:
            n, t = rvalue.split("/")
            return f"🔁 каждые {n} дн. в {t}"
        except: return f"🔁 каждые {rvalue} дн."
    return ""

def format_task(row, now=None):
    if now is None: now = datetime.now()
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
    rep = repeat_label(row.get("repeat_type","none"), row.get("repeat_value") or "")
    rep_str = f"\n    {rep}" if rep else ""
    return f"{status} {pri} <b>[{row['id']}]</b> {row['title']}\n    📁 {row['project']}{dl_str}{rep_str}"

def parse_deadline(text, tz=DEFAULT_TZ):
    text = text.strip()
    try: zone = ZoneInfo(tz)
    except: zone = ZoneInfo(DEFAULT_TZ)
    now = datetime.now(zone)
    m = re.match(r"^\+(\d+)[дdD]?$", text)
    if m:
        return (now + timedelta(days=int(m.group(1)))).replace(hour=9,minute=0,second=0,microsecond=0).isoformat()
    rel = {"сегодня":0,"today":0,"завтра":1,"tomorrow":1,"послезавтра":2}
    if text.lower() in rel:
        return (now + timedelta(days=rel[text.lower()])).replace(hour=9,minute=0,second=0,microsecond=0).isoformat()
    for fmt in ("%d.%m.%Y %H:%M","%d.%m %H:%M","%d.%m.%Y","%d.%m"):
        try:
            dt = datetime.strptime(text, fmt)
            if "%Y" not in fmt: dt = dt.replace(year=now.year)
            if "%H" not in fmt: dt = dt.replace(hour=9,minute=0)
            return dt.replace(second=0,microsecond=0,tzinfo=zone).isoformat()
        except: continue
    return None

# ─── Главное меню ────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить задачу", callback_data="menu_add")],
        [InlineKeyboardButton("📋 Все задачи",      callback_data="menu_list"),
         InlineKeyboardButton("🔥 Сегодня",         callback_data="menu_today")],
        [InlineKeyboardButton("📁 Проекты",         callback_data="menu_projects"),
         InlineKeyboardButton("⚙️ Настройки",       callback_data="menu_settings")],
    ])

async def show_main_menu(update, text="Выбери действие:"):
    kb = main_menu_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Task Bot</b> — твой менеджер задач!\n\nВыбери действие:",
        reply_markup=main_menu_keyboard(), parse_mode="HTML"
    )

# ─── Пошаговое добавление задачи ─────────────────────────────────────────────
async def add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Начало — спрашиваем название"""
    query = update.callback_query
    if query: await query.answer()
    ctx.user_data.clear()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
    text = "➕ <b>Новая задача</b>\n\nШаг 1/5 — Как называется задача?"
    if query:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADD_TITLE

async def add_got_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["title"] = update.message.text.strip()
    user_id = update.effective_user.id
    projects = get_projects(user_id)

    buttons = [[InlineKeyboardButton(p, callback_data=f"proj_{p}")] for p in projects]
    buttons.append([InlineKeyboardButton("➕ Новый проект", callback_data="proj_new")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    await update.message.reply_text(
        f"✅ Название: <b>{ctx.user_data['title']}</b>\n\nШаг 2/5 — Выбери проект:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
    )
    return ADD_PROJECT

async def add_got_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "proj_new":
        await query.edit_message_text(
            "Введи название нового проекта:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])
        )
        return ADD_PROJECT_NEW

    ctx.user_data["project"] = data.replace("proj_", "", 1)
    return await ask_deadline(query, ctx)

async def add_got_new_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["project"] = update.message.text.strip()
    return await ask_deadline(update, ctx)

async def ask_deadline(target, ctx):
    buttons = [
        [InlineKeyboardButton("Сегодня",      callback_data="dl_сегодня"),
         InlineKeyboardButton("Завтра",        callback_data="dl_завтра")],
        [InlineKeyboardButton("+3 дня",        callback_data="dl_+3д"),
         InlineKeyboardButton("+7 дней",       callback_data="dl_+7д")],
        [InlineKeyboardButton("Без дедлайна",  callback_data="dl_none")],
        [InlineKeyboardButton("❌ Отмена",      callback_data="cancel")],
    ]
    text = (
        f"✅ Проект: <b>{ctx.user_data['project']}</b>\n\n"
        "Шаг 3/5 — Дедлайн?\n"
        "<i>Или напиши вручную: 25.03, 25.03 18:00, +5д</i>"
    )
    kb = InlineKeyboardMarkup(buttons)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADD_DEADLINE

async def add_got_deadline_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.replace("dl_", "", 1)
    if val == "none":
        ctx.user_data["deadline"] = None
    else:
        ctx.user_data["deadline"] = parse_deadline(val, get_settings(update.effective_user.id)["timezone"])
    return await ask_priority(query, ctx)

async def add_got_deadline_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dl = parse_deadline(update.message.text.strip(), get_settings(update.effective_user.id)["timezone"])
    if not dl:
        await update.message.reply_text("❌ Не понял дату. Попробуй: <code>25.03</code>, <code>25.03 18:00</code>, <code>+5д</code>", parse_mode="HTML")
        return ADD_DEADLINE
    ctx.user_data["deadline"] = dl
    return await ask_priority(update, ctx)

async def ask_priority(target, ctx):
    buttons = [
        [InlineKeyboardButton("🔴 Высокий",  callback_data="pri_high"),
         InlineKeyboardButton("🟡 Обычный",  callback_data="pri_normal"),
         InlineKeyboardButton("🟢 Низкий",   callback_data="pri_low")],
        [InlineKeyboardButton("❌ Отмена",    callback_data="cancel")],
    ]
    dl_str = ""
    if ctx.user_data.get("deadline"):
        dl_str = f"✅ Дедлайн: <b>{datetime.fromisoformat(ctx.user_data['deadline']).strftime('%d.%m.%Y %H:%M')}</b>\n\n"
    else:
        dl_str = "✅ Дедлайн: <b>без дедлайна</b>\n\n"

    text = dl_str + "Шаг 4/5 — Приоритет?"
    kb = InlineKeyboardMarkup(buttons)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADD_PRIORITY

async def add_got_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["priority"] = query.data.replace("pri_", "", 1)

    buttons = [
        [InlineKeyboardButton("🔁 Ежедневно",    callback_data="rep_daily")],
        [InlineKeyboardButton("🔁 Еженедельно",  callback_data="rep_weekly")],
        [InlineKeyboardButton("🔁 Каждые N дней",callback_data="rep_interval")],
        [InlineKeyboardButton("Без повтора",      callback_data="rep_none")],
        [InlineKeyboardButton("❌ Отмена",         callback_data="cancel")],
    ]
    await query.edit_message_text(
        "Шаг 5/5 — Повторяется?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_REPEAT

async def add_got_repeat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    repeat_type, repeat_value = "none", None
    if data == "rep_daily":
        t = ctx.user_data["deadline"]
        time_str = datetime.fromisoformat(t).strftime("%H:%M") if t else "09:00"
        repeat_type, repeat_value = "daily", time_str
    elif data == "rep_weekly":
        t = ctx.user_data["deadline"]
        if t:
            dl = datetime.fromisoformat(t)
            repeat_type = "weekly"
            repeat_value = f"{dl.weekday()}/{dl.strftime('%H:%M')}"
        else:
            repeat_type, repeat_value = "weekly", "0/09:00"
    elif data == "rep_interval":
        repeat_type, repeat_value = "interval", "7/09:00"
    # rep_none → остаётся none

    ctx.user_data["repeat_type"]  = repeat_type
    ctx.user_data["repeat_value"] = repeat_value
    return await save_new_task(query, ctx)

async def save_new_task(target, ctx):
    user_id = target.from_user.id
    d = ctx.user_data
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id,title,project,deadline,priority,repeat_type,repeat_value) VALUES (?,?,?,?,?,?,?)",
            (user_id, d["title"], d["project"], d.get("deadline"), d.get("priority","normal"),
             d.get("repeat_type","none"), d.get("repeat_value"))
        )
        task_id = cur.lastrowid

    dl_str = datetime.fromisoformat(d["deadline"]).strftime("%d.%m.%Y %H:%M") if d.get("deadline") else "без дедлайна"
    rep    = repeat_label(d.get("repeat_type","none"), d.get("repeat_value") or "")
    text = (
        f"✅ <b>Задача #{task_id} добавлена!</b>\n\n"
        f"{PRI_EMOJI.get(d.get('priority','normal'),'🟡')} <b>{d['title']}</b>\n"
        f"📁 {d['project']}   📅 {dl_str}"
    )
    if rep: text += f"\n{rep}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ещё задачу", callback_data="menu_add"),
         InlineKeyboardButton("📋 Мои задачи", callback_data="menu_list")],
        [InlineKeyboardButton("🏠 Меню",       callback_data="menu_main")],
    ])
    await target.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    ctx.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    ctx.user_data.clear()
    await show_main_menu(update, "❌ Отменено.")
    return ConversationHandler.END

# ─── Список задач ─────────────────────────────────────────────────────────────
async def show_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, project_filter=None):
    user_id = update.effective_user.id
    now = user_now(user_id)
    query = update.callback_query

    with get_conn() as conn:
        if project_filter:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND done=0 AND project=?"
                " ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline",
                (user_id, project_filter)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND done=0"
                " ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline",
                (user_id,)
            ).fetchall()

    if not rows:
        text = "📭 Задач нет! Можно отдыхать 🎉"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить", callback_data="menu_add"),
                                    InlineKeyboardButton("🏠 Меню", callback_data="menu_main")]])
        if query:
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)
        return

    projects: dict = {}
    for r in rows:
        projects.setdefault(r["project"], []).append(dict(r))

    parts = [f"📋 <b>Задачи{' / ' + project_filter if project_filter else ''}</b> ({len(rows)})\n"]
    for proj, tasks in projects.items():
        parts.append(f"\n<b>📁 {proj}</b>")
        parts.extend(format_task(t, now) for t in tasks)

    text = "\n".join(parts)

    # Кнопки управления задачами
    buttons = []
    # Кнопки выполнить/удалить для каждой задачи
    task_btns = []
    for r in rows[:5]:  # показываем кнопки для первых 5
        task_btns.append(InlineKeyboardButton(f"✅{r['id']}", callback_data=f"done_{r['id']}"))
        task_btns.append(InlineKeyboardButton(f"🗑{r['id']}", callback_data=f"del_{r['id']}"))
    if task_btns:
        # разбиваем по 4 кнопки в ряд
        for i in range(0, len(task_btns), 4):
            buttons.append(task_btns[i:i+4])
    buttons.append([InlineKeyboardButton("➕ Добавить", callback_data="menu_add"),
                    InlineKeyboardButton("🏠 Меню",     callback_data="menu_main")])
    kb = InlineKeyboardMarkup(buttons)

    if len(text) > 4000:
        text = text[:4000] + "\n…"

    if query:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def show_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query   = update.callback_query
    now     = user_now(user_id)
    start   = now.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    end     = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND done=0 AND deadline BETWEEN ? AND ? ORDER BY deadline",
            (user_id, start, end)
        ).fetchall()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu_main")]])
    if not rows:
        text = "🎉 На сегодня задач нет!"
    else:
        text = f"🔥 <b>Сегодня, {now.strftime('%d.%m')}:</b>\n\n"
        text += "\n\n".join(format_task(dict(r), now) for r in rows)

    if query:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Проекты ─────────────────────────────────────────────────────────────────
async def show_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query   = update.callback_query

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT project, COUNT(*) cnt, SUM(CASE WHEN priority='high' THEN 1 ELSE 0 END) hi"
            " FROM tasks WHERE user_id=? AND done=0 GROUP BY project ORDER BY cnt DESC",
            (user_id,)
        ).fetchall()

    if not rows:
        text = "📂 Проектов нет."
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu_main")]])
        await query.edit_message_text(text, reply_markup=kb)
        return

    buttons = []
    text = "📂 <b>Проекты:</b>\n\n"
    for r in rows:
        hi = f" 🔴×{r['hi']}" if r["hi"] else ""
        text += f"📁 <b>{r['project']}</b> — {r['cnt']} задач{hi}\n"
        buttons.append([
            InlineKeyboardButton(f"📋 {r['project']}", callback_data=f"projlist_{r['project']}"),
            InlineKeyboardButton("🗑 Удалить",          callback_data=f"projdel_{r['project']}"),
        ])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="menu_main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

# ─── Настройки ────────────────────────────────────────────────────────────────
async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query   = update.callback_query
    s = get_settings(user_id)
    r1h  = "✅" if s["remind_1h"]      else "❌"
    rmrn = "✅" if s["remind_morning"] else "❌"
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"🌍 Часовой пояс: <code>{s['timezone']}</code>\n"
        f"⏰ Утренний дайджест: <code>{s['morning_time']}</code>\n"
        f"🔔 За 1 час: {r1h}   🌅 Утром: {rmrn}\n\n"
        f"<i>Для смены TZ: /settz Europe/Moscow\n"
        f"Для смены времени: /setmorning 08:30</i>"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'Выкл' if s['remind_1h'] else 'Вкл'} за 1ч",  callback_data="toggle_1h"),
         InlineKeyboardButton(f"{'Выкл' if s['remind_morning'] else 'Вкл'} утром", callback_data="toggle_morning")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Callback роутер ──────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    if data == "menu_main":
        await show_main_menu(update, "Выбери действие:")
    elif data == "menu_list":
        await show_list(update, ctx)
    elif data == "menu_today":
        await show_today(update, ctx)
    elif data == "menu_projects":
        await show_projects(update, ctx)
    elif data == "menu_settings":
        await show_settings(update, ctx)
    elif data.startswith("done_"):
        task_id = int(data.split("_")[1])
        await do_done(update, ctx, task_id)
    elif data.startswith("del_"):
        task_id = int(data.split("_")[1])
        await do_del(update, ctx, task_id)
    elif data.startswith("projlist_"):
        proj = data.replace("projlist_", "", 1)
        await show_list(update, ctx, project_filter=proj)
    elif data.startswith("projdel_"):
        proj = data.replace("projdel_", "", 1)
        with get_conn() as conn:
            conn.execute("DELETE FROM tasks WHERE project=? AND user_id=?", (proj, user_id))
        await query.answer(f"🗑 Проект «{proj}» удалён со всеми задачами", show_alert=True)
        await show_projects(update, ctx)
    elif data == "toggle_1h":
        s = get_settings(user_id)
        save_settings(user_id, remind_1h=0 if s["remind_1h"] else 1)
        await show_settings(update, ctx)
    elif data == "toggle_morning":
        s = get_settings(user_id)
        save_settings(user_id, remind_morning=0 if s["remind_morning"] else 1)
        await show_settings(update, ctx)

async def do_done(update, ctx, task_id):
    user_id = update.callback_query.from_user.id
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
        if not row:
            await update.callback_query.answer("Задача не найдена", show_alert=True)
            return
        row = dict(row)
        if row["repeat_type"] != "none" and row["deadline"]:
            next_dl = _next_deadline(row)
            if next_dl:
                conn.execute(
                    "INSERT INTO tasks (user_id,title,project,deadline,priority,repeat_type,repeat_value) VALUES (?,?,?,?,?,?,?)",
                    (user_id, row["title"], row["project"], next_dl, row["priority"], row["repeat_type"], row["repeat_value"])
                )
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    await update.callback_query.answer(f"✅ Задача #{task_id} выполнена!", show_alert=False)
    await show_list(update, ctx)

async def do_del(update, ctx, task_id):
    user_id = update.callback_query.from_user.id
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id))
    await update.callback_query.answer(f"🗑 Задача #{task_id} удалена", show_alert=False)
    await show_list(update, ctx)

def _next_deadline(row):
    try:
        cur    = datetime.fromisoformat(row["deadline"])
        rtype  = row["repeat_type"]
        rvalue = row["repeat_value"] or ""
        if rtype == "daily":
            h,m = map(int, rvalue.split(":"))
            return (cur + timedelta(days=1)).replace(hour=h,minute=m,second=0).isoformat()
        if rtype == "weekly":
            wd,ts = rvalue.split("/"); h,m = map(int,ts.split(":"))
            delta = (int(wd) - cur.weekday() + 7) % 7 or 7
            return (cur + timedelta(days=delta)).replace(hour=h,minute=m,second=0).isoformat()
        if rtype == "interval":
            n,ts = rvalue.split("/"); h,m = map(int,ts.split(":"))
            return (cur + timedelta(days=int(n))).replace(hour=h,minute=m,second=0).isoformat()
    except Exception as e:
        log.warning(f"_next_deadline: {e}")
    return None

# ─── Команды ──────────────────────────────────────────────────────────────────
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_list(update, ctx)

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_today(update, ctx)

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Укажи номер: /done 3")
        return
    task_id = int(ctx.args[0])
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
        if not row:
            await update.message.reply_text(f"Задача #{task_id} не найдена.")
            return
        row = dict(row)
        if row["repeat_type"] != "none" and row["deadline"]:
            next_dl = _next_deadline(row)
            if next_dl:
                conn.execute(
                    "INSERT INTO tasks (user_id,title,project,deadline,priority,repeat_type,repeat_value) VALUES (?,?,?,?,?,?,?)",
                    (user_id,row["title"],row["project"],next_dl,row["priority"],row["repeat_type"],row["repeat_value"])
                )
        conn.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    await update.message.reply_text(f"✅ Задача #{task_id} выполнена! 🎉")

async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Укажи номер: /del 3")
        return
    task_id = ctx.args[0]
    with get_conn() as conn:
        affected = conn.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)).rowcount
    if affected:
        await update.message.reply_text(f"🗑 Задача #{task_id} удалена.")
    else:
        await update.message.reply_text(f"Задача #{task_id} не найдена.")

async def cmd_settz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Формат: /settz Europe/Moscow")
        return
    tz = ctx.args[0]
    try: ZoneInfo(tz)
    except:
        await update.message.reply_text(f"❌ Неверный TZ: {tz}")
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

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Любой текст вне диалога — показываем меню"""
    await show_main_menu(update, "Выбери действие:")

# ─── Напоминания ──────────────────────────────────────────────────────────────
async def job_reminders(app):
    with get_conn() as conn:
        tasks = conn.execute(
            "SELECT t.*, s.timezone, s.remind_1h, s.remind_morning, s.morning_time"
            " FROM tasks t LEFT JOIN user_settings s ON t.user_id=s.user_id WHERE t.done=0"
        ).fetchall()

    for raw in tasks:
        row = dict(raw)
        tz_str = row.get("timezone") or DEFAULT_TZ
        try: tz = ZoneInfo(tz_str)
        except: tz = ZoneInfo(DEFAULT_TZ)
        now_local = datetime.now(tz)
        today_str = now_local.strftime("%Y-%m-%d")

        if row["deadline"]:
            dl = datetime.fromisoformat(row["deadline"])
            if dl.tzinfo is None: dl = dl.replace(tzinfo=tz)

            if row.get("remind_1h", 1):
                diff = (dl - now_local).total_seconds()
                if 3000 <= diff <= 3900:
                    await _send_if_not_sent(app, row, "1h", today_str,
                        f"🔥 До дедлайна <b>1 час!</b>\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}")

            if row.get("remind_morning", 1) and dl.date() == now_local.date():
                mt = row.get("morning_time") or DEFAULT_MORNING
                mh,mm = map(int, mt.split(":"))
                morning = now_local.replace(hour=mh,minute=mm,second=0,microsecond=0)
                if now_local >= morning:
                    await _send_if_not_sent(app, row, "morning", today_str,
                        f"🌅 Сегодня дедлайн!\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}\n🕐 до {dl.strftime('%H:%M')}")

        if row.get("remind_custom"):
            try:
                rc = datetime.fromisoformat(row["remind_custom"])
                if rc.tzinfo is None: rc = rc.replace(tzinfo=tz)
                if abs((rc - now_local).total_seconds()) <= 300:
                    sent = await _send_if_not_sent(app, row, "custom", today_str,
                        f"🔔 Напоминание!\n📌 <b>[#{row['id']}] {row['title']}</b>\n📁 {row['project']}")
                    if sent:
                        with get_conn() as conn:
                            conn.execute("UPDATE tasks SET remind_custom=NULL WHERE id=?", (row["id"],))
            except Exception as e:
                log.warning(f"custom remind: {e}")

async def _send_if_not_sent(app, row, rtype, today_str, text):
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM sent_reminders WHERE task_id=? AND remind_type=? AND sent_date=?",
                        (row["id"], rtype, today_str)).fetchone():
            return False
    try:
        await app.bot.send_message(row["user_id"], text, parse_mode="HTML")
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO sent_reminders VALUES (?,?,?)", (row["id"],rtype,today_str))
        return True
    except Exception as e:
        log.warning(f"send_message failed: {e}")
        return False

# ─── Запуск ───────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для пошагового добавления
    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_start, pattern="^menu_add$"),
            CommandHandler("add", add_start),
        ],
        states={
            ADD_TITLE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_title)],
            ADD_PROJECT:     [CallbackQueryHandler(add_got_project, pattern="^proj_")],
            ADD_PROJECT_NEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_new_project)],
            ADD_DEADLINE:    [
                CallbackQueryHandler(add_got_deadline_btn, pattern="^dl_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_deadline_text),
            ],
            ADD_PRIORITY:    [CallbackQueryHandler(add_got_priority, pattern="^pri_")],
            ADD_REPEAT:      [CallbackQueryHandler(add_got_repeat,   pattern="^rep_")],
        },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern="^cancel$")],
        per_message=False,
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("list",       cmd_list))
    app.add_handler(CommandHandler("today",      cmd_today))
    app.add_handler(CommandHandler("done",       cmd_done))
    app.add_handler(CommandHandler("del",        cmd_del))
    app.add_handler(CommandHandler("settz",      cmd_settz))
    app.add_handler(CommandHandler("setmorning", cmd_setmorning))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_reminders, "interval", minutes=5, args=[app])
    scheduler.start()

    log.info("🤖 Task Bot v3 запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
    
