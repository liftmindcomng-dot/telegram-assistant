import os, re, json, time, sqlite3, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ["TELEGRAM_TOKEN"]
TZ = ZoneInfo(os.getenv("TZ", "UTC"))
MODEL = "claude-sonnet-4-5"
claude = anthropic.Anthropic()
db = sqlite3.connect(os.getenv("DB_PATH", "assistant.db"), check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, chat INT, text TEXT);
CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY, chat INT, text TEXT, done INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, chat INT, text TEXT, due REAL, repeat TEXT);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, chat INT, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS settings(chat INT PRIMARY KEY, morning TEXT);
""")
db.commit()

SYSTEM = f"""You are a warm, concise personal assistant chatting with the user on Telegram.
Current local time: {{now}} ({os.getenv("TZ", "UTC")}).
You can take actions by replying with a JSON block wrapped in <action></action> tags. Supported actions:
- {{"type":"remind","text":"...","due":"ISO datetime local","repeat":"none|daily|weekly|monthly"}}
- {{"type":"delete_reminder","id":N}}
- {{"type":"note","text":"..."}}
- {{"type":"todo","text":"..."}}
- {{"type":"done","id":N}}
- {{"type":"set_morning","time":"HH:MM"}}
Include an action only when the user clearly wants one. You may include several. Always also write a natural reply for the user outside the tags.
Current reminders: {{reminders}}
Open to-dos: {{todos}}
"""

def now(): return datetime.now(TZ)

def context_block(chat):
    rs = db.execute("SELECT id,text,due,repeat FROM reminders WHERE chat=? ORDER BY due", (chat,)).fetchall()
    ts = db.execute("SELECT id,text FROM todos WHERE chat=? AND done=0", (chat,)).fetchall()
    r = "; ".join(f"#{i} '{t}' at {datetime.fromtimestamp(d, TZ):%Y-%m-%d %H:%M} ({rep})" for i, t, d, rep in rs) or "none"
    td = "; ".join(f"#{i} {t}" for i, t in ts) or "none"
    return r, td

def next_due(due, repeat):
    dt = datetime.fromtimestamp(due, TZ)
    if repeat == "daily": dt += timedelta(days=1)
    elif repeat == "weekly": dt += timedelta(weeks=1)
    elif repeat == "monthly":
        m = dt.month % 12 + 1
        y = dt.year + (dt.month == 12)
        dt = dt.replace(year=y, month=m)
    return dt.timestamp()

async def fire(ctx: ContextTypes.DEFAULT_TYPE):
    rid, chat, text, repeat = ctx.job.data
    await ctx.bot.send_message(chat, f"⏰ {text}")
    if repeat and repeat != "none":
        nd = next_due(db.execute("SELECT due FROM reminders WHERE id=?", (rid,)).fetchone()[0], repeat)
        db.execute("UPDATE reminders SET due=? WHERE id=?", (nd, rid))
        db.commit()
        schedule(ctx.application, rid, chat, text, nd, repeat)
    else:
        db.execute("DELETE FROM reminders WHERE id=?", (rid,)); db.commit()

def schedule(app, rid, chat, text, due, repeat):
    app.job_queue.run_once(fire, max(due - time.time(), 1), data=(rid, chat, text, repeat), name=f"r{rid}")

def unschedule(app, rid):
    for j in app.job_queue.get_jobs_by_name(f"r{rid}"):
        j.schedule_removal()

async def send_morning(ctx: ContextTypes.DEFAULT_TYPE):
    chat = ctx.job.data
    r, td = context_block(chat)
    msg = claude.messages.create(model=MODEL, max_tokens=400,
        system="Write a short, friendly good-morning briefing for the user. Plain text, no lists longer than 5 items.",
        messages=[{"role": "user", "content": f"Today is {now():%A %d %B}. Reminders: {r}. Open to-dos: {td}."}])
    await ctx.bot.send_message(chat, msg.content[0].text)

def schedule_morning(app, chat, hhmm):
    for j in app.job_queue.get_jobs_by_name(f"m{chat}"):
        j.schedule_removal()
    h, m = map(int, hhmm.split(":"))
    app.job_queue.run_daily(send_morning, time=datetime.strptime(f"{h}:{m}", "%H:%M").time().replace(tzinfo=TZ),
                            data=chat, name=f"m{chat}")

ACTION_RE = re.compile(r"<action>(.*?)</action>", re.S)

async def handle_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat = u.effective_chat.id
    text = u.message.text
    db.execute("INSERT INTO history(chat,role,content) VALUES(?,?,?)", (chat, "user", text))
    db.commit()
    hist = db.execute("SELECT role,content FROM history WHERE chat=? ORDER BY id DESC LIMIT 20", (chat,)).fetchall()[::-1]
    r, td = context_block(chat)
    system = SYSTEM.replace("{now}", now().strftime("%A %Y-%m-%d %H:%M")).replace("{reminders}", r).replace("{todos}", td)

    def call():
        return claude.messages.create(model=MODEL, max_tokens=800, system=system,
                                      messages=[{"role": a, "content": b} for a, b in hist])
    try:
        resp = await asyncio.to_thread(call)
        reply = resp.content[0].text
    except Exception as e:
        return await u.message.reply_text(f"Sorry, I hit an error: {e}")

    for raw in ACTION_RE.findall(reply):
        try:
            await run_action(c.application, chat, json.loads(raw))
        except Exception as e:
            reply += f"\n(Couldn't do one action: {e})"
    clean = ACTION_RE.sub("", reply).strip() or "Done."
    db.execute("INSERT INTO history(chat,role,content) VALUES(?,?,?)", (chat, "assistant", clean))
    db.commit()
    await u.message.reply_text(clean)

async def run_action(app, chat, a):
    t = a.get("type")
    if t == "remind":
        dt = datetime.fromisoformat(a["due"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        due = dt.timestamp()
        rep = a.get("repeat", "none")
        cur = db.execute("INSERT INTO reminders(chat,text,due,repeat) VALUES(?,?,?,?)", (chat, a["text"], due, rep))
        db.commit()
        schedule(app, cur.lastrowid, chat, a["text"], due, rep)
    elif t == "delete_reminder":
        db.execute("DELETE FROM reminders WHERE id=? AND chat=?", (a["id"], chat)); db.commit()
        unschedule(app, a["id"])
    elif t == "note":
        db.execute("INSERT INTO notes(chat,text) VALUES(?,?)", (chat, a["text"])); db.commit()
    elif t == "todo":
        db.execute("INSERT INTO todos(chat,text) VALUES(?,?)", (chat, a["text"])); db.commit()
    elif t == "done":
        db.execute("UPDATE todos SET done=1 WHERE id=? AND chat=?", (a["id"], chat)); db.commit()
    elif t == "set_morning":
        db.execute("INSERT OR REPLACE INTO settings(chat,morning) VALUES(?,?)", (chat, a["time"])); db.commit()
        schedule_morning(app, chat, a["time"])

async def cmd_morning(u: Update, c):
    if not c.args:
        return await u.message.reply_text("Usage: /morning HH:MM (e.g. /morning 07:30)")
    await run_action(c.application, u.effective_chat.id, {"type": "set_morning", "time": c.args[0]})
    await u.message.reply_text(f"Morning briefing set for {c.args[0]}.")

async def cmd_start(u: Update, c):
    await u.message.reply_text("Hey! Just talk to me normally. I can set reminders, keep notes and to-dos, "
                               "and send a morning briefing. Use /morning 07:30 to set one up.")

async def post_init(app):
    for rid, chat, text, due, rep in db.execute("SELECT id,chat,text,due,repeat FROM reminders"):
        schedule(app, rid, chat, text, due, rep)
    for chat, hhmm in db.execute("SELECT chat,morning FROM settings"):
        schedule_morning(app, chat, hhmm)

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()
