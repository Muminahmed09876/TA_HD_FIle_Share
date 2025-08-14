import os
import asyncio
import logging
from threading import Thread
from flask import Flask
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Env validation helpers ---
def get_env(key, required=False):
    v = os.environ.get(key)
    if required and not v:
        logger.error("Missing required environment variable: %s", key)
        raise RuntimeError(f"Missing required environment variable: {key}")
    return v

def get_env_int(key, required=False):
    v = get_env(key, required=required)
    if v is None:
        return None
    try:
        return int(v)
    except:
        logger.error("Environment variable %s must be an integer. Got: %s", key, v)
        raise

# --- Bot Configuration (Using Environment Variables) ---
API_ID = get_env_int("API_ID", required=True)
API_HASH = get_env("API_HASH", required=True)
BOT_TOKEN = get_env("BOT_TOKEN", required=True)
ADMIN_ID = get_env_int("ADMIN_ID", required=True)
CHANNEL_ID = get_env_int("CHANNEL_ID", required=True)
LOG_CHANNEL_ID = get_env_int("LOG_CHANNEL_ID", required=True)

# --- MongoDB Configuration ---
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "TA_File_Share_Bot")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "bot_data")

# --- Global Data Variables ---
filters_dict = {}
user_list = set()
banned_users = set()
join_channels = []
restrict_status = False
autodelete_filters = {}
user_states = {}
last_filter = None

# --- MongoDB Client (optional) ---
mongo_client = None
collection = None
if MONGODB_URI:
    try:
        mongo_client = AsyncIOMotorClient(MONGODB_URI)
        db = mongo_client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        logger.info("MongoDB client initialized.")
    except Exception as e:
        logger.exception("Failed to initialize MongoDB client: %s", e)

# --- Data Management Functions ---
async def save_data():
    if not collection:
        logger.debug("No MongoDB configured, skipping save_data.")
        return
    data = {
        "_id": "bot_state",
        "filters_dict": filters_dict,
        "user_list": list(user_list),
        "banned_users": list(banned_users),
        "join_channels": join_channels,
        "restrict_status": restrict_status,
        "autodelete_filters": autodelete_filters,
        "user_states": user_states
    }
    try:
        await collection.update_one({"_id": "bot_state"}, {"$set": data}, upsert=True)
        logger.info("Data saved to MongoDB successfully.")
    except Exception:
        logger.exception("save_data failed")

async def load_data():
    global filters_dict, user_list, banned_users, join_channels, restrict_status, autodelete_filters, user_states
    if not collection:
        logger.debug("No MongoDB configured, skipping load_data.")
        return
    try:
        data = await collection.find_one({"_id": "bot_state"})
        if data:
            filters_dict = data.get("filters_dict", {})
            user_list = set(data.get("user_list", []))
            banned_users = set(data.get("banned_users", []))
            join_channels = data.get("join_channels", [])
            restrict_status = data.get("restrict_status", False)
            autodelete_filters = data.get("autodelete_filters", {})
            user_states = data.get("user_states", {})
            logger.info("Data loaded from MongoDB successfully.")
        else:
            logger.info("No saved bot state found in MongoDB.")
    except Exception:
        logger.exception("load_data failed")

def save_last_filter(keyword):
    try:
        with open("last_filter.txt", "w", encoding="utf-8") as f:
            f.write(keyword or "")
    except Exception:
        logger.exception("Failed to write last_filter.txt")

def load_last_filter():
    global last_filter
    try:
        if os.path.exists("last_filter.txt"):
            with open("last_filter.txt", "r", encoding="utf-8") as f:
                last = f.read().strip()
                last_filter = last or None
                logger.info("Loaded last_filter: %s", last_filter)
        else:
            logger.info("No last_filter.txt found.")
    except Exception:
        logger.exception("Failed to load last_filter.txt")

# --- Flask App for Ping Service ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    return "<h1>Bot is Running!</h1><p>The TA File Share Bot is online and working properly.</p>"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting Flask on port %s", port)
    flask_app.run(host='0.0.0.0', port=port)

# --- Helper Functions ---
def is_user_banned(user_id):
    return user_id in banned_users

async def ban_user(user_id):
    banned_users.add(user_id)
    await save_data()

async def unban_user(user_id):
    banned_users.discard(user_id)
    await save_data()

def get_join_channels():
    return join_channels

async def add_join_channel(name, link, channel_id):
    join_channels.append({"name": name, "link": link, "id": channel_id})
    await save_data()

async def delete_join_channel(identifier):
    global join_channels
    original_count = len(join_channels)
    try:
        channel_id = int(identifier)
        join_channels = [c for c in join_channels if c['id'] != channel_id]
    except ValueError:
        join_channels = [c for c in join_channels if c['link'] != identifier]
    if len(join_channels) < original_count:
        await save_data()
        return True
    return False

async def is_user_member(client, user_id):
    required_channels = get_join_channels()
    if not required_channels:
        return True
    for channel in required_channels:
        try:
            member = await client.get_chat_member(chat_id=channel['id'], user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except UserNotParticipant:
            return False
        except Exception:
            logger.exception("Error checking membership for user %s in channel %s", user_id, channel.get('link'))
            return False
    return True

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
    except Exception:
        logger.exception("Failed to delete messages")

# --- Pyrogram Client ---
app = Client(
    "ta_file_share_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Debug helper: log every incoming message (temporary) ---
@app.on_message(filters.all)
async def _debug_log_every_message(client, message):
    try:
        from_user = getattr(message, "from_user", None)
        user_id = from_user.id if from_user else None
        logger.info("INCOMING message: chat=%s from_user=%s text=%s", message.chat.id if message.chat else None, user_id, getattr(message, "text", "") or message.media)
    except Exception:
        logger.exception("Failed to log incoming message")

# --- Your existing handlers (only start handler shown here for brevity; keep all other handlers as-is) ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        return await message.reply_text("❌ **You are banned from using this bot.**")

    if user_id not in user_list:
        user_list.add(user_id)
        await save_data()
        user = message.from_user
        user_full_name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
        log_message = (
            f"➡️ **New User**\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Full Name: `{user_full_name}`"
        )
        if user.username:
            log_message += f"\n🔗 Username: @{user.username}"
        try:
            await client.send_message(LOG_CHANNEL_ID, log_message, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logger.exception("Failed to send log message to LOG_CHANNEL_ID")

    args = message.text.split(maxsplit=1)
    deep_link_keyword = args[1].lower() if len(args) > 1 else None

    if deep_link_keyword:
        # ... keep the rest of your logic exactly as before ...
        await message.reply_text("Debug: deep link received. (logic continues)")
        return

    if user_id == ADMIN_ID:
        await message.reply_text("Welcome Admin (debug).")
    else:
        await message.reply_text("Welcome user (debug).")

# --- (Add other handlers here: channel_text_handler, channel_media_handler, delete handlers, admin commands, etc.)
# For brevity include the rest of your handlers from your original file unchanged.

# --- Bot start up ---
async def main():
    await load_data()
    load_last_filter()
    logger.info("Starting Flask background thread...")
    Thread(target=run_flask, daemon=True).start()
    logger.info("Starting Pyrogram client...")
    await app.start()
    logger.info("Bot started. Entering idle state.")
    await idle()
    logger.info("Stopping bot...")
    await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Fatal error in main")
