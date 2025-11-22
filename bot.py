print("=" * 50)
print("BOT SCRIPT LOADING...")
print("=" * 50)

import os
import asyncio
from datetime import datetime, date, timedelta, time, timezone
import discord
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput

# --- NEW MONGO IMPORTS ---
from pymongo import MongoClient
from pymongo.collection import Collection

# Import the keep_alive function to prevent the Repl from sleeping
from keep_alive import keep_alive

# ---------------------------------------------
# ---- MONGO DB SETUP ----
# ---------------------------------------------

MONGO_CLIENT: MongoClient = None
USER_COLLECTION: Collection = None
SETTINGS_COLLECTION: Collection = None

def init_db():
    """Initializes MongoDB connection and collection objects."""
    global MONGO_CLIENT, USER_COLLECTION, SETTINGS_COLLECTION

    MONGO_URI = os.getenv("MONGO_URI")
    if not MONGO_URI:
        print("FATAL: MONGO_URI environment variable is not set.")
        return

    try:
        MONGO_CLIENT = MongoClient(MONGO_URI)
        db = MONGO_CLIENT.get_database("pushup_db")
        USER_COLLECTION = db.get_collection("user_stats")
        SETTINGS_COLLECTION = db.get_collection("bot_settings")

        print("MongoDB connection successful. Collections initialized.")

        default_settings = {
            "_id": "message_ids",
            "leaderboard_msg_id": 0,
            "quick_log_msg_id": 0,
            "reminder_control_msg_id": 0
        }
        SETTINGS_COLLECTION.update_one(
            {"_id": "message_ids"},
            {"$setOnInsert": default_settings},
            upsert=True
        )

    except Exception as e:
        print(f"FATAL: Failed to connect to MongoDB: {e}")
        MONGO_CLIENT = None
        USER_COLLECTION = None

def get_user_stats(user_id):
    if USER_COLLECTION is None: return None
    return USER_COLLECTION.find_one({"user_id": user_id})

def get_all_users():
    if USER_COLLECTION is None: return []
    return list(USER_COLLECTION.find({}))

def update_user_stats(user_id, **kwargs):
    if USER_COLLECTION is None: return
    
    # Initialize defaults if user doesn't exist
    on_insert_data = {
        "user_id": user_id,
        "total_pushups": 0,
        "reminder_level": 0, # 0 = Off, 1 = On
        "daily_logs": {},
        "last_reminder_check": 0
    }
    clean_on_insert = {k: v for k, v in on_insert_data.items() if k not in kwargs}
    
    USER_COLLECTION.update_one(
        {"user_id": user_id},
        {"$set": kwargs, "$setOnInsert": clean_on_insert},
        upsert=True
    )

def log_pushups(user_id, reps, log_date):
    if USER_COLLECTION is None: return
    date_str = log_date.strftime("%Y-%m-%d")
    USER_COLLECTION.update_one(
        {"user_id": user_id},
        {
            "$inc": {
                "total_pushups": reps,
                f"daily_logs.{date_str}": reps 
            },
            "$setOnInsert": {
                "user_id": user_id,
                "reminder_level": 0,
                "last_reminder_check": 0,
            }
        },
        upsert=True
    )

def calculate_streak(user_stats):
    daily_logs = user_stats.get("daily_logs", {})
    if not daily_logs: return 0

    # Sort logs by date (newest first)
    logged_dates = sorted([
        datetime.strptime(d, "%Y-%m-%d").date() 
        for d, reps in daily_logs.items() if reps > 0
    ], reverse=True)

    if not logged_dates: return 0

    today = datetime.now(TZ).date()
    current_streak = 0
    most_recent_log = logged_dates[0]

    if most_recent_log == today:
        current_streak = 1
        expected_date = today - timedelta(days=1)
        check_logs = logged_dates[1:]
    elif most_recent_log == today - timedelta(days=1):
        current_streak = 1
        expected_date = today - timedelta(days=2)
        check_logs = logged_dates
    else:
        return 0

    for log_date in check_logs:
        if log_date == expected_date:
            current_streak += 1
            expected_date -= timedelta(days=1)
        elif log_date < expected_date:
            break

    return current_streak


# ---- CONSTANTS & ENV ----
try:
    LEADERBOARD_CHANNEL = int(os.getenv("LEADERBOARD_CHANNEL"))
except (TypeError, ValueError):
    print("WARNING: LEADERBOARD_CHANNEL environment variable is missing or invalid.")
    LEADERBOARD_CHANNEL = 0 

# Timezone (UTC)
TZ = timezone.utc 

# --- REMINDER SCHEDULE ---
# 4 fixed stages. 
REMINDER_SCHEDULE = {
    1: time(hour=9, minute=0, tzinfo=TZ),   # Stage 1: Morning
    2: time(hour=14, minute=0, tzinfo=TZ),  # Stage 2: Afternoon
    3: time(hour=20, minute=0, tzinfo=TZ),  # Stage 3: Evening
    4: time(hour=23, minute=30, tzinfo=TZ), # Stage 4: 11:30 PM Panic
}

# --- ESCALATING MESSAGES ---
URGENCY_MESSAGES = {
    1: "🌅 **Good morning!** Start your day strong with your daily pushups.",
    2: "🕑 **Afternoon check-in.** Have you done your pushups yet? Don't let the day slip away!",
    3: "🌇 **Evening Reminder.** The day is almost over! Get those reps in to keep your streak alive.",
    4: "🚨 **FINAL CALL (11:30 PM)** 🚨\nYou have 30 minutes to log your pushups or you lose your streak! **GO GO GO!**"
}

# Setup Intents
intents = discord.Intents.default()
intents.message_content = False 
intents.dm_messages = True 
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------
# ---- PERSISTENT MESSAGE ID HANDLING ----
# ---------------------------------------------

def _get_setting(key):
    if SETTINGS_COLLECTION is None: return 0
    settings = SETTINGS_COLLECTION.find_one({"_id": "message_ids"})
    return settings.get(key, 0) if settings else 0

def _set_setting(key, value):
    if SETTINGS_COLLECTION is None: return
    SETTINGS_COLLECTION.update_one(
        {"_id": "message_ids"},
        {"$set": {key: value}},
        upsert=True
    )

def get_leaderboard_msg_id(): return _get_setting("leaderboard_msg_id")
def set_leaderboard_msg_id(msg_id): _set_setting("leaderboard_msg_id", msg_id)
def get_quick_log_msg_id(): return _get_setting("quick_log_msg_id")
def set_quick_log_msg_id(msg_id): _set_setting("quick_log_msg_id", msg_id)
def get_reminder_control_msg_id(): return _get_setting("reminder_control_msg_id")
def set_reminder_control_msg_id(msg_id): _set_setting("reminder_control_msg_id", msg_id)

# ---------------------------------------------
# ---- VIEW COMPONENTS ----
# ---------------------------------------------

class RepsModal(Modal, title='Log Pushups'):
    reps_input = TextInput(label='Number of Pushups', placeholder='e.g., 20', required=True, max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            reps = int(self.reps_input.value.strip())
            if reps <= 0:
                await interaction.response.send_message("Please enter a positive number.", ephemeral=True)
                return

            log_pushups(interaction.user.id, reps, datetime.now(TZ).date())
            user_stats = get_user_stats(interaction.user.id)
            streak = calculate_streak(user_stats) if user_stats else 0 

            await interaction.response.send_message(
                f"💪 Logged **{reps}** pushups! Current streak: **{streak}** days.", 
                ephemeral=True
            )
            await update_leaderboard(interaction.client)
            await update_quick_log_message(interaction.client)

        except ValueError:
            await interaction.response.send_message("Invalid input. Enter a whole number.", ephemeral=True)
        except Exception as e:
            print(f"Error processing modal: {e}")
            await interaction.response.send_message("Error logging pushups.", ephemeral=True)

class PushupQuickLogView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Log Pushups", style=discord.ButtonStyle.primary, custom_id="quick_log_button", emoji="📝")
    async def quick_log_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RepsModal()) 

class ReminderToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="reminder_toggle_select",
        placeholder="Manage your reminders...",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label="Enable Reminders", 
                value="1", 
                emoji="🔔", 
                description="Get increasingly urgent reminders until you log."
            ),
            discord.SelectOption(
                label="Disable Reminders", 
                value="0", 
                emoji="🔕", 
                description="Turn off all daily DMs."
            ),
        ]
    )
    async def reminder_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        setting_value = int(select.values[0])
        user_id = interaction.user.id

        # Update DB
        update_user_stats(user_id, reminder_level=setting_value)

        status_text = "ENABLED" if setting_value == 1 else "DISABLED"
        color_emoji = "✅" if setting_value == 1 else "❌"

        await interaction.response.send_message(
            f"{color_emoji} Reminders **{status_text}**. "
            f"{'You will receive reminders until you log your reps for the day.' if setting_value == 1 else 'You will no longer be bothered.'}", 
            ephemeral=True
        )

# ---------------------------------------------
# ---- PERSISTENT MESSAGE MANAGEMENT ----
# ---------------------------------------------

async def setup_reminder_control_message(bot_instance: commands.Bot):
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel: return

    msg_id = get_reminder_control_msg_id()
    view = ReminderToggleView()

    embed = discord.Embed(
        title="🔔 Pushup Reminder Settings",
        description=(
            "Turn reminders **On** to receive motivation throughout the day.\n"
            "Reminders stop automatically for the day once you log your reps!\n\n"
            "**Schedule:**\n"
            "1. Morning Nudge\n"
            "2. Afternoon Check-in\n"
            "3. Evening Urgent\n"
            "4. **11:30 PM FINAL CALL**"
        ),
        color=discord.Color.blue()
    )

    try:
        if msg_id != 0:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            set_reminder_control_msg_id(msg.id)
    except discord.NotFound:
        msg = await channel.send(embed=embed, view=view)
        set_reminder_control_msg_id(msg.id)
    except Exception as e:
        print(f"Failed to setup reminder msg: {e}")

async def update_quick_log_message(bot_instance: commands.Bot):
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel: return

    msg_id = get_quick_log_msg_id()
    view = PushupQuickLogView()
    embed = discord.Embed(title="Quick Log Your Pushups 📝", description="Click below to log reps!", color=discord.Color.green())

    try:
        if msg_id != 0:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        else:
            msg = await channel.send(embed=embed, view=view)
            set_quick_log_msg_id(msg.id)
    except discord.NotFound:
        msg = await channel.send(embed=embed, view=view)
        set_quick_log_msg_id(msg.id)
    except Exception:
        pass

# --- LEADERBOARD LOGIC ---

def get_leaderboard_data(all_users, time_frame="all"):
    """Calculates rep totals for specific time frames."""
    now = datetime.now(TZ).date()
    if time_frame == "today": start_date = now
    elif time_frame == "week": start_date = now - timedelta(days=now.weekday())
    else: start_date = date.min 

    leaderboard_totals = []
    for user_stats in all_users:
        total_reps = 0
        for date_str, reps in user_stats.get("daily_logs", {}).items():
            try:
                log_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if log_date >= start_date: total_reps += reps
            except ValueError: continue
        if total_reps > 0: leaderboard_totals.append((user_stats, total_reps))

    return sorted(leaderboard_totals, key=lambda x: x[1], reverse=True)

def get_streak_leaderboard_data(all_users):
    """Calculates active streaks for all users and returns sorted list."""
    streak_data = []
    for user_stats in all_users:
        streak = calculate_streak(user_stats)
        if streak > 0:
            streak_data.append((user_stats, streak))
    
    # Sort by streak length (descending)
    return sorted(streak_data, key=lambda x: x[1], reverse=True)

async def format_leaderboard(bot_instance, leaderboard_data):
    """Formats rep-based leaderboards."""
    text = ""
    for i, (user_stats, total_reps) in enumerate(leaderboard_data[:10]):
        user_id = user_stats["user_id"]
        try:
            member = bot_instance.get_user(user_id) or await bot_instance.fetch_user(user_id)
            name = member.display_name if member else f"ID: {user_id}"
        except: name = f"ID: {user_id}"
        
        emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"#{i+1}"
        text += f"{emoji} **{name}**: {total_reps:,}\n"
    return text if text else "No logs yet!"

async def format_streak_leaderboard(bot_instance, streak_data):
    """Formats the specific streak leaderboard."""
    text = ""
    for i, (user_stats, streak) in enumerate(streak_data[:10]):
        user_id = user_stats["user_id"]
        try:
            member = bot_instance.get_user(user_id) or await bot_instance.fetch_user(user_id)
            name = member.display_name if member else f"ID: {user_id}"
        except: name = f"ID: {user_id}"
        
        emoji = "👑" if i == 0 else "🔥" 
        text += f"{emoji} **{name}**: {streak} days\n"
    return text if text else "No active streaks!"

async def update_leaderboard(bot_instance: commands.Bot):
    if MONGO_CLIENT is None: return
    all_users = get_all_users()
    
    # Get Data
    daily_data = get_leaderboard_data(all_users, "today")
    weekly_data = get_leaderboard_data(all_users, "week")
    all_time_data = get_leaderboard_data(all_users, "all")
    streak_data = get_streak_leaderboard_data(all_users)

    # Create Embed
    embed = discord.Embed(title="🏆 Pushup Leaderboards", color=discord.Color.gold())
    
    # Add Streak Field (Placed first for motivation)
    embed.add_field(name="🔥 Longest Active Streaks", value=await format_streak_leaderboard(bot_instance, streak_data), inline=False)
    
    embed.add_field(name="🗓️ Today's Reps", value=await format_leaderboard(bot_instance, daily_data), inline=False)
    embed.add_field(name="📅 This Week's Reps", value=await format_leaderboard(bot_instance, weekly_data), inline=False)
    embed.add_field(name="💪 All-Time Reps", value=await format_leaderboard(bot_instance, all_time_data), inline=False)
    
    embed.set_footer(text=f"Updated: {datetime.now(TZ).strftime('%H:%M UTC')}")

    # Send/Edit Message
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel: return
    msg_id = get_leaderboard_msg_id()
    
    try:
        if msg_id != 0:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
        else:
            msg = await channel.send(embed=embed)
            set_leaderboard_msg_id(msg.id)
    except discord.NotFound:
        msg = await channel.send(embed=embed)
        set_leaderboard_msg_id(msg.id)
    except Exception: pass

# ---------------------------------------------
# ---- BACKGROUND TASKS & REMINDERS ----
# ---------------------------------------------

@tasks.loop(minutes=30)
async def persistent_message_task():
    await update_leaderboard(bot)
    await update_quick_log_message(bot)
    await setup_reminder_control_message(bot)

async def send_reminders_at_stage(stage_level):
    """
    Sends reminders to users who have reminders enabled AND haven't logged yet today.
    """
    if MONGO_CLIENT is None: return

    current_timestamp = int(datetime.now(TZ).timestamp())
    ten_minutes_ago = current_timestamp - 600
    all_users = get_all_users()

    print(f"--- Running Reminder Stage {stage_level} ---")

    for user_stats in all_users:
        user_id = user_stats["user_id"]
        
        # Check if user has reminders Enabled (level 1 = ON)
        is_enabled = user_stats.get("reminder_level", 0) == 1
        last_reminder_check = user_stats.get("last_reminder_check", 0)

        if not is_enabled: continue

        # Check if they have logged today
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        logged_today = user_stats.get("daily_logs", {}).get(today_str, 0)

        # IF: Reminders ON, Not Logged, Not recently messaged
        if logged_today == 0 and last_reminder_check < ten_minutes_ago:
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                msg_content = URGENCY_MESSAGES.get(stage_level, "Reminder: Do your pushups!")
                
                await user.send(msg_content)
                print(f"Sent Stage {stage_level} reminder to {user.display_name}")

                update_user_stats(user_id, last_reminder_check=current_timestamp)

            except discord.Forbidden:
                print(f"Cannot DM user {user_id}.")
            except Exception as e:
                print(f"Error reminding {user_id}: {e}")

# Specific time loops for the 4 stages
@tasks.loop(time=REMINDER_SCHEDULE[1])
async def reminder_stage_1(): await send_reminders_at_stage(1)

@tasks.loop(time=REMINDER_SCHEDULE[2])
async def reminder_stage_2(): await send_reminders_at_stage(2)

@tasks.loop(time=REMINDER_SCHEDULE[3])
async def reminder_stage_3(): await send_reminders_at_stage(3)

@tasks.loop(time=REMINDER_SCHEDULE[4])
async def reminder_stage_4(): await send_reminders_at_stage(4) 

@persistent_message_task.before_loop
async def before_persistent_message_task(): await bot.wait_until_ready()

@reminder_stage_1.before_loop
@reminder_stage_2.before_loop
@reminder_stage_3.before_loop
@reminder_stage_4.before_loop
async def before_reminder_tasks(): await bot.wait_until_ready()

# ---------------------------------------------
# ---- COMMANDS ----
# ---------------------------------------------

@bot.tree.command(name="log", description="Log pushups for a specific user/date (Admin).")
@commands.has_permissions(administrator=True)
async def log_command(interaction: discord.Interaction, user: discord.Member, reps: int, log_date: str):
    try:
        if reps <= 0: return await interaction.response.send_message("Must be positive.", ephemeral=True)
        date_obj = datetime.strptime(log_date, "%Y-%m-%d").date()
        log_pushups(user.id, reps, date_obj)
        await interaction.response.send_message(f"✅ Logged **{reps}** for **{user.display_name}** on **{log_date}**.")
        await update_leaderboard(bot)
    except ValueError:
        await interaction.response.send_message("Invalid date. Use YYYY-MM-DD.", ephemeral=True)

@bot.tree.command(name="stats", description="Show your stats.")
async def stats_command(interaction: discord.Interaction):
    user_stats = get_user_stats(interaction.user.id)
    total = user_stats.get("total_pushups", 0) if user_stats else 0
    streak = calculate_streak(user_stats) if user_stats else 0
    await interaction.response.send_message(f"Total: **{total:,}** | Streak: **{streak}** days.", ephemeral=True)

@bot.tree.command(name="editreps", description="Adjust reps for a user/date (Admin).")
@commands.has_permissions(administrator=True)
async def edit_reps_command(interaction: discord.Interaction, user: discord.Member, delta_reps: int, log_date: str):
    """
    Detailed version: Shows new totals after editing.
    """
    try:
        date_obj = datetime.strptime(log_date, "%Y-%m-%d").date()
        date_str = date_obj.strftime("%Y-%m-%d")
        
        # 1. Update the database
        USER_COLLECTION.update_one(
            {"user_id": user.id},
            {"$inc": {"total_pushups": delta_reps, f"daily_logs.{date_str}": delta_reps},
             "$setOnInsert": {"user_id": user.id, "reminder_level": 0, "last_reminder_check": 0}},
            upsert=True
        )

        # 2. Fetch new stats to show the user
        user_stats = get_user_stats(user.id)
        # Check if user_stats is None (unlikely after upsert, but safe to check)
        if not user_stats:
             await interaction.response.send_message("Error fetching updated stats.", ephemeral=True)
             return
             
        new_total = user_stats.get("total_pushups", 0)
        new_daily = user_stats.get("daily_logs", {}).get(date_str, 0)
        new_streak = calculate_streak(user_stats)

        await interaction.response.send_message(
            f"✏️ **Adjustment Successful** for {user.display_name} on {log_date}:\n"
            f"• Change: {delta_reps:+}\n"
            f"• New Daily: {new_daily}\n"
            f"• New Total: {new_total:,}\n"
            f"• Current Streak: {new_streak} days"
        )
        
        # 3. Update the leaderboard
        await update_leaderboard(bot)

    except ValueError:
        await interaction.response.send_message("Invalid date. Use YYYY-MM-DD.", ephemeral=True)
    except Exception as e:
        print(f"Error in editreps: {e}")
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)


# ---------------------------------------------
# ---- BOT EVENTS ----
# ---------------------------------------------

async def check_for_missed_reminders(current_time: datetime):
    """Checks if bot missed a reminder time recently due to restart."""
    if MONGO_CLIENT is None: return
    
    # Iterate through the 4 stages
    for stage, schedule_time in REMINDER_SCHEDULE.items():
        scheduled_dt = datetime.combine(current_time.date(), schedule_time, tzinfo=TZ)
        
        # If scheduled time passed less than 30 mins ago
        if scheduled_dt < current_time and (current_time - scheduled_dt) < timedelta(minutes=30):
            print(f"Detected missed reminder Stage {stage}. Running now.")
            await send_reminders_at_stage(stage)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    if MONGO_CLIENT is None: return

    await bot.tree.sync()
    bot.add_view(PushupQuickLogView())
    bot.add_view(ReminderToggleView())

    await check_for_missed_reminders(datetime.now(TZ))

    if not persistent_message_task.is_running(): persistent_message_task.start()
    if not reminder_stage_1.is_running(): reminder_stage_1.start()
    if not reminder_stage_2.is_running(): reminder_stage_2.start()
    if not reminder_stage_3.is_running(): reminder_stage_3.start()
    if not reminder_stage_4.is_running(): reminder_stage_4.start()

print("=" * 50)
print("ABOUT TO START BOT...")
print("=" * 50)

if __name__ == "__main__":
    keep_alive()
    bot.run(os.getenv("DISCORD_BOT_TOKEN"))
