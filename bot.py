import os, asyncio, datetime as dt, random
import aiosqlite
import discord

# ... existing imports ...

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set. Add it as a Fly secret on your app.")

# (rest of config)

from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from zoneinfo import ZoneInfo

# ---------- Config via env ----------
TOKEN = os.getenv("DISCORD_TOKEN")
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))     # optional daily prompt channel
DB_PATH = os.getenv("DB_PATH", "/data/madsminder.db")
TZ = os.getenv("TZ", "America/New_York")
# Start threatening nudges this long after a task is created (default 6 hours = 360 mins)
THREAT_GRACE_MINUTES = int(os.getenv("THREAT_GRACE_MINUTES", "360"))
# Minimum minutes between threats on the same task (anti-spam)
THREAT_COOLDOWN_MINUTES = int(os.getenv("THREAT_COOLDOWN_MINUTES", "180"))

INTENTS = discord.Intents.default()
INTENTS.reactions = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- Phrase bank ----------
LINES = {
    "task_tick": [
        "One down. Understated excellence.",
        "Neat work. Don’t let it go to your head.",
        "Progress suits you.",
        "A clean strike. The kind that scares paperwork.",
        "Good. Now keep moving.",
        "Tidy work. It almost looks easy.",
        "A win, however small, is still a win.",
        "Nicely done.",
        "A quiet victory. The best kind.",
        "Well struck.",
        "That’s one fewer reason to frown.",
        "Steady hands. Keep them that way.",
        "Satisfying, isn’t it?",
        "A dent in the day. Well placed.",
        "That’s how it’s done. Without fuss.",
        "Clean and quiet—just how I like it.",
        "Another mark in your favour.",
        "One more step in the right direction.",
        "Done without drama. Excellent.",
        "If only all victories were this tidy.",
    ],
    # Threatening nudges, Hannibal-flavoured (non-violent; ominous, elegant)
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

# ---------- Discord lifecycle ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    tz = ZoneInfo(TZ)
    scheduler = AsyncIOScheduler()
    # Optional morning prompt (09:00 local TZ) in an announce channel
    if ANNOUNCE_CHANNEL_ID:
        scheduler.add_job(daily_prompt, CronTrigger(hour=9, minute=0, timezone=tz))
    # Threat scanner runs every 10 minutes
    scheduler.add_job(threat_scan, IntervalTrigger(minutes=10, timezone=tz))
    scheduler.start()

# ---------- Slash commands ----------
@bot.tree.command(name="addtask", description="Add a single task (react with ✅ when done)")
async def addtask(interaction: discord.Interaction, text: str):
    """
    Adds one task for 'today'; posts it to the channel; when user reacts ✅ the task is marked done.
    """
    await interaction.response.defer(ephemeral=True)
    # Post the task message
    task_msg = await interaction.channel.send(
        f"**Task for {interaction.user.display_name} ({today_iso()})**\n• {text}\n\n"
        f"Mark complete by reacting with ✅ to this message."
    )

    conn = await get_db()
    await conn.execute("""
        INSERT INTO tasks(user_id, task_date, task_text, done, message_id, channel_id, created_at, last_threat_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, NULL)
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

# ---------- Reactions ----------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅":
        return
    # Look up the task by message_id and today
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

# ---------- Jobs ----------
async def daily_prompt():
    if not ANNOUNCE_CHANNEL_ID:
        return
    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(ANNOUNCE_CHANNEL_ID)
    await channel.send(f"{pick(LINES['daily_prompt'])}\nUse `/addtask` to register a task.")

async def threat_scan():
    """
    Runs every 10 minutes.
    For each task that is:
     - not done,
     - older than THREAT_GRACE_MINUTES, and
     - not threatened within THREAT_COOLDOWN_MINUTES,
    the bot replies to the original task message with a threatening nudge.
    """
    conn = await get_db()
    cur = await conn.execute("""
        SELECT id, user_id, message_id, channel_id, created_at, last_threat_at
        FROM tasks
        WHERE done=0
    """)
    rows = await cur.fetchall()
    await cur.close()

    for (tid, user_id, message_id, channel_id, created_at, last_threat_at) in rows:
        created_dt = parse_iso(created_at)
        last_threat_dt = parse_iso(last_threat_at)
        if not created_dt:
            continue

        age_minutes = (now_utc() - created_dt).total_seconds() / 60
        cooldown_ok = (last_threat_dt is None) or ((now_utc() - last_threat_dt).total_seconds() / 60 >= THREAT_COOLDOWN_MINUTES)

        if age_minutes >= THREAT_GRACE_MINUTES and cooldown_ok:
            try:
                channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                msg = await channel.fetch_message(int(message_id))
                # Reply to the original task message
                await msg.reply(pick(LINES["threat"]))
                # Update cooldown timestamp
                await conn.execute("UPDATE tasks SET last_threat_at=? WHERE id=?", (now_utc().isoformat(), tid))
                await conn.commit()
            except Exception:
                # If message/channel no longer exists, stop threatening this task
                await conn.execute("UPDATE tasks SET last_threat_at=? WHERE id=?", (now_utc().isoformat(), tid))
                await conn.commit()

    await conn.close()

# ---------- Entrypoint ----------
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
