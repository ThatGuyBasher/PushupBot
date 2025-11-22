import os
import asyncio
from datetime import datetime, date, timedelta, time, timezone
# --- NEW: MongoDB Imports ---
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
import discord
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput
# --- REMOVED: from replit import db
# --- REMOVED: from keep_alive import keep_alive 

# ---- CONSTANTS & ENV ----
# Load LEADERBOARD_CHANNEL ID from environment variable
try:
    LEADERBOARD_CHANNEL = int(os.getenv("LEADERBOARD_CHANNEL"))
except (TypeError, ValueError):
    print("WARNING: LEADERBOARD_CHANNEL environment variable is missing or invalid.")
    LEADERBOARD_CHANNEL = 0 

# Define Timezone (Using UTC as a standard for background tasks)
TZ = timezone.utc

# Reminder Levels (in minutes from midnight UTC)
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
# ---- MONGODB SETUP ----
# ---------------------------------------------
# Load the URI from the Render Environment variable
MONGO_URI = os.getenv("MONGO_URI") 
# Global collection references
user_data_collection = None
message_ids_collection = None 

def setup_database():
    """Initializes and verifies the MongoDB connection."""
    global user_data_collection, message_ids_collection
    if not MONGO_URI:
        print("FATAL ERROR: MONGO_URI is missing. Cannot connect to database.")
        return
        
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ping') # Verify connection
        
        # Define the database and collections
        mongo_db = mongo_client["PushupBotDB"] 
        user_data_collection = mongo_db["UserData"] # For user stats and logs
        message_ids_collection = mongo_db["MessageIDs"] # For persistent message IDs
        
        print("Successfully connected to MongoDB Atlas.")
    except ConnectionFailure as e:
        print(f"FATAL ERROR: MongoDB connection failed (Check Network Access): {e}")
    except OperationFailure as e:
        print(f"FATAL ERROR: MongoDB authentication failed (Check URI credentials): {e}")

# Call this setup function immediately
setup_database()


# ---------------------------------------------
# ---- DATABASE OPERATIONS (MONGODB) ----
# ---------------------------------------------
# Removed: def init_db():

def _get_user_data(user_id):
    """Internal function to safely retrieve user data."""
    if user_data_collection is None:
        return None # Fail gracefully if not connected
        
    # Finds the document where the _id field matches the user_id string
    document = user_data_collection.find_one({"_id": str(user_id)})
    
    # Return the 'data' field, or None if the user document is not found
    return document.get("data") if document else None

def _set_user_data(user_id, data):
    """Internal function to safely save user data."""
    if user_data_collection is None:
        return

    # Update the existing document or insert a new one
    user_data_collection.replace_one(
        {"_id": str(user_id)}, # Filter: find the document by its _id
        {"_id": str(user_id), "data": data}, # The document to insert/replace
        upsert=True # Insert a new one if no matching document is found
    )

def get_user_stats(user_id):
    """Retrieves all stats for a user."""
    return _get_user_data(user_id)

def get_all_users():
    """Returns a list of all user dictionaries."""
    if user_data_collection is None:
        return []
            
    # Fetch all documents and extract the 'data' field from each
    all_data = [doc.get("data") for doc in user_data_collection.find() if doc.get("data")]
    return all_data

# The rest of the functions (calculate_streak, log_pushups, update_user_stats)
# remain the same as they call _get_user_data and _set_user_data, which are updated.

# ---- MESSAGE ID HANDLING (MONGODB) ----
def _get_msg_id_helper(key_name):
    """Reads a stored message ID for a persistent message."""
    if message_ids_collection is None:
        return 0
        
    # Find the specific key by its unique name
    document = message_ids_collection.find_one({"_id": key_name})
    # Return the stored ID or 0 if not found
    return document.get("message_id", 0) if document else 0

def _set_msg_id_helper(key_name, message_id):
    """Stores a message ID for a persistent message."""
    if message_ids_collection is None:
        return
        
    # Update or insert the document for the specific key
    message_ids_collection.replace_one(
        {"_id": key_name},
        {"_id": key_name, "message_id": message_id},
        upsert=True
    )

def get_leaderboard_msg_id():
    return _get_msg_id_helper("leaderboard_msg_id")

def set_leaderboard_msg_id(message_id):
    _set_msg_id_helper("leaderboard_msg_id", message_id)

def get_quick_log_msg_id():
    return _get_msg_id_helper("quick_log_msg_id")

def set_quick_log_msg_id(message_id):
    _set_msg_id_helper("quick_log_msg_id", message_id)

def get_reminder_control_msg_id():
    return _get_msg_id_helper("reminder_control_msg_id")

def set_reminder_control_msg_id(message_id):
    _set_msg_id_helper("reminder_control_msg_id", message_id)


# ---------------------------------------------
# ---- VIEW COMPONENTS (No change needed) ----
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
            # We use the date from when the pushups were logged (now, in UTC)
            log_pushups(interaction.user.id, reps, datetime.now(TZ).date())

            # Calculate the streak after logging
            user_stats = get_user_stats(interaction.user.id)
            streak = calculate_streak(user_stats)

            await interaction.response.send_message(
                f"💪 Logged **{reps}** pushups for today! "
                f"Your current streak is **{streak}** days.", 
                ephemeral=True
            )

            # Access bot instance using interaction.client
            bot_instance = interaction.client 
            # After logging, update the leaderboard and quick log message
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
                # Use interaction.followup.send() if interaction.response was already used 
                if not interaction.response.is_done():
                    await interaction.response.send_message("An internal error occurred. Check the Replit console.", ephemeral=True)
                else:
                    await interaction.followup.send("An internal error occurred. Check the Replit console.", ephemeral=True)
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
# ---- PERSISTENT MESSAGE MANAGEMENT (Unchanged) ----
# ---------------------------------------------

async def setup_reminder_control_message(bot_instance: commands.Bot):
    """Sends or edits the persistent message for reminder control."""
    # print(f"Setting up Reminder Control Message in Channel ID: {LEADERBOARD_CHANNEL}")
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel:
        # print(f"Error: Reminder control channel ID {LEADERBOARD_CHANNEL} not found.")
        return
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
        # Try to fetch and edit the existing message
        if msg_id != 0:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        # If no message ID is stored, send a new message
        else:
            msg = await channel.send(embed=embed, view=view)
            set_reminder_control_msg_id(msg.id)
    except discord.NotFound:
        # If the message was deleted, send a new one
        msg = await channel.send(embed=embed, view=view)
        set_reminder_control_msg_id(msg.id)
    except Exception as e:
        print(f"Failed to set up reminder control message: {e}")

async def update_quick_log_message(bot_instance: commands.Bot):
    """Updates the persistent message with the quick log button."""
    # print(f"Setting up Quick Log Message in Channel ID: {LEADERBOARD_CHANNEL}")
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel:
        # print(f"Error: Quick log channel ID {LEADERBOARD_CHANNEL} not found.")
        return
    msg_id = get_quick_log_msg_id()
    view = PushupQuickLogView()
    embed = discord.Embed(
        title="Quick Log Your Pushups 📝",
        description="Click the button below to quickly log your pushup reps for today!",
        color=discord.Color.green()
    )
    try:
        # Try to fetch and edit the existing message
        if msg_id != 0:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        # If no message ID is stored, send a new message
        else:
            msg = await channel.send(embed=embed, view=view)
            set_quick_log_msg_id(msg.id)
    except discord.NotFound:
        # If the message was deleted, send a new one
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
        # Start of the week is Monday (0)
        start_date = now - timedelta(days=now.weekday())
    else: # "all"
        start_date = date.min # Effectively from the beginning
    leaderboard_totals = []
    for user_stats in all_users:
        total_reps = 0
        for date_str, reps in user_stats.get("daily_logs", {}).items():
            log_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if log_date >= start_date:
                total_reps += reps
        # Only include users who logged reps in this period
        if total_reps > 0:
            leaderboard_totals.append((user_stats, total_reps))
    # Sort by total reps descending
    return sorted(leaderboard_totals, key=lambda x: x[1], reverse=True)

async def format_leaderboard(bot_instance, leaderboard_data):
    """Formats the top 10 users into a string for an embed field, including streaks."""
    leaderboard_text = ""
    for i, (user_stats, total_reps) in enumerate(leaderboard_data[:10]): # Top 10
        user_id = user_stats["user_id"]
        # Fetch user object (try local cache first, then API)
        member = bot_instance.get_user(user_id) or await bot_instance.fetch_user(user_id)
        name = member.display_name if member else f"User ID: {user_id}"
        emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"#{i+1}"
        # Calculate and display the streak only for All-Time or Current data
        current_streak = calculate_streak(user_stats)
        streak_text = f"🔥 {current_streak} days" if current_streak > 0 else ""
        # Format the line: Emoji Name: Reps Streak
        leaderboard_text += f"{emoji} **{name}**: {total_reps:,} reps {streak_text}\n"
    if not leaderboard_text:
        leaderboard_text = "No pushup logs yet! Be the first one to log some reps."
    return leaderboard_text

async def update_leaderboard(bot_instance: commands.Bot):
    """Generates the leaderboard and sends/edits the persistent message."""
    # print(f"Updating Leaderboard in Channel ID: {LEADERBOARD_CHANNEL}")
    # 1. Get and process data for different timeframes
    all_users = get_all_users()
    # Calculate Leaderboard Data for each view
    all_time_data = get_leaderboard_data(all_users, "all")
    weekly_data = get_leaderboard_data(all_users, "week")
    daily_data = get_leaderboard_data(all_users, "today")
    # Format the data into strings
    all_time_text = await format_leaderboard(bot_instance, all_time_data)
    weekly_text = await format_leaderboard(bot_instance, weekly_data)
    daily_text = await format_leaderboard(bot_instance, daily_data)
    # 2. Create the embed
    current_time_str = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S UTC")
    embed = discord.Embed(
        title="🏆 Pushup Challenge Leaderboards 🏆",
        description="Check out the top performers across different timeframes!",
        color=discord.Color.gold()
    )
    # Add fields for each leaderboard (Daily, Weekly, All-Time)
    embed.add_field(
        name="🗓️ Today's Top 10", 
        value=daily_text, 
        inline=False
    )
    embed.add_field(
        name="📅 This Week's Top 10", 
        value=weekly_text, 
        inline=False
    )
    embed.add_field(
        name="👑 All-Time Top 10", 
        value=all_time_text, 
        inline=False
    )
    embed.set_footer(text=f"Last updated: {current_time_str} | Week starts Monday UTC")
    # 3. Find and update the message
    channel = bot_instance.get_channel(LEADERBOARD_CHANNEL)
    if not channel:
        # print(f"Error: Leaderboard channel ID {LEADERBOARD_CHANNEL} not found.")
        return
    msg_id = get_leaderboard_msg_id()
    try:
        if msg_id != 0:
            # Edit existing message
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed)
        else:
            # Send new message and store ID
            msg = await channel.send(embed=embed)
            set_leaderboard_msg_id(msg.id)
    except discord.NotFound:
        # print("Leaderboard message not found. Sending new one...")
        msg = await channel.send(embed=embed)
        set_leaderboard_msg_id(msg.id)
    except Exception as e:
        print(f"Failed to update leaderboard: {e}")

# ---------------------------------------------
# ---- BACKGROUND TASKS & REMINDERS (Unchanged) ----
# ---------------------------------------------
@tasks.loop(minutes=30)
async def persistent_message_task():
    """Task to periodically update the leaderboard and other persistent messages."""
    await update_leaderboard(bot)
    await update_quick_log_message(bot)
    await setup_reminder_control_message(bot)

async def send_reminders_for_level(level):
    """Checks all users and sends a reminder to those at the specified level."""
    current_timestamp = int(datetime.now(TZ).timestamp())
    # We allow a buffer for the last check to be up to 10 minutes ago
    ten_minutes_ago = current_timestamp - 600
    # Get a fresh list of users
    all_users = get_all_users()
    for user_stats in all_users:
        user_id = user_stats["user_id"]
        reminder_level = user_stats["reminder_level"]
        last_reminder_check = user_stats.get("last_reminder_check", 0)

        # Check if the user is at the correct level AND has not been reminded recently
        if reminder_level >= level and last_reminder_check < ten_minutes_ago:
            try:
                user = bot.get_user(user_id) or await bot.fetch_user(user_id)

                # Check if user has logged pushups today (UTC date)
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
                    # Send message
                    await user.send(message_content)
                    # Update last_reminder_check to prevent repeat DMs
                    update_user_stats(user_id, last_reminder_check=current_timestamp)
                # else:
                #     print(f"Skipping reminder for user {user_id}: logged {logged_today} today.")
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
# ---- COMMANDS (Unchanged) ----
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

        # Log the pushups using the new DB function
        log_pushups(user.id, reps, date_obj)

        # Calculate the streak after logging (for the response)
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
    Use positive numbers to add and negative numbers to subtract.
    """
    try:
        # delta_reps can be positive or negative
        date_obj = datetime.strptime(log_date, "%Y-%m-%d").date()
        date_str = date_obj.strftime("%Y-%m-%d")

        user_stats = get_user_stats(user.id)

        # Initialize stats if user is new (to allow editing/adding logs)
        if not user_stats:
            user_stats = {
                "user_id": user.id,
                "total_pushups": 0,
                "reminder_level": 0,
                "daily_logs": {},
                "last_reminder_check": 0
            }

        daily_logs = user_stats["daily_logs"]
        current_reps = daily_logs.get(date_str, 0)

        # 1. Calculate the new daily total, ensuring it doesn't go below 0
        new_daily_reps = max(0, current_reps + delta_reps)

        # 2. The delta applied to the all-time total is the difference between the new and old daily totals.
        # This correctly handles cases where the subtraction was limited by the max(0, ...) check.
        total_delta = new_daily_reps - current_reps

        # 3. Update the all-time total (ensuring it also doesn't go below 0 globally)
        user_stats["total_pushups"] = max(0, user_stats["total_pushups"] + total_delta)

        # 4. Update the specific daily log
        if new_daily_reps == 0:
             if date_str in daily_logs:
                 # If new reps is 0, remove the entry from daily logs
                 del daily_logs[date_str]
        else:
            # If new reps > 0, set the new value
            daily_logs[date_str] = new_daily_reps

        # Save the updated stats
        _set_user_data(user.id, user_stats)

        # Recalculate streak for the confirmation message
        new_streak = calculate_streak(user_stats)

        await interaction.response.send_message(
            f"✏️ Logs for **{user.display_name}** on **{log_date}** adjusted by **{delta_reps}** reps:\n"
            f"   - Old Daily Reps: {current_reps:,}\n"
            f"   - New Daily Reps: {new_daily_reps:,}\n"
            f"   - Total Change to All-Time: {total_delta:,}\n"
            f"   - New All-Time Total: {user_stats['total_pushups']:,}\n"
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
# ---- BOT EVENTS (Unchanged) ----
# ---------------------------------------------

async def check_for_missed_reminders(current_time: datetime):
    # ... (function body remains the same)
    pass # Function implementation removed for brevity

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Removed: init_db() call

    # Sync commands globally 
    try:
        synced = await bot.tree.sync()
        print(f"Commands synced globally: {[cmd.name for cmd in synced]}")
        for guild in bot.guilds:
            try:
                await bot.tree.sync(guild=guild)
            except Exception as e:
                print(f"Failed to sync to {guild.name}: {e}")
    except Exception as e:
        print("Failed to sync commands:", e)

    # Register the persistent views before starting tasks
    bot.add_view(PushupQuickLogView())
    bot.add_view(ReminderToggleView()) 

    # Check for and run any missed reminders immediately upon connection
    await check_for_missed_reminders(datetime.now(TZ))

    # Start the tasks
    if not persistent_message_task.is_running():
        persistent_message_task.start()

    # Start reminder tasks
    if not reminder_level_1.is_running():
        reminder_level_1.start()
    if not reminder_level_2.is_running():
        reminder_level_2.start()
    if not reminder_level_3.is_running():
        reminder_level_3.start()
    if not reminder_level_4.is_running():
        reminder_level_4.start()

# ---------------------------------------------
# ---- RUN BOT (Simplified for Render) ----
# ---------------------------------------------

# Run the bot using the token from the Render environment variables
bot.run(os.getenv("DISCORD_BOT_TOKEN"))