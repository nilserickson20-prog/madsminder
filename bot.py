import os, asyncio, datetime as dt, random
import aiosqlite
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

# ---------- Robust env parsing ----------
def getenv_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v not in (None, "", "null", "None") else default
    except Exception:
        return default

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Add it as a Fly secret on your app (raw token string, no 'Bot ').")

ANNOUNCE_CHANNEL_ID = getenv_int("ANNOUNCE_CHANNEL_ID", 0)  # optional morning prompt channel
DB_PATH = os.getenv("DB_PATH", "/data/madsminder.db")
TZ = os.getenv("TZ", "America/New_York")
THREAT_GRACE_MINUTES = getenv_int("THREAT_GRACE_MINUTES", 360)       # start nudging after 6h
THREAT_COOLDOWN_MINUTES = getenv_int("THREAT_COOLDOWN_MINUTES", 180) # 3h between nudges
GUILD_ID = os.getenv("GUILD_ID")  # optional; if set, sync commands to this guild instantly

INTENTS = discord.Intents.default()
INTENTS.reactions = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

print(f"[startup] TZ={TZ} ANNOUNCE_CHANNEL_ID={ANNOUNCE_CHANNEL_ID} "
      f"GRACE={THREAT_GRACE_MINUTES} COOLDOWN={THREAT_COOLDOWN_MINUTES} GUILD_ID={GUILD_ID or 'None'}")

# ---------- Phrase bank ----------
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

# ---------- Database ----------
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

# ---------- Command registration (instant guild sync) ----------
@bot.event
async def setup_hook():
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"[commands] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"[commands] Synced {len(synced)} global commands (can take up to ~1h to appear)")
    except Exception as e:
        import traceback
        print("[commands] Sync error:", repr(e))
        traceback.print_exc()

# ---------- Lifecycle ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    tz = ZoneInfo(TZ)
    scheduler = AsyncIOScheduler()
    if ANNOUNCE_CHANNEL_ID:
        scheduler.add_j_
