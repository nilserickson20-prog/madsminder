import os, random, signal, datetime as dt, asyncio
from pathlib import Path

import aiosqlite
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

# -------------------- env helpers --------------------
def getenv_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v not in (None, "", "null", "None") else default
    except Exception:
        return default

def getenv_int_or_none(name: str):
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return int(v)
    except Exception:
        return None

# -------------------- configuration --------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set on the Fly app (Secrets). Paste the raw token string (no 'Bot ').")

DB_PATH = os.getenv("DB_PATH", "/data/madsminder.db")
TZ = os.getenv("TZ", "America/New_York")
ANNOUNCE_CHANNEL_ID = getenv_int("ANNOUNCE_CHANNEL_ID", 0)   # optional
THREAT_GRACE_MINUTES = getenv_int("THREAT_GRACE_MINUTES", 360)   # start nudging after 6h
THREAT_COOLDOWN_MINUTES = getenv_int("THREAT_COOLDOWN_MINUTES", 180)  # 3h between nudges
GUILD_ID = getenv_int_or_none("GUILD_ID")  # optional for instant slash commands

CELEBRATE_DIR = os.getenv("CELEBRATE_DIR", "/app/celebrate_images")  # folder with images in the container
CELEBRATE_THRESHOLD = getenv_int("CELEBRATE_THRESHOLD", 6)           # celebrate at 6th completed task

print(f"[startup] TZ={TZ} ANNOUNCE_CHANNEL_ID={ANNOUNCE_CHANNEL_ID} "
      f"GRACE={THREAT_GRACE_MINUTES} COOLDOWN={THREAT_COOLDOWN_MINUTES} "
      f"GUILD_ID={GUILD_ID or 'None'} CELEBRATE_DIR={CELEBRATE_DIR} THRESH={CELEBRATE_THRESHOLD}")

# -------------------- discord client --------------------
INTENTS = discord.Intents.default()
INTENTS.reactions = True     # we only need reaction events, not message content
bot = commands.Bot(command_prefix="!", intents=INTENTS)

# graceful shutdown logs (so we can see if Fly sends a stop)
def _handle_signal(sig, frame):
    print(f"[signal] received {sig}, shutting down gracefully")
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# -------------------- phrases --------------------
LINES = {
    "task_tick": [
        "One down. Understated excellence.", "Neat work. Don’t let it go to your head.",
        "Progress suits you.", "A clean strike. The kind that scares paperwork.",
        "Good. Now keep moving.", "Tidy work. It almost looks easy.",
        "A win, however small, is still a win.", "Nicely done.",
        "A quiet victory. The best kind.", "Well struck.",
        "That’s one fewer reason to frown.", "Steady hands. Keep them that way.",
        "Satisfying, isn’t it?", "A dent in the day. Well placed.",
        "That’s how it’s done. Without fuss.", "Clean and quiet—just how I like it.",
        "Another mark in your favour.", "One more step in the right direction.",
        "Done without drama. Excellent.", "If only all victories were this tidy.",
    ],
    # Hannibal-flavoured (ominous, non-violent)
    "threat": [
        "You’ve left something undone. It watches. I do as well.",
        "I could tidy this for you. My methods are… exacting.",
        "Every loose end wants trimming. I’m quite good with edges.",
        "The list is incomplete. Loose ends unsettle me.",
        "Do you know what happens to half-cooked things? They spoil.",
        "Neglect invites predators. Do not invite them.",
        "The day is bleeding time. You could stop it.",
        "Incompletion is… distasteful. Finish the final course.",
        "The task is waiting. It prefers not to wait long.",
        "Leaving work unfinished is uncivilised. Correct it.",
        "Unfinished business has a scent. Yours is noticeable.",
        "You’re so close I can almost taste the finish.",
        "It still breathes. Silence it cleanly.",
        "A symphony without its final note is an irritation. Resolve it.",
        "There is elegance in completion—and consequences in its absence.",
        "You could end this now. That would be wisest.",
        "I’m patient. Hunger rarely is.",
        "One final bite, and the day is yours.",
        "You left the table before the last course. Rude.",
        "Do finish. It’s far more pleasant than being finished with.",
    ],
    "daily_prompt": [
        "Morning. Add your tasks one by one with `/addtask`. Keep them sharp.",
        "Today likes precision. Use `/addtask` to begin.",
        "Select your priorities. One task at a time—quality over clutter.",
        "Plan it now, or the day will plan itself for you. `/addtask`.",
        "Three good cuts beat twelve dull ones. Start with `/addtask`.",
        "If everything matters, nothing does. Declare your first task.",
        "Your intentions, please. `/addtask` is listening.",
        "Begin elegantly. One clear task will do.",
        "Discipline starts with a single line. Add it.",
        "A short list makes for a long life. `/addtask`.",
        "Pick your battles—preferably ones you can win.",
        "The canvas is blank. Limit your brushstrokes.",
        "Write it before it escapes you.",
        "We’re aiming for precision, not clutter. Proceed.",
        "Ambiguity is laziness in disguise. Name the work.",
        "A tidy list is a courteous future.",
        "Make the first move. `/addtask`.",
        "Quiet intentions make loud results.",
        "Tasteful goals only, please.",
        "Let’s keep today civilised.",
    ],
}
def pick(seq): return random.choice(seq)

# -------------------- db helpers --------------------
async def get_db():
    conn = await aiosqlite.connect(DB_PATH)

    # base tasks table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            task_date TEXT,
            task_text TEXT,
            done INTEGER DEFAULT 0,
            message_id TEXT,
            channel_id TEXT,
            created_at TEXT,
            last_threat_at TEXT
        )
    """)
    # migrations: due fields
    cols = {row[1] for row in await (await conn.execute("PRAGMA table_info(tasks)")).fetchall()}
    if "due_type" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN due_type TEXT")
    if "due_at" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
    await conn.commit()

    # reminders
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            channel_id TEXT,
            text TEXT,
            remind_at TEXT,      -- UTC ISO
            created_at TEXT,
            sent INTEGER DEFAULT 0
        )
    """)
    await conn.commit()

    # celebrations: once per user/day
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS celebrations(
            user_id TEXT,
            task_date TEXT,
            sent INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, task_date)
        )
    """)
    await conn.commit()

    return conn

def now_utc():
    return dt.datetime.now(dt.timezone.utc)

def parse_iso(s: str | None):
    if not s: return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

def today_iso():
    return dt.date.today().isoformat()

def to_utc(dt_local: dt.datetime) -> dt.datetime:
    return dt_local.astimezone(dt.timezone.utc)

def end_of_day_utc(date_obj: dt.date, tz_str: str) -> dt.datetime:
    tz = ZoneInfo(tz_str)
    local_eod = dt.datetime(
        year=date_obj.year, month=date_obj.month, day=date_obj.day,
        hour=23, minute=59, second=59, tzinfo=tz
    )
    return to_utc(local_eod)

def pick_celebration_image() -> Path | None:
    p = Path(CELEBRATE_DIR)
    if not p.exists():
        return None
    files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}]
    return random.choice(files) if files else None

# -------------------- command registration --------------------
@bot.event
async def setup_hook():
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"[commands] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"[commands] Synced {len(synced)} global commands (may take ~1h to appear)")
    except Exception as e:
        import traceback
        print("[commands] Sync error; attempting global fallback:", repr(e))
        traceback.print_exc()
        try:
            synced = await bot.tree.sync()
            print(f"[commands] Fallback global sync OK ({len(synced)} commands)")
        except Exception as e2:
            print("[commands] Global sync also failed:", repr(e2))

# -------------------- lifecycle --------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    tz = ZoneInfo(TZ)
    scheduler = AsyncIOScheduler()
    if ANNOUNCE_CHANNEL_ID:
        scheduler.add_job(daily_prompt, CronTrigger(hour=9, minute=0, timezone=tz))
    scheduler.add_job(threat_scan,   IntervalTrigger(minutes=10, timezone=tz))
    scheduler.add_job(reminder_scan, IntervalTrigger(minutes=1,  timezone=tz))
    scheduler.start()

# -------------------- slash commands --------------------
@bot.tree.command(name="help", description="Show MadsMinder commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**MadsMinder — Commands**\n"
        "• `/addtask text:<task>` — Add a task for today (react ✅ on that message when done)\n"
        "• `/taskby days:<N> text:<task>` — Task due within N days (nudges start after N days)\n"
        "• `/taskon date:<YYYY-MM-DD> text:<task>` — Task due by end of that date (nudges start after it)\n"
        "• `/remindme hours:<N> text:<note>` — DM reminder after N hours\n"
        "• `/mytasks` — Show your tasks for today\n"
        "\nElegance over enthusiasm."
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="addtask", description="Add a single task (react with ✅ when done)")
async def addtask(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name} ({today_iso()})**\n• {text}\n\n"
    )
    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at, last_threat_at, due_type, due_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL)
    """, (
        str(interaction.user.id),
        today_iso(),
        text,
        str(task_msg.id),
        str(task_msg.channel.id),
        now_utc().isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Noted. I’ll be… observing.", ephemeral=True)

@bot.tree.command(name="taskby", description="Add a task due within N days (threats begin after that window)")
async def taskby(interaction: discord.Interaction, days: int, text: str):
    if days <= 0 or days > 365:
        await interaction.response.send_message("Days must be between 1 and 365.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name}** — due within {days} day(s)\n• {text}\n\n"
    )
    tz = ZoneInfo(TZ)
    now_local = dt.datetime.now(tz)
    due_date_local = (now_local + dt.timedelta(days=days)).date()
    due_at_utc = end_of_day_utc(due_date_local, TZ)

    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at, last_threat_at, due_type, due_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, 'by_days', ?)
    """, (
        str(interaction.user.id),
        today_iso(),
        text,
        str(task_msg.id),
        str(task_msg.channel.id),
        now_utc().isoformat(),
        due_at_utc.isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Registered. I’ll be… patient. For now.", ephemeral=True)

@bot.tree.command(name="taskon", description="Add a task due by the end of a specific date (YYYY-MM-DD)")
async def taskon(interaction: discord.Interaction, date: str, text: str):
    try:
        due_date = dt.date.fromisoformat(date)
    except Exception:
        await interaction.response.send_message("Use a date like 2025-08-30 (YYYY-MM-DD).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name}** — due by end of {date}\n• {text}\n\n"
    )
    due_at_utc = end_of_day_utc(due_date, TZ)

    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at, last_threat_at, due_type, due_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, 'on_date', ?)
    """, (
        str(interaction.user.id),
        today_iso(),
        text,
        str(task_msg.id),
        str(task_msg.channel.id),
        now_utc().isoformat(),
        due_at_utc.isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Understood. The deadline is… absolute.", ephemeral=True)

@bot.tree.command(name="remindme", description="DM me a reminder after N hours")
async def remindme(interaction: discord.Interaction, hours: int, text: str):
    if hours <= 0 or hours > 24*14:
        await interaction.response.send_message("Choose a number of hours between 1 and 336.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    now = now_utc()
    remind_at = now + dt.timedelta(hours=hours)
    conn = await get_db()
    await conn.execute("""
        INSERT INTO reminders(user_id, channel_id, text, remind_at, created_at, sent)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (str(interaction.user.id), str(interaction.channel_id), text, remind_at.isoformat(), now.isoformat()))
    await conn.commit(); await conn.close()
    await interaction.followup.send(f"Noted. I’ll whisper in {hours} hour(s).", ephemeral=True)

@bot.tree.command(name="mytasks", description="View all of your tasks for today")
async def mytasks(interaction: discord.Interaction):
    conn = await get_db()
    cur = await conn.execute("""
        SELECT task_text, done FROM tasks
        WHERE user_id=? AND task_date=?
        ORDER BY id ASC
    """, (str(interaction.user.id), today_iso()))
    rows = await cur.fetchall()
    await cur.close(); await conn.close()

    if not rows:
        await interaction.response.send_message("You’ve added no tasks today. Try `/addtask`.", ephemeral=True)
        return

    lines = []
    for i, (text, done) in enumerate(rows, start=1):
        lines.append(f"{'✅' if done else '⬜️'} {i}) {text}")
    await interaction.response.send_message("**Your tasks for today**\n" + "\n".join(lines), ephemeral=True)

# -------------------- reactions --------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅":
        return
    conn = await get_db()
    cur = await conn.execute(
        "SELECT user_id, done FROM tasks WHERE message_id=? AND task_date=?",
        (str(payload.message_id), today_iso())
    )
    row = await cur.fetchone()
    await cur.close()
    if not row:
        await conn.close(); return
    user_id, done = row
    if str(payload.user_id) != user_id:
        await conn.close(); return  # only the task owner can tick their task

    if not done:
        await conn.execute("UPDATE tasks SET done=1 WHERE message_id=?", (str(payload.message_id),))
        await conn.commit()
    await conn.close()

    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    user = await bot.fetch_user(payload.user_id)
    await channel.send(f"{user.mention} {pick(LINES['task_tick'])}")

    # celebration check after mark done
    conn2 = await get_db()
    cur2 = await conn2.execute("""
        SELECT COUNT(*) FROM tasks WHERE user_id=? AND task_date=? AND done=1
    """, (str(payload.user_id), today_iso()))
    (done_count,) = await cur2.fetchone()
    await cur2.close()

    cur3 = await conn2.execute("""
        SELECT sent FROM celebrations WHERE user_id=? AND task_date=?
    """, (str(payload.user_id), today_iso()))
    row3 = await cur3.fetchone()
    await cur3.close()

    if done_count >= CELEBRATE_THRESHOLD and (not row3 or row3[0] == 0):
        img = pick_celebration_image()
        try:
            if img:
                await channel.send(
                    content=f"{user.mention} A spree of competence. Accept this… memento.",
                    file=discord.File(img)
                )
            else:
                await channel.send(f"{user.mention} A spree of competence. Imagine confetti.")
        except Exception:
            pass
        await conn2.execute("""
            INSERT INTO celebrations(user_id, task_date, sent) VALUES(?, ?, 1)
            ON CONFLICT(user_id, task_date) DO UPDATE SET sent=1
        """, (str(payload.user_id), today_iso()))
        await conn2.commit()
    await conn2.close()

# -------------------- jobs --------------------
async def daily_prompt():
    if not ANNOUNCE_CHANNEL_ID:
        return
    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
    await channel.send(f"{pick(LINES['daily_prompt'])}\nUse `/addtask` to register a task.")

async def threat_scan():
    """
    Every 10 minutes:
      - For each task not done,
      - If it has a due_at: threats start only after due_at,
        else: threats start after THREAT_GRACE_MINUTES since creation,
      - and not threatened within THREAT_COOLDOWN_MINUTES,
    reply to the original task message with an ominous nudge.
    """
    conn = await get_db()
    cur = await conn.execute("""
        SELECT id, user_id, message_id, channel_id, created_at, last_threat_at, due_type, due_at
        FROM tasks
        WHERE done=0
    """)
    rows = await cur.fetchall()
    await cur.close()

    now = now_utc()
    for (tid, user_id, message_id, channel_id, created_at, last_threat_at, due_type, due_at) in rows:
        created_dt = parse_iso(created_at)
        last_dt = parse_iso(last_threat_at)
        due_dt = parse_iso(due_at)
        if not created_dt:
            continue

        ready = False
        if due_dt is not None:
            ready = now >= due_dt   # wait until due moment has passed
        else:
            age_min = (now - created_dt).total_seconds() / 60
            ready = age_min >= THREAT_GRACE_MINUTES

        cooldown_ok = (last_dt is None) or ((now - last_dt).total_seconds() / 60 >= THREAT_COOLDOWN_MINUTES)

        if ready and cooldown_ok:
            try:
                channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                msg = await channel.fetch_message(int(message_id))
                await msg.reply(pick(LINES["threat"]))
                await conn.execute("UPDATE tasks SET last_threat_at=? WHERE id=?", (now.isoformat(), tid))
                await conn.commit()
            except Exception:
                await conn.execute("UPDATE tasks SET last_threat_at=? WHERE id=?", (now.isoformat(), tid))
                await conn.commit()

    await conn.close()

async def reminder_scan():
    """
    Every minute: DM users for reminders that are due.
    Falls back to posting in the original channel if DM fails.
    """
    conn = await get_db()
    cur = await conn.execute("""
        SELECT id, user_id, channel_id, text, remind_at FROM reminders
        WHERE sent=0
    """)
    rows = await cur.fetchall()
    await cur.close()

    now = now_utc()
    for (rid, user_id, channel_id, text, remind_at) in rows:
        due = parse_iso(remind_at)
        if not due or due > now:
            continue
        try:
            user = await bot.fetch_user(int(user_id))
            try:
                await user.send(f"Reminder: {text}")
            except Exception:
                try:
                    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                    await channel.send(f"<@{user_id}> Reminder: {text}")
                except Exception:
                    pass
        finally:
            await conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
            await conn.commit()

    await conn.close()

# -------------------- entrypoint (blocks forever) --------------------
if __name__ == "__main__":
    print("[startup] starting discord client…")
    # bot.run blocks and manages the event loop & reconnect logic
    bot.run(TOKEN, log_handler=None)
