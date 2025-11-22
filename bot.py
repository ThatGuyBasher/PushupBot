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

# Global variables for MongoDB
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
        # Connect to MongoDB
        MONGO_CLIENT = MongoClient(MONGO_URI)
        
        # Access the database (using 'pushup_db' as a default name)
        db = MONGO_CLIENT.get_database("pushup_db")
        
        # Access collections
        USER_COLLECTION = db.get_collection("user_stats")
        SETTINGS_COLLECTION = db.get_collection("bot_settings")

        print("MongoDB connection successful. Collections initialized.")

        # --- Initialize Settings (Persistent Message IDs) ---
        default_settings = {
            "_id": "message_ids",
            "leaderboard_msg_id": 0,
            "quick_log_msg_id": 0,
            "reminder_control_msg_id": 0
        }
        # Upsert the settings document to ensure keys exist
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
    """Retrieves all stats for a user."""
    if USER_COLLECTION is None:
        return None
    return USER_COLLECTION.find_one({"user_id": user_id})

def get_all_users():
    """Returns a list of all user dictionaries."""
    if USER_COLLECTION is None:
        return []
    # Fetch all documents and convert cursor to list
    return list(USER_COLLECTION.find({}))

def update_user_stats(user_id, **kwargs):
    """Updates user fields like reminder_level or last_reminder_check."""
    if USER_COLLECTION is None:
        return

    # Use $set to update specific fields, and $setOnInsert to initialize new user data
    # if no document is found for user_id.
    update_data = {"$set": kwargs}
    
    # Define default stats for a new user if one doesn't exist
    on_insert_data = {
        "user_id": user_id,
        "total_pushups": 0,
        "reminder_level": 0,
        "daily_logs": {}, # Key: YYYY-MM-DD, Value: reps
        "last_reminder_check": 0 # Unix timestamp of last reminder check
    }

    USER_COLLECTION.update_one(
        {"user_id": user_id},
        {"$set": kwargs, "$setOnInsert": on_insert_data},
        upsert=True
    )

# --- MongoDB specific log_pushups (handles daily/total update atomically) ---
def log_pushups(user_id, reps, log_date):
    """Logs reps for a user on a given date and updates total."""
    if USER_COLLECTION is None:
        return

    date_str = log_date.strftime("%Y-%m-%d")
    
    # Increment total_pushups and update the specific daily log entry
    # $inc: increments a numeric field.
    # $set: sets or updates a specific field, including nested dict keys.
    
    update_result = USER_COLLECTION.update_one(
        {"user_id": user_id},
        {
            "$inc": {
                "total_pushups": reps,
                f"daily_logs.{date_str}": reps 
            },
            "$setOnInsert": {
                "user_id": user_id,
                "total_pushups": 0,
                "reminder_level": 0,
                "daily_logs": {},
                "last_reminder_check": 0
            }
        },
        upsert=True
    )

    # Need to run a second update if $setOnInsert had to create the document
    # because the initial $inc would have been applied to 0-values from $setOnInsert.
    if update_result.upserted_id:
        # Re-run update to apply the reps to the newly created document correctly
        USER_COLLECTION.update_one(
            {"user_id": user_id},
            {
                "$inc": {
                    "total_pushups": reps,
                    f"daily_logs.{date_str}": reps 
                }
            }
        )

def calculate_streak(user_stats):
    """Calculates the current consecutive daily pushup streak based on logs."""
    daily_logs = user_stats.get("daily_logs", {})
    if not daily_logs:
        return 0

    # Convert all logged dates to datetime.date objects and sort them
    # Filter for dates with > 0 reps
    logged_dates = sorted([
        datetime.strptime(d, "%Y-%m-%d").date() 
        for d, reps in daily_logs.items() if reps > 0
    ], reverse=True) # Sort newest to oldest

    if not logged_dates:
        return 0

    today = datetime.now(TZ).date()
    current_streak = 0

    # Check if the most recent log is today or yesterday
    most_recent_log = logged_dates[0]

    if most_recent_log == today:
        current_streak = 1
        expected_date = today - timedelta(days=1)
        # Start checking from the second most recent log
        check_logs = logged_dates[1:]
    elif most_recent_log == today - timedelta(days=1):
        current_streak = 1
        expected_date = today - timedelta(days=2)
        # Start checking from the most recent log (yesterday)
        check_logs = logged_dates
    else:
        # Most recent log is older than yesterday, streak is 0
        return 0

    # Iterate through past dates to extend the streak
    for log_date in check_logs:
        if log_date == expected_date:
            current_streak += 1
            expected_date -= timedelta(days=1)
        # If the logged date is older than expected, the streak is broken
        elif log_date < expected_date:
            break

    return current_streak


# ---- CONSTANTS & ENV ----
# Load LEADERBOARD_CHANNEL ID from environment variable
# The second argument (0) is a fallback if the environment variable is not set
try:
    LEADERBOARD_CHANNEL = int(os.getenv("LEADERBOARD_CHANNEL"))
except (TypeError, ValueError):
    print("WARNING: LEADERBOARD_CHANNEL environment variable is missing or invalid.")
    LEADERBOARD_CHANNEL = 0 

# Define Timezone (Using UTC as a standard for background tasks)
TZ = timezone.utc

# Reminder Levels (in minutes from midnight UTC)
# Key: Level, Value: Time object for the specific time
REMINDER_SCHEDULE = {
    1: time(hour=9, minute=0, tzinfo=TZ),  # 09:00 UTC
    2: time(hour=12, minute=0, tzinfo=TZ), # 12:00 UTC
    3: time(hour=17, minute=0, tzinfo=TZ), # 17:00 UTC
    4: time(hour=21, minute=0, tzinfo=TZ), # 21:00 UTC
}

# Reminder Status Emojis
REMINDER_EMOJIS = {
    0: "⚫", # Off
    1: "🕘", # Level 1 (9am UTC)
    2: "🕛", # Level 2 (12pm UTC)
    3: "🕔", # Level 3 (5pm UTC)
    4: "🕤", # Level 4 (9pm UTC)
}

# Setup Intents
intents = discord.Intents.default()
intents.message_content = False 
intents.dm_messages = True # Required for sending direct reminders
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------
# ---- PERSISTENT MESSAGE ID HANDLING ----
# ---------------------------------------------

def _get_setting(key):
    """Safely retrieves a single setting value (like a message ID)."""
    if SETTINGS_COLLECTION is None:
        return 0
    settings = SETTINGS_COLLECTION.find_one({"_id": "message_ids"})
    return settings.get(key, 0) if settings else 0

def _set_setting(key, value):
    """Safely sets a single setting value (like a message ID)."""
    if SETTINGS_COLLECTION is None:
        return
    SETTINGS_COLLECTION.update_one(
        {"_id": "message_ids"},
        {"$set": {key: value}},
        upsert=True
    )

def get_leaderboard_msg_id():
    return _get_setting("leaderboard_msg_id")

def set_leaderboard_msg_id(message_id):
    _set_setting("leaderboard_msg_id", message_id)

def get_quick_log_msg_id():
    return _get_setting("quick_log_msg_id")

def set_quick_log_msg_id(message_id):
    _set_setting("quick_log_msg_id", message_id)

def get_reminder_control_msg_id():
    return _get_setting("reminder_control_msg_id")

def set_reminder_control_msg_id(message_id):
    _set_setting("reminder_control_msg_id", message_id)

# ---------------------------------------------
# ---- VIEW COMPONENTS ----
# ---------------------------------------------

class RepsModal(Modal, title='Log Pushups'):
    """Modal for users to input the number of pushups."""
    reps_input = TextInput(
        label='Number of Pushups',
        placeholder='e.g., 20',
        required=True,
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            reps = int(self.reps_input.value.strip())
            if reps <= 0:
                await interaction.response.send_message(
                    "Please enter a positive number of pushups.", ephemeral=True
                )
                return

            # Log the pushups to the database
            log_pushups(interaction.user.id, reps, datetime.now(TZ).date())

            # Calculate the streak after logging
            user_stats = get_user_stats(interaction.user.id)
            streak = calculate_streak(user_stats)

            await interaction.response.send_message(
                f"💪 Logged **{reps}** pushups for today! "
                f"Your current streak is **{streak}** days.", 
                ephemeral=True
            )

            bot_instance = interaction.client 
            await update_leaderboard(bot_instance)
            await update_quick_log_message(bot_instance)

        except ValueError:
            await interaction.response.send_message(
                "Invalid input. Please enter a whole number.", ephemeral=True
            )
        except Exception as e:
            print(f"Error processing modal submission: {e}")
            await interaction.response.send_message(
                "An unexpected error occurred while logging your pushups.", ephemeral=True
            )

class PushupQuickLogView(discord.ui.View):
    """
    View with a single button to trigger the pushup log modal.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Log Pushups", 
        style=discord.ButtonStyle.primary, 
        custom_id="quick_log_button", 
        emoji="📝"
    )
    async def quick_log_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Displays the modal when the button is pressed."""
        try:
            await interaction.response.send_modal(RepsModal()) 
        except Exception as e:
            print(f"ERROR: Failed to send Modal on button click: {e}")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("An internal error occurred. Check the console.", ephemeral=True)
                else:
                    await interaction.followup.send("An internal error occurred. Check the console.", ephemeral=True)
            except Exception:
                pass 

class ReminderToggleView(discord.ui.View):
    """
    View for toggling and setting reminder levels.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="reminder_level_select",
        placeholder="Choose your reminder level...",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label="Reminders OFF", 
                value="0", 
                emoji=REMINDER_EMOJIS[0], 
                description="No daily reminders sent."
            ),
            discord.SelectOption(
                label="Level 1: 9:00 AM UTC", 
                value="1", 
                emoji=REMINDER_EMOJIS[1], 
                description="One reminder early in the day."
            ),
            discord.SelectOption(
                label="Level 2: 9 AM & 12 PM UTC", 
                value="2", 
                emoji=REMINDER_EMOJIS[2], 
                description="Two reminders."
            ),
            discord.SelectOption(
                label="Level 3: 9 AM, 12 PM, & 5 PM UTC", 
                value="3", 
                emoji=REMINDER_EMOJIS[3], 
                description="Three reminders throughout the day."
            ),
            discord.SelectOption(
                label="Level 4: 9 AM, 12 PM, 5 PM, & 9 PM UTC", 
                value="4", 
                emoji=REMINDER_EMOJIS[4], 
                description="Four reminders throughout the day."
            ),
        ]
    )
    async def reminder_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        level = int(select.values[0])
        user_id = interaction.user.id

        # Update the user's reminder level in the database
        update_user_stats(user_id, reminder_level=level)

        await interaction.response.send_message(
            f"Reminder level set to **Level {level}** {REMINDER_EMOJIS.get(level, '')}. "
            f"You will {'no longer' if level == 0 else 'now'} receive reminders.", 
            ephemeral=True
        )

# ---------------------------------------------
# ---- PERSISTENT MESSAGE MANAGEMENT ----
# ---------------------------------------------

async def setup_reminder_control_message(bot_instance: commands.Bot):
    """Sends or edits the persistent message for reminder control."""
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel: return

    msg_id = get_reminder_control_msg_id()
    view = ReminderToggleView()

    embed = discord.Embed(
        title="🔔 Daily Pushup Reminders",
        description=(
            "Use the dropdown menu below to choose how many daily reminders you want.\n"
            "Reminders are sent via Direct Message (DM) to encourage you to log your pushups."
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
        print(f"Failed to set up reminder control message: {e}")

async def update_quick_log_message(bot_instance: commands.Bot):
    """Updates the persistent message with the quick log button."""
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel: return

    msg_id = get_quick_log_msg_id()
    view = PushupQuickLogView()

    embed = discord.Embed(
        title="Quick Log Your Pushups 📝",
        description="Click the button below to quickly log your pushup reps for today!",
        color=discord.Color.green()
    )

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

    except Exception as e:
        print(f"Failed to update quick log message: {e}")

def get_leaderboard_data(all_users, time_frame="all"):
    """
    Calculates the pushup total for a given time frame (today, week, all).
    Returns a sorted list of tuples (user_stats, total_reps_for_frame).
    """
    now = datetime.now(TZ).date()

    if time_frame == "today":
        start_date = now
    elif time_frame == "week":
        start_date = now - timedelta(days=now.weekday())
    else: # "all"
        start_date = date.min 

    leaderboard_totals = []

    for user_stats in all_users:
        total_reps = 0
        for date_str, reps in user_stats.get("daily_logs", {}).items():
            try:
                log_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if log_date >= start_date:
                    total_reps += reps
            except ValueError:
                # Skip invalid date strings if any exist in the database
                continue

        if total_reps > 0:
            leaderboard_totals.append((user_stats, total_reps))

    return sorted(leaderboard_totals, key=lambda x: x[1], reverse=True)

async def format_leaderboard(bot_instance, leaderboard_data):
    """Formats the top 10 users into a string for an embed field, including streaks."""
    leaderboard_text = ""
    for i, (user_stats, total_reps) in enumerate(leaderboard_data[:10]):
        user_id = user_stats["user_id"]

        member = bot_instance.get_user(user_id) or await bot_instance.fetch_user(user_id)
        name = member.display_name if member else f"User ID: {user_id}"

        emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"#{i+1}"

        current_streak = calculate_streak(user_stats)
        streak_text = f"🔥 {current_streak} days" if current_streak > 0 else ""

        leaderboard_text += f"{emoji} **{name}**: {total_reps:,} reps {streak_text}\n"

    if not leaderboard_text:
        leaderboard_text = "No pushup logs yet! Be the first one to log some reps."

    return leaderboard_text

async def update_leaderboard(bot_instance: commands.Bot):
    """Generates the leaderboard and sends/edits the persistent message."""
    
    if MONGO_CLIENT is None: return

    all_users = get_all_users()

    all_time_data = get_leaderboard_data(all_users, "all")
    weekly_data = get_leaderboard_data(all_users, "week")
    daily_data = get_leaderboard_data(all_users, "today")

    all_time_text = await format_leaderboard(bot_instance, all_time_data)
    weekly_text = await format_leaderboard(bot_instance, weekly_data)
    daily_text = await format_leaderboard(bot_instance, daily_data)

    current_time_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S UTC")

    embed = discord.Embed(
        title="🏆 Pushup Challenge Leaderboards 🏆",
        description="Check out the top performers across different timeframes!",
        color=discord.Color.gold()
    )

    embed.add_field(name="🗓️ Today's Top 10", value=daily_text, inline=False)
    embed.add_field(name="📅 This Week's Top 10", value=weekly_text, inline=False)
    embed.add_field(name="👑 All-Time Top 10", value=all_time_text, inline=False)

    embed.set_footer(text=f"Last updated: {current_time_str} | Week starts Monday UTC")

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

    except Exception as e:
        print(f"Failed to update leaderboard: {e}")

# ---------------------------------------------
# ---- BACKGROUND TASKS & REMINDERS ----
# ---------------------------------------------

@tasks.loop(minutes=30)
async def persistent_message_task():
    """Task to periodically update the leaderboard and other persistent messages."""
    await update_leaderboard(bot)
    await update_quick_log_message(bot)
    await setup_reminder_control_message(bot)

async def send_reminders_for_level(level):
    """Checks all users and sends a reminder to those at the specified level."""
    if MONGO_CLIENT is None: return

    current_timestamp = int(datetime.now(TZ).timestamp())
    ten_minutes_ago = current_timestamp - 600

    all_users = get_all_users()

    for user_stats in all_users:
        user_id = user_stats["user_id"]
        reminder_level = user_stats.get("reminder_level", 0)
        last_reminder_check = user_stats.get("last_reminder_check", 0)

        if reminder_level >= level and last_reminder_check < ten_minutes_ago:
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)

                today_str = datetime.now(TZ).strftime("%Y-%m-%d")
                logged_today = user_stats.get("daily_logs", {}).get(today_str, 0)

                if logged_today == 0:
                    print(f"Sending reminder level {level} to user {user_id}")
                    message_content = (
                        f"🔔 Pushup Reminder (Level {level})! 🔔\n"
                        "You haven't logged any reps yet today. "
                        "Don't break that streak—get pushing! "
                        "Use the quick log button in the server or the `/log` command."
                    )

                    await user.send(message_content)

                    update_user_stats(user_id, last_reminder_check=current_timestamp)

            except discord.Forbidden:
                print(f"Cannot send DM to user {user_id}. They may have DMs disabled.")
            except Exception as e:
                print(f"Error sending reminder to user {user_id}: {e}")

# Individual tasks for each reminder level for precise timing control
@tasks.loop(time=REMINDER_SCHEDULE[1])
async def reminder_level_1():
    await send_reminders_for_level(1)

@tasks.loop(time=REMINDER_SCHEDULE[2])
async def reminder_level_2():
    await send_reminders_for_level(2)

@tasks.loop(time=REMINDER_SCHEDULE[3])
async def reminder_level_3():
    await send_reminders_for_level(3)

@tasks.loop(time=REMINDER_SCHEDULE[4])
async def reminder_level_4():
    await send_reminders_for_level(4)

# Wait until the bot is ready before starting the tasks
@persistent_message_task.before_loop
async def before_persistent_message_task():
    await bot.wait_until_ready()

@reminder_level_1.before_loop
@reminder_level_2.before_loop
@reminder_level_3.before_loop
@reminder_level_4.before_loop
async def before_reminder_tasks():
    await bot.wait_until_ready()

# ---------------------------------------------
# ---- COMMANDS ----
# ---------------------------------------------

@bot.tree.command(name="log", description="Log pushups for a specific user and date (Admin/Manual).")
@commands.has_permissions(administrator=True)
async def log_command(interaction: discord.Interaction, user: discord.Member, reps: int, log_date: str):
    """
    Logs pushups for a specific user and date.
    Date must be in YYYY-MM-DD format.
    """
    try:
        if reps <= 0:
            await interaction.response.send_message("Reps must be a positive number.", ephemeral=True)
            return

        date_obj = datetime.strptime(log_date, "%Y-%m-%d").date()
        log_pushups(user.id, reps, date_obj)

        user_stats = get_user_stats(user.id)
        streak = calculate_streak(user_stats)

        await interaction.response.send_message(
            f"✅ Logged **{reps}** pushups for **{user.display_name}** on **{log_date}**. "
            f"Current streak is **{streak}** days."
        )

        await update_leaderboard(bot)

    except ValueError:
        await interaction.response.send_message(
            "Invalid date format. Please use YYYY-MM-DD (e.g., 2023-10-27).", ephemeral=True
        )
    except Exception as e:
        print(f"Error in log command: {e}")
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)


@bot.tree.command(name="stats", description="Show your total pushup count.")
async def stats_command(interaction: discord.Interaction):
    """Displays the user's total pushup count and current streak."""
    user_stats = get_user_stats(interaction.user.id)

    total = user_stats.get("total_pushups", 0) if user_stats else 0
    streak = calculate_streak(user_stats) if user_stats else 0

    await interaction.response.send_message(
        f"You have logged a total of **{total:,}** pushups. "
        f"Your current daily streak is **{streak}** days. Keep pushing!", 
        ephemeral=True
    )

@bot.tree.command(name="editreps", description="Add or subtract reps (positive/negative number) for a specific user and date (Admin/Manual).")
@commands.has_permissions(administrator=True)
async def edit_reps_command(interaction: discord.Interaction, user: discord.Member, delta_reps: int, log_date: str):
    """
    Adjusts the total number of pushups for a user on a specific day by a delta amount. 
    """
    if USER_COLLECTION is None:
        await interaction.response.send_message("Database not connected.", ephemeral=True)
        return

    try:
        date_obj = datetime.strptime(log_date, "%Y-%m-%d").date()
        date_str = date_obj.strftime("%Y-%m-%d")

        # MongoDB update using $inc for total and $set for the daily log field
        USER_COLLECTION.update_one(
            {"user_id": user.id},
            {
                "$inc": {
                    "total_pushups": delta_reps,
                    f"daily_logs.{date_str}": delta_reps
                },
                "$setOnInsert": {
                    "user_id": user.id,
                    "total_pushups": 0,
                    "reminder_level": 0,
                    "daily_logs": {},
                    "last_reminder_check": 0
                }
            },
            upsert=True
        )
        
        # NOTE: Handling negative totals and ensuring daily reps >= 0 is more complex
        # with MongoDB's $inc and often requires application-level logic/transactions.
        # For simplicity, we trust the admin input for now.

        user_stats = get_user_stats(user.id)
        new_streak = calculate_streak(user_stats)
        new_total = user_stats.get("total_pushups", 0)
        
        # Daily reps is fetched after the update
        new_daily_reps = user_stats.get("daily_logs", {}).get(date_str, 0)

        await interaction.response.send_message(
            f"✏️ Logs for **{user.display_name}** on **{log_date}** adjusted by **{delta_reps}** reps:\n"
            f"   - New Daily Reps: {new_daily_reps:,}\n"
            f"   - New All-Time Total: {new_total:,}\n"
            f"   - New Streak: **{new_streak}** days."
        )

        await update_leaderboard(bot)

    except ValueError:
        await interaction.response.send_message(
            "Invalid date format. Please use YYYY-MM-DD (e.g., 2023-10-27).", ephemeral=True
        )
    except Exception as e:
        print(f"Error in editreps command: {e}")
        await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)

# ---------------------------------------------
# ---- BOT EVENTS ----
# ---------------------------------------------

async def check_for_missed_reminders(current_time: datetime):
    """
    Checks if the bot missed any scheduled reminder times today due to a
