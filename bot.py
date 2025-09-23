import os, random, signal, datetime as dt
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo
from openai import OpenAI

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

THREAT_GRACE_MINUTES      = getenv_int("THREAT_GRACE_MINUTES", 360)    # 6h before nudges
THREAT_COOLDOWN_MINUTES   = getenv_int("THREAT_COOLDOWN_MINUTES", 180) # 3h between nudges
MAX_THREATS_PER_TASK      = getenv_int("MAX_THREATS_PER_TASK", 5)
GUILD_ID                  = getenv_int_or_none("GUILD_ID")

CELEBRATE_DIR             = os.getenv("CELEBRATE_DIR", "/app/celebrate_images")
CELEBRATE_THRESHOLD       = getenv_int("CELEBRATE_THRESHOLD", 6)  # celebrate at 6 tasks done in a day

PEPTALKS_DIR              = os.getenv("PEPTALKS_DIR", "/app/peptalks")  # .mp3 pep talks
STREAK_VIDEOS_DIR         = os.getenv("STREAK_VIDEOS_DIR", "/app/streak_videos")  # .mp4/.mov/.webm

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


print(
    f"[startup] TZ={TZ} ANNOUNCE_CHANNEL_ID={ANNOUNCE_CHANNEL_ID} "
    f"GRACE={THREAT_GRACE_MINUTES} COOLDOWN={THREAT_COOLDOWN_MINUTES} "
    f"GUILD_ID={GUILD_ID or 'None'} CELEBRATE_DIR={CELEBRATE_DIR} "
    f"THRESH={CELEBRATE_THRESHOLD} MAX_THREATS={MAX_THREATS_PER_TASK} "
    f"PEPTALKS_DIR={PEPTALKS_DIR} STREAK_VIDEOS_DIR={STREAK_VIDEOS_DIR}"
)

# -------------------- discord client --------------------
INTENTS = discord.Intents.default()
INTENTS.reactions = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)

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
    # ominous, non-violent
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
    # random celebratory line when posting the 6+ image
    "celebrate": [
        "Order restored. Enjoy the spoils.", "Six clean cuts. I’m impressed—quietly.",
        "Discipline looks good on you.", "Efficiency is a refined taste. You have it.",
        "You carved today to your liking. Elegant.", "The day yielded. You insisted.",
        "Consider me… satisfied.", "That was precise. I notice precision.",
        "You’ve earned something beautiful.", "I prefer excellence. Thank you for providing it.",
        "A day without loose ends. Civilised.", "The list looks empty. How charming.",
        "A perfect course—start to finish.", "I appreciate a thorough appetite.",
        "Graceful, relentless, effective.", "No fuss. Just results.",
        "If only everyone were so… capable.", "You didn’t hesitate. Nor should you.",
        "A fine performance. Take your bow.", "Meticulous. I approve.",
    ],
    # streak sayings: when streak > 0
    "streak_keep": [
        "Routine is an art when practiced daily.",
        "Elegance is repetition without boredom.",
        "Discipline is appetite refined. Keep feeding it.",
        "Consistency is the quietest form of power.",
        "You’re cultivating taste. One day at a time.",
        "A day unbroken. Another line in a clean pattern.",
        "The best habits are invisible—like good tailoring.",
        "Continue. Grace thrives on rhythm.",
        "Civilisation is built by small, daily choices.",
        "You arrive, and the day behaves.",
        "Keep your promises to yourself. I’m watching.",
        "Your restraint is… reassuring.",
        "Patterns become identity. Choose carefully.",
        "Today does not intimidate you. Good.",
        "You are not at the mercy of mood. Impressive.",
        "Precision loves routine; so do I.",
        "Nothing extravagant—just excellent, again.",
        "Momentum is a delicate broth. Don’t spill it.",
        "You’ve acquired a taste for progress.",
        "Let’s keep things exquisitely predictable.",
    ],
    # streak sayings: when streak == 0 (reset)
    "streak_reset": [
        "A lapse. Untidy. Let’s not make a habit of it.",
        "You missed a step. I prefer symmetry.",
        "The pattern broke. Reassemble yourself.",
        "Neutral taste is forgettable. You are not.",
        "Start again. Quietly. Thoroughly.",
        "The day escaped you. Don’t let the next one.",
        "Elegance faltered. Discipline did not. Retrieve it.",
        "Precision is patient. Try again.",
        "The absence was noticed. Correct the record.",
        "We can forgive one day. Let’s not collect them.",
        "The table is reset. Serve something better.",
        "Stumbles are acceptable. Idle is not.",
        "Momentum decays quickly. Reignite it.",
        "The mirror prefers you focused.",
        "Consider the cost of neglect. Then proceed.",
        "You will be remembered for what you repeat. Choose anew.",
        "Yesterday underwhelmed. Today needn’t.",
        "You can do better. In fact, you will.",
        "Let’s restore the formality of progress.",
        "Begin again—without apology, with intent.",
    ],
}
def pick(seq): return random.choice(seq)

# -------------------- file pickers --------------------
def _list_files(dirpath: Path, exts: set[str]) -> list[Path]:
    if not dirpath.exists():
        return []
    out = []
    for p in dirpath.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            out.append(p)
    return out

def pick_celebration_image() -> Path | None:
    files = []
    for loc in (Path(CELEBRATE_DIR), Path("/app/celebrate_images"), Path("./celebrate_images")):
        files.extend(_list_files(loc, {".png", ".jpg", ".jpeg", ".gif"}))
    return random.choice(files) if files else None

def pick_peptalk_mp3() -> Path | None:
    files = []
    for loc in (Path(PEPTALKS_DIR), Path("/app/peptalks"), Path("./peptalks"), Path("/peptalks")):
        files.extend(_list_files(loc, {".mp3"}))
    return random.choice(files) if files else None

def pick_streak_video() -> Path | None:
    files = []
    for loc in (Path(STREAK_VIDEOS_DIR), Path("/app/streak_videos"), Path("./streak_videos")):
        files.extend(_list_files(loc, {".mp4", ".mov", ".webm"}))
    return random.choice(files) if files else None

# -------------------- db helpers --------------------
async def get_db():
    conn = await aiosqlite.connect(DB_PATH)
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
    cols = {row[1] for row in await (await conn.execute("PRAGMA table_info(tasks)")).fetchall()}
    if "due_type" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN due_type TEXT")
    if "due_at" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
    if "threat_count" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN threat_count INTEGER DEFAULT 0")
    if "closed" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN closed INTEGER DEFAULT 0")
    if "completed_at" not in cols:
        await conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
    await conn.commit()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            channel_id TEXT,
            text TEXT,
            remind_at TEXT,
            created_at TEXT,
            sent INTEGER DEFAULT 0
        )
    """)
    await conn.commit()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS celebrations(
            user_id TEXT,
            task_date TEXT,
            sent INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, task_date)
        )
    """)
    await conn.commit()
    # record of '7-day multiple' videos sent to avoid duplicates per day
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS streak_awards(
            user_id TEXT,
            award_date TEXT,     -- local date we evaluated (YYYY-MM-DD)
            streak_len INTEGER,
            PRIMARY KEY(user_id, award_date)
        )
    """)
    await conn.commit()
    return conn

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_iso(s: str | None):
    if not s: return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

def today_iso() -> str:
    return dt.date.today().isoformat()

def to_utc(dt_local: dt.datetime) -> dt.datetime:
    return dt_local.astimezone(dt.timezone.utc)

def end_of_day_utc(date_obj: dt.date, tz_str: str) -> dt.datetime:
    tz = ZoneInfo(tz_str)
    local_eod = dt.datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, 59, tzinfo=tz)
    return to_utc(local_eod)

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
        print("[commands] Sync error:", repr(e))
        traceback.print_exc()

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
    # NEW: streak digest at 3:00 AM local time
    scheduler.add_job(streak_digest_all, CronTrigger(hour=3, minute=0, timezone=tz))
    scheduler.start()

# -------------------- slash commands --------------------
@bot.tree.command(name="help", description="Show MadsMinder commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**MadsMinder — Commands**\n"
        "• `/addtask text:<task>`\n"
        "• `/taskby days:<N> text:<task>`\n"
        "• `/taskon date:<YYYY-MM-DD> text:<task>`\n"
        "• `/remindme hours:<N> text:<note>`\n"
        "• `/mytasks scope:(today|open|all)`\n"
        "• `/cleartasks scope:(today|open|all)`\n"
        "• `/peptalk` (random MP3 pep talk)\n"
        "\nAt 3:00 AM, you’ll receive your streak status."
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="addtask", description="Add a task for today")
async def addtask(interaction: discord.Interaction, text: str):
    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(f"**Task for {interaction.user.display_name} ({today_iso()})**\n• {text}")
    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at,
                          last_threat_at, due_type, due_at, threat_count, closed, completed_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL, 0, 0, NULL)
    """, (
        str(interaction.user.id), today_iso(), text,
        str(task_msg.id), str(task_msg.channel.id), now_utc().isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Noted.", ephemeral=True)

@bot.tree.command(name="taskby", description="Task due within N days (nudges begin after that window)")
async def taskby(interaction: discord.Interaction, days: int, text: str):
    if days <= 0 or days > 365:
        await interaction.response.send_message("Days must be 1–365.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name}** — due within {days} day(s)\n• {text}"
    )
    tz = ZoneInfo(TZ)
    due_date_local = (dt.datetime.now(tz) + dt.timedelta(days=days)).date()
    due_at_utc = end_of_day_utc(due_date_local, TZ)
    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at,
                          last_threat_at, due_type, due_at, threat_count, closed, completed_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, 'by_days', ?, 0, 0, NULL)
    """, (
        str(interaction.user.id), today_iso(), text,
        str(task_msg.id), str(task_msg.channel.id), now_utc().isoformat(), due_at_utc.isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Registered.", ephemeral=True)

@bot.tree.command(name="taskon", description="Task due by the end of a specific date (YYYY-MM-DD)")
async def taskon(interaction: discord.Interaction, date: str, text: str):
    try:
        due_date = dt.date.fromisoformat(date)
    except Exception:
        await interaction.response.send_message("Use YYYY-MM-DD.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name}** — due by end of {date}\n• {text}"
    )
    due_at_utc = end_of_day_utc(due_date, TZ)
    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at,
                          last_threat_at, due_type, due_at, threat_count, closed, completed_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL, 'on_date', ?, 0, 0, NULL)
    """, (
        str(interaction.user.id), today_iso(), text,
        str(task_msg.id), str(task_msg.channel.id), now_utc().isoformat(), due_at_utc.isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send("Understood.", ephemeral=True)

@bot.tree.command(name="remindme", description="DM me a reminder after N hours")
async def remindme(interaction: discord.Interaction, hours: int, text: str):
    if hours <= 0 or hours > 24*14:
        await interaction.response.send_message("Hours must be 1–336.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    remind_at = now_utc() + dt.timedelta(hours=hours)
    conn = await get_db()
    await conn.execute("""
        INSERT INTO reminders(user_id, channel_id, text, remind_at, created_at, sent)
        VALUES (?, ?, ?, ?, ?, 0)
    """, (
        str(interaction.user.id), str(interaction.channel_id), text, remind_at.isoformat(), now_utc().isoformat()
    ))
    await conn.commit(); await conn.close()
    await interaction.followup.send(f"Noted. I’ll whisper in {hours} hour(s).", ephemeral=True)

@bot.tree.command(name="mytasks", description="View your tasks")
@app_commands.describe(scope="Which tasks to show: today, open, or all")
@app_commands.choices(
    scope=[
        app_commands.Choice(name="today", value="today"),
        app_commands.Choice(name="open",  value="open"),
        app_commands.Choice(name="all",   value="all"),
    ]
)
async def mytasks(interaction: discord.Interaction, scope: app_commands.Choice[str] = None):
    scope_val = (scope.value if scope else "today").lower()
    uid = str(interaction.user.id)
    conn = await get_db()

    if scope_val == "today":
        cur = await conn.execute(
            "SELECT task_text, done, task_date FROM tasks WHERE user_id=? AND task_date=? ORDER BY id ASC",
            (uid, today_iso())
        )
    elif scope_val == "open":
        cur = await conn.execute(
            "SELECT task_text, done, task_date FROM tasks WHERE user_id=? AND done=0 ORDER BY created_at ASC",
            (uid,)
        )
    else:  # all
        cur = await conn.execute(
            "SELECT task_text, done, task_date FROM tasks WHERE user_id=? ORDER BY created_at DESC",
            (uid,)
        )
    rows = await cur.fetchall()
    await cur.close(); await conn.close()

    if not rows:
        await interaction.response.send_message("No tasks match that view.", ephemeral=True)
        return

    def line(i, text, done, d):
        return f"{'✅' if done else '⬜️'} {i}) [{d}] {text}" if scope_val != "today" else f"{'✅' if done else '⬜️'} {i}) {text}"

    out = "\n".join(line(i, t, d, date) for i, (t, d, date) in enumerate(rows, 1))
    title = "**Your tasks**" if scope_val != "today" else "**Your tasks for today**"
    await interaction.response.send_message(f"{title}\n{out}", ephemeral=True)

@bot.tree.command(name="cleartasks", description="Clear your tasks (today | open | all)")
@app_commands.describe(scope="Which tasks to clear: today, open, or all")
@app_commands.choices(
    scope=[
        app_commands.Choice(name="today", value="today"),
        app_commands.Choice(name="open",  value="open"),
        app_commands.Choice(name="all",   value="all"),
    ]
)
async def cleartasks(interaction: discord.Interaction, scope: app_commands.Choice[str] = None):
    scope_val = (scope.value if scope else "today").lower()
    uid = str(interaction.user.id)
    conn = await get_db()

    # Count first
    if scope_val == "today":
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND task_date=?", (uid, today_iso()))
    elif scope_val == "open":
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id=? AND done=0", (uid,))
    else:
        cur = await conn.execute("SELECT COUNT(*) FROM tasks WHERE user_id=?", (uid,))
    (count_to_delete,) = await cur.fetchone(); await cur.close()

    # Delete + tidy celebrations
    if scope_val == "today":
        await conn.execute("DELETE FROM tasks WHERE user_id=? AND task_date=?", (uid, today_iso()))
        await conn.execute("DELETE FROM celebrations WHERE user_id=? AND task_date=?", (uid, today_iso()))
    elif scope_val == "open":
        await conn.execute("DELETE FROM tasks WHERE user_id=? AND done=0", (uid,))
    else:
        await conn.execute("DELETE FROM tasks WHERE user_id=?", (uid,))
        await conn.execute("DELETE FROM celebrations WHERE user_id=?", (uid,))
    await conn.commit(); await conn.close()

    await interaction.response.send_message(f"Cleared **{count_to_delete}** task(s) ({scope_val}).", ephemeral=True)

@bot.tree.command(name="peptalk", description="Post a random pep talk MP3")
async def peptalk(interaction: discord.Interaction):
    mp3 = pick_peptalk_mp3()
    if not mp3:
        await interaction.response.send_message(
            f"No pep talks found. Expected in: {PEPTALKS_DIR}, /app/peptalks, ./peptalks, or /peptalks",
            ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        await interaction.followup.send(file=discord.File(mp3))
    except Exception:
        await interaction.followup.send("Audio refused to cooperate. Try again.", ephemeral=True)

# -------------------- reactions --------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅":
        return
    conn = await get_db()
    cur = await conn.execute("SELECT user_id, done FROM tasks WHERE message_id=?", (str(payload.message_id),))
    row = await cur.fetchone(); await cur.close()
    if not row:
        await conn.close(); return
    user_id, done = row
    if str(payload.user_id) != user_id:
        await conn.close(); return
    if not done:
        await conn.execute(
            "UPDATE tasks SET done=1, completed_at=? WHERE message_id=?",
            (now_utc().isoformat(), str(payload.message_id))
        )
        await conn.commit()
    await conn.close()

    channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
    user = await bot.fetch_user(payload.user_id)
    await channel.send(f"{user.mention} {pick(LINES['task_tick'])}")

    # celebration check: count tasks completed today (any task type)
    conn2 = await get_db()
    cur2 = await conn2.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id=? AND done=1 AND DATE(completed_at)=?",
        (str(payload.user_id), today_iso())
    )
    (done_count,) = await cur2.fetchone(); await cur2.close()
    cur3 = await conn2.execute(
        "SELECT sent FROM celebrations WHERE user_id=? AND task_date=?",
        (str(payload.user_id), today_iso())
    )
    row3 = await cur3.fetchone(); await cur3.close()
    if done_count >= CELEBRATE_THRESHOLD and (not row3 or row3[0] == 0):
        img = pick_celebration_image()
        say = pick(LINES["celebrate"])
        try:
            if img:
                await channel.send(content=f"{user.mention} {say}", file=discord.File(img))
            else:
                await channel.send(f"{user.mention} {say}")
        except Exception:
            pass
        await conn2.execute(
            """INSERT INTO celebrations(user_id, task_date, sent) VALUES(?, ?, 1)
               ON CONFLICT(user_id, task_date) DO UPDATE SET sent=1""",
            (str(payload.user_id), today_iso())
        )
        await conn2.commit()
    await conn2.close()

# -------------------- jobs --------------------
async def daily_prompt():
    if not ANNOUNCE_CHANNEL_ID: return
    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
    await channel.send(f"{pick(LINES['daily_prompt'])}\nUse `/addtask` to register a task.")

async def threat_scan():
    conn = await get_db()
    cur = await conn.execute("""
        SELECT id, user_id, message_id, channel_id, created_at, last_threat_at,
               due_type, due_at, threat_count, closed
        FROM tasks
        WHERE done=0
    """)
    rows = await cur.fetchall(); await cur.close()
    now = now_utc()
    for (tid, user_id, message_id, channel_id, created_at, last_threat_at,
         due_type, due_at, threat_count, closed) in rows:
        if closed: continue
        created_dt = parse_iso(created_at); last_dt = parse_iso(last_threat_at); due_dt = parse_iso(due_at)
        if not created_dt: continue
        if (threat_count or 0) >= MAX_THREATS_PER_TASK: continue
        if due_dt is not None:
            ready = now >= due_dt
        else:
            age_min = (now - created_dt).total_seconds() / 60
            ready = age_min >= THREAT_GRACE_MINUTES
        if not ready: continue
        cooldown_ok = (last_dt is None) or ((now - last_dt).total_seconds() / 60 >= THREAT_COOLDOWN_MINUTES)
        if not cooldown_ok: continue
        try:
            channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
            msg = await channel.fetch_message(int(message_id))
            await msg.reply(pick(LINES["threat"]))
            await conn.execute(
                "UPDATE tasks SET last_threat_at=?, threat_count=COALESCE(threat_count,0)+1 WHERE id=?",
                (now.isoformat(), tid)
            ); await conn.commit()
        except Exception:
            await conn.execute("UPDATE tasks SET closed=1, last_threat_at=? WHERE id=?", (now.isoformat(), tid))
            await conn.commit()
    await conn.close()

async def reminder_scan():
    conn = await get_db()
    cur = await conn.execute("SELECT id, user_id, channel_id, text, remind_at FROM reminders WHERE sent=0")
    rows = await cur.fetchall(); await cur.close()
    now = now_utc()
    for (rid, user_id, channel_id, text, remind_at) in rows:
        due = parse_iso(remind_at)
        if not due or due > now: continue
        try:
            user = await bot.fetch_user(int(user_id))
            try:
                await user.send(f"Reminder: {text}")
            except Exception:
                try:
                    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                    await channel.send(f"<@{user_id}> Reminder: {text}")
                except Exception: pass
        finally:
            await conn.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
            await conn.commit()
    await conn.close()

# -------------------- streak logic --------------------
def local_date_today(tz_str: str) -> dt.date:
    tz = ZoneInfo(tz_str)
    return dt.datetime.now(tz).date()

def local_date_yesterday(tz_str: str) -> dt.date:
    return local_date_today(tz_str) - dt.timedelta(days=1)

async def get_all_user_ids(conn) -> list[str]:
    cur = await conn.execute("SELECT DISTINCT user_id FROM tasks")
    rows = await cur.fetchall(); await cur.close()
    return [r[0] for r in rows]

async def completed_on_date(conn, user_id: str, day: dt.date) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM tasks WHERE user_id=? AND done=1 AND DATE(completed_at)=? LIMIT 1",
        (user_id, day.isoformat())
    )
    row = await cur.fetchone(); await cur.close()
    return bool(row)

async def compute_streak(conn, user_id: str, end_day: dt.date) -> int:
    """
    Count consecutive days (backwards from end_day inclusive) with >=1 completion.
    """
    streak = 0
    day = end_day
    # cap lookback to 365 to avoid runaway loops
    for _ in range(365):
        ok = await completed_on_date(conn, user_id, day)
        if not ok:
            break
        streak += 1
        day = day - dt.timedelta(days=1)
    return streak

async def streak_digest_all():
    """
    At 3:00 AM local time:
      - For every known user, compute streak ending at YESTERDAY (local).
      - DM the user with their streak and a line (keep vs reset).
      - If streak is a multiple of 7, post a random video (DM + optional announce).
      - Record award in streak_awards to avoid duplicate sends for the same day.
    """
    local_yday = local_date_yesterday(TZ)
    conn = await get_db()
    users = await get_all_user_ids(conn)
    for user_id in users:
        streak = await compute_streak(conn, user_id, local_yday)
        # Send DM
        try:
            user = await bot.fetch_user(int(user_id))
            if streak > 0:
                line = pick(LINES["streak_keep"])
                await user.send(f"Streak: **{streak}** day(s). {line}")
            else:
                line = pick(LINES["streak_reset"])
                await user.send(f"Streak: **0**. {line}")
        except Exception:
            pass

        # Check 7-day multiple reward, avoid duplicate for this date
        if streak > 0 and streak % 7 == 0:
            cur = await conn.execute(
                "SELECT 1 FROM streak_awards WHERE user_id=? AND award_date=?",
                (user_id, local_yday.isoformat())
            )
            already = await cur.fetchone(); await cur.close()
            if not already:
                vid = pick_streak_video()
                try:
                    user = await bot.fetch_user(int(user_id))
                    if vid:
                        await user.send(content=f"Seven in a row. Sustained taste.", file=discord.File(vid))
                    else:
                        await user.send("Seven in a row. Sustained taste. (No video found.)")
                except Exception:
                    pass
                # Optionally also announce in a channel
                if ANNOUNCE_CHANNEL_ID:
                    try:
                        channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
                        if vid:
                            await channel.send(content=f"<@{user_id}> Seven days. Civilised.", file=discord.File(vid))
                        else:
                            await channel.send(f"<@{user_id}> Seven days. Civilised. (No video found.)")
                    except Exception:
                        pass
                await conn.execute(
                    "INSERT INTO streak_awards(user_id, award_date, streak_len) VALUES(?, ?, ?)",
                    (user_id, local_yday.isoformat(), streak)
                )
                await conn.commit()
    await conn.close()

@bot.tree.command(name="askmads", description="Ask MadsMinder anything. He’ll answer… in his style.")
async def askmads(interaction: discord.Interaction, question: str):
    # basic guard
    if openai_client is None:
        await interaction.response.send_message(
            "This feature isn’t configured. Ask the admin to set OPENAI_API_KEY.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True, ephemeral=False)

    # Persona prompt: emulate the cool, dry tone—without claiming to be the real person.
    system_instructions = (
        "You are 'MadsMinder', a laconic, sharp-witted productivity consigliere with a cool, dry Danish cadence. "
        "You speak briefly, precisely, and with understated elegance. "
        "Offer practical, grounded answers with a calm, slightly ominous charm. "
        "Avoid explicit impersonation claims; you're an assistant with that vibe. "
        "Keep replies under 180–220 words unless the user asks for detail."
    )

    try:
        # OpenAI Responses API (simple, reliable)
        resp = openai_client.responses.create(
            model="gpt-4o",  # or "gpt-4o-mini" to save cost
            input=[{
                "role": "system",
                "content": system_instructions
            },{
                "role": "user",
                "content": question
            }],
            temperature=0.7,
        )
        text = resp.output_text.strip()
    except Exception as e:
        text = f"(MadsMinder pauses.) Something went wrong: `{e}`"

    await interaction.followup.send(text)


# -------------------- entrypoint --------------------
if __name__ == "__main__":
    print("[startup] starting discord client…")
    bot.run(TOKEN, log_handler=None)
