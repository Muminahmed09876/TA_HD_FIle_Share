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

# --- Env helpers ---
def get_env(key: str, required: bool = False) -> str | None:
    v = os.environ.get(key)
    if required and not v:
        logger.error("Missing required environment variable: %s", key)
        raise RuntimeError(f"Missing required environment variable: {key}")
    return v

def get_env_int(key: str, required: bool = False) -> int | None:
    v = get_env(key, required=required)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
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
# Prefer setting MONGODB_URI as an environment variable for security.
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "TA_File_Share_Bot")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "bot_data")

# --- Global Data Variables ---
filters_dict: dict = {}
user_list: set = set()
banned_users: set = set()
join_channels: list = []
restrict_status: bool = False
autodelete_filters: dict = {}
user_states: dict = {}
last_filter: str | None = None

# --- MongoDB Client (optional) ---
mongo_client = None
collection = None
if MONGODB_URI:
    try:
        mongo_client = AsyncIOMotorClient(MONGODB_URI)
        db = mongo_client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        logger.info("MongoDB client initialized.")
    except Exception:
        logger.exception("Failed to initialize MongoDB client")

# --- Data Management Functions ---
async def save_data():
    """Saves all bot data to MongoDB if configured."""
    if collection is None:
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
    """Loads all bot data from MongoDB if configured."""
    global filters_dict, user_list, banned_users, join_channels, restrict_status, autodelete_filters, user_states
    if collection is None:
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
            logger.info("No data found in MongoDB. Starting with empty data.")
    except Exception:
        logger.exception("load_data failed")

def save_last_filter(keyword: str | None):
    """Saves the last active filter keyword to a file."""
    try:
        with open("last_filter.txt", "w", encoding="utf-8") as f:
            f.write(keyword or "")
    except Exception:
        logger.exception("Failed to save last_filter.txt")

def load_last_filter():
    """Loads the last active filter from a file."""
    global last_filter
    try:
        if os.path.exists("last_filter.txt"):
            with open("last_filter.txt", "r", encoding="utf-8") as f:
                last = f.read().strip()
                last_filter = last or None
                logger.info("Last filter '%s' loaded.", last_filter)
        else:
            logger.info("No last filter file found.")
    except Exception:
        logger.exception("Failed to load last_filter.txt")

# --- Flask App for Ping Service ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    """A simple status page for the bot."""
    return "<h1>Bot is Running!</h1><p>The TA File Share Bot is online and working properly.</p><p>Keep sharing!</p>"

def run_flask():
    """Runs the Flask app in a separate thread."""
    try:
        port = int(os.environ.get("PORT", 5000))
    except Exception:
        port = 5000
    logger.info("Starting Flask on port %s", port)
    flask_app.run(host='0.0.0.0', port=port)

# --- Helper Functions ---
def is_user_banned(user_id: int) -> bool:
    """Checks if a user is banned."""
    return user_id in banned_users

async def ban_user(user_id: int):
    """Bans a user."""
    banned_users.add(user_id)
    await save_data()

async def unban_user(user_id: int):
    """Unbans a user."""
    banned_users.discard(user_id)
    await save_data()

def get_join_channels() -> list:
    """Gets all required join channels."""
    return join_channels

async def add_join_channel(name: str, link: str, channel_id: int):
    """Adds a required join channel."""
    join_channels.append({"name": name, "link": link, "id": channel_id})
    await save_data()

async def delete_join_channel(identifier: str) -> bool:
    """Deletes a join channel by link or ID."""
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

async def is_user_member(client: Client, user_id: int) -> bool:
    """Checks if a user is a member of all required channels."""
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
            logger.exception("Error checking user %s in channel %s", user_id, channel.get('link'))
            return False
    return True

async def delete_messages_later(chat_id: int, message_ids: list, delay_seconds: int):
    """Schedules the deletion of messages after a delay."""
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

# --- Debug helper: log every incoming message (temporary; remove for production) ---
@app.on_message(filters.all)
async def _debug_log_every_message(client: Client, message):
    try:
        from_user = getattr(message, "from_user", None)
        user_id = from_user.id if from_user else None
        chat_id = message.chat.id if message.chat else None
        text = getattr(message, "text", None)
        logger.info("INCOMING message: chat=%s from_user=%s text=%s media=%s", chat_id, user_id, text, bool(message.media))
    except Exception:
        logger.exception("Failed to log incoming message")

# --- Message Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message):
    user = message.from_user
    user_id = user.id if user else None
    if user_id is None:
        return

    if is_user_banned(user_id):
        return await message.reply_text("❌ **You are banned from using this bot.**")

    if user_id not in user_list:
        user_list.add(user_id)
        await save_data()

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

    args = message.text.split(maxsplit=1) if message.text else []
    deep_link_keyword = args[1].lower() if len(args) > 1 else None

    if deep_link_keyword:
        if restrict_status and not await is_user_member(client, user_id):
            buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
            buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
            keyboard = InlineKeyboardMarkup(buttons)
            return await message.reply_text(
                "❌ **You must join the following channels to use this bot.**",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        filter_data = filters_dict.get(deep_link_keyword)
        if filter_data:
            user_full_name = (user.first_name or "") + (" " + user.last_name if user.last_name else "")
            log_link_message = (
                f"🔍 **Deep Link Clicked**\n"
                f"🆔 User ID: `{user_id}`\n"
                f"👤 Full Name: `{user_full_name}`\n"
                f"🔑 Keyword: `{deep_link_keyword}`"
            )
            try:
                await client.send_message(LOG_CHANNEL_ID, log_link_message, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                logger.exception("Failed to log deep link activity")

            delete_time = autodelete_filters.get(deep_link_keyword, 0)
            if delete_time > 0:
                await message.reply_text(
                    f"✅ **ফাইল পাওয়া গেছে!** এই ফাইলগুলো স্বয়ংক্রিয়ভাবে {int(delete_time / 60)} মিনিটের মধ্যে মুছে যাবে।",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await message.reply_text(
                    f"✅ **ফাইল পাওয়া গেছে!** ফাইল পাঠানো হচ্ছে...",
                    parse_mode=ParseMode.MARKDOWN
                )

            sent_message_ids = []
            for file_id in filter_data:
                try:
                    if isinstance(file_id, int):
                        sent_msg = await app.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=int(CHANNEL_ID),
                            message_id=file_id,
                            protect_content=restrict_status
                        )
                        sent_message_ids.append(sent_msg.id)
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("Invalid file_id found: %s", file_id)
                except FloodWait as e:
                    logger.info("FloodWait %s seconds, sleeping...", e.value)
                    await asyncio.sleep(e.value)
                    try:
                        if isinstance(file_id, int):
                            sent_msg = await app.copy_message(
                                chat_id=message.chat.id,
                                from_chat_id=int(CHANNEL_ID),
                                message_id=file_id,
                                protect_content=restrict_status
                            )
                            sent_message_ids.append(sent_msg.id)
                    except Exception:
                        logger.exception("Error copying message after floodwait")
                except Exception:
                    logger.exception("Error copying message %s", file_id)

            await message.reply_text("🎉 **সকল ফাইল পাঠানো সম্পন্ন হয়েছে!** আশা করি আপনি যা খুঁজছিলেন তা পেয়েছেন।")

            if delete_time > 0 and sent_message_ids:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, delete_time))
        else:
            await message.reply_text("❌ **এই কিওয়ার্ডের জন্য কোনো ফাইল খুঁজে পাওয়া যায়নি।**")
        return

    # No deep link, normal start message:
    if user_id == ADMIN_ID:
        await message.reply_text(
            "🌟 **Welcome, Admin!** 🌟\n\n"
            "This bot is your personal file-sharing hub.\n\n"
            "**Channel Workflow:**\n"
            "📂 **Create Filter**: Send a single-word message in the channel (e.g., `#python`).\n"
            "💾 **Add Files**: Any media sent after that will be added to the active filter.\n"
            "🗑️ **Delete Filter**: Delete the original single-word message to remove the filter.\n\n"
            "**Commands:**\n"
            "• `/broadcast` to send a message to all users.\n"
            "• `/delete <keyword>` to remove a filter and its files.\n"
            "• `/ban <user_id>` to ban a user.\n"
            "• `/unban <user_id>` to unban a user.\n"
            "• `/add_channel` to add a required join channel.\n"
            "• `/delete_channel <link or id>` to delete a channel from join list.\n"
            "• `/restrict` to toggle the channel join requirement.\n"
            "• `/auto_delete <keyword> <time>` to set auto-delete time for a specific filter.\n"
            "• `/channel_id` to get a channel ID by forwarding a message to the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(
            "👋 **Welcome!**\n\n"
            "This bot is a file-sharing service. You can access files "
            "by using a special link provided by the admin.\n\n"
            "Have a great day!",
            parse_mode=ParseMode.MARKDOWN
        )

# --- Handler for channel messages (Filter Management) ---
@app.on_message(filters.channel & filters.text & filters.chat(CHANNEL_ID))
async def channel_text_handler(client: Client, message):
    global last_filter
    text = message.text
    if message.from_user and message.from_user.id == ADMIN_ID and text and len(text.split()) == 1:
        keyword = text.lower().replace('#', '')
        if not keyword:
            return

        last_filter = keyword
        save_last_filter(keyword)

        if keyword not in filters_dict:
            filters_dict[keyword] = []
            await save_data()
            try:
                me = await app.get_me()
                username = me.username if me and getattr(me, "username", None) else "bot"
            except Exception:
                username = "bot"
            await app.send_message(
                ADMIN_ID,
                f"✅ **New filter created!**\n"
                f"🔗 Share link: `https://t.me/{username}?start={keyword}`\n\n"
                "Any media you send now will be added to this filter.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await app.send_message(
                ADMIN_ID,
                f"⚠️ **Filter '{keyword}' is already active.** All new files will be added to it.",
                parse_mode=ParseMode.MARKDOWN
            )

# --- Handler for new media in the channel ---
@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client: Client, message):
    if message.from_user and message.from_user.id == ADMIN_ID and last_filter:
        keyword = last_filter

        # Ensure the filter exists before adding a message
        if keyword not in filters_dict:
            filters_dict[keyword] = []

        filters_dict[keyword].append(message.id)
        await save_data()
        await app.send_message(
            ADMIN_ID,
            f"✅ **ফাইল '{last_filter}' ফিল্টারে যোগ করা হয়েছে।**"
        )
    else:
        if message.from_user and message.from_user.id == ADMIN_ID:
            await app.send_message(
                ADMIN_ID,
                f"⚠️ **No active filter found.** Please create a new filter with a single-word message (e.g., `#newfilter`) in the channel.",
                parse_mode=ParseMode.MARKDOWN
            )

# --- Handler for message deletion in the channel (to delete filters) ---
@app.on_deleted_messages(filters.channel & filters.chat(CHANNEL_ID))
async def channel_delete_handler(client: Client, messages):
    global last_filter
    for message in messages:
        if getattr(message, "text", None) and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if keyword in filters_dict:
                del filters_dict[keyword]
                if keyword in autodelete_filters:
                    del autodelete_filters[keyword]
                await save_data()
                await app.send_message(
                    ADMIN_ID,
                    f"🗑️ **Filter '{keyword}' has been deleted** because the original message was removed from the channel.",
                    parse_mode=ParseMode.MARKDOWN
                )

            if last_filter == keyword:
                last_filter = None
                save_last_filter(None)
                await app.send_message(
                    ADMIN_ID,
                    "📝 **Note:** The last active filter has been cleared because the filter message was deleted."
                )

# --- Other Handlers (Admin Commands) ---
@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client: Client, message):
    if not message.reply_to_message:
        return await message.reply_text("📌 **Reply to a message** with `/broadcast` to send it to all users.")

    sent_count = 0
    failed_count = 0
    user_list_copy = list(user_list)
    total_users = len(user_list_copy)

    if total_users == 0:
        return await message.reply_text("❌ **No users found in the database.**")

    progress_msg = await message.reply_text(f"📢 **Broadcasting to {total_users} users...** (0/{total_users})")

    for user_id in user_list_copy:
        try:
            if is_user_banned(user_id):
                continue
            await message.reply_to_message.copy(user_id, protect_content=True)
            sent_count += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await message.reply_to_message.copy(user_id, protect_content=True)
                sent_count += 1
            except Exception:
                logger.exception("Failed to resend broadcast after floodwait to %s", user_id)
                failed_count += 1
        except Exception:
            logger.exception("Failed to send broadcast to user %s", user_id)
            failed_count += 1

        if (sent_count + failed_count) % 10 == 0 and sent_count + failed_count > 0:
            try:
                await progress_msg.edit_text(
                    f"📢 **Broadcasting...**\n"
                    f"✅ Sent: {sent_count}\n"
                    f"❌ Failed: {failed_count}\n"
                    f"Total: {total_users}"
                )
            except MessageNotModified:
                pass
            except Exception:
                logger.exception("Error updating progress message")

        await asyncio.sleep(0.1)

    try:
        await progress_msg.edit_text(
            f"✅ **Broadcast complete!**\n"
            f"Sent to {sent_count} users.\n"
            f"Failed to send to {failed_count} users."
        )
    except Exception:
        await message.reply_text(
            f"✅ **Broadcast complete!**\n"
            f"Sent to {sent_count} users.\n"
            f"Failed to send to {failed_count} users."
        )

@app.on_message(filters.command("delete") & filters.private & filters.user(ADMIN_ID))
async def delete_cmd(client: Client, message):
    global last_filter
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Please provide a keyword to delete.**\nExample: `/delete python`")

    keyword = args[1].lower()
    if keyword in filters_dict:
        del filters_dict[keyword]
        if keyword in autodelete_filters:
            del autodelete_filters[keyword]
        if last_filter == keyword:
            last_filter = None
            save_last_filter(None)

        await save_data()
        await message.reply_text(
            f"🗑️ **Filter '{keyword}' and its associated files have been deleted.**",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(f"❌ **Filter '{keyword}' not found.**")

@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command(["add_channel", "delete_channel", "start", "broadcast", "delete", "ban", "unban", "restrict", "auto_delete", "channel_id"]))
async def handle_conversational_input(client: Client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        state = user_states[user_id]

        if state["command"] == "channel_id_awaiting_message":
            if getattr(message, "forward_from_chat", None):
                chat_id = message.forward_from_chat.id
                chat_type = message.forward_from_chat.type
                chat_title = message.forward_from_chat.title if message.forward_from_chat.title else "N/A"

                response = (
                    f"✅ **সফলভাবে চ্যানেল আইডি পাওয়া গেছে!**\n\n"
                    f"🆔 **Chat ID:** `{chat_id}`\n"
                    f"📝 **Chat Type:** `{chat_type}`\n"
                    f"🔖 **Chat Title:** `{chat_title}`"
                )
                await message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
            else:
                await message.reply_text(
                    "❌ **ভুল মেসেজ!**\n\n"
                    "অনুগ্রহ করে একটি চ্যানেল থেকে **সরাসরি ফরওয়ার্ড করা** মেসেজ পাঠান।"
                )
            del user_states[user_id]
            return

        if state["command"] == "add_channel":
            if state["step"] == "awaiting_name":
                user_states[user_id]["channel_name"] = message.text
                user_states[user_id]["step"] = "awaiting_link"
                await message.reply_text("🔗 **এবার চ্যানেলের লিংক দিন।** (যেমন: `https://t.me/channel` অথবা `t.me/channel`)")
            elif state["step"] == "awaiting_link":
                channel_link = message.text
                if not (channel_link.startswith('https://t.me/') or channel_link.startswith('t.me/')):
                    del user_states[user_id]
                    await message.reply_text("❌ **ভুল লিংক ফরম্যাট।** `/add_channel` দিয়ে আবার চেষ্টা করুন।")
                    return
                user_states[user_id]["channel_link"] = channel_link
                user_states[user_id]["step"] = "awaiting_id"
                await message.reply_text("🆔 **এবার চ্যানেলের আইডি দিন।** (যেমন: `-100123456789`)")
            elif state["step"] == "awaiting_id":
                try:
                    channel_id = int(message.text)
                    channel_name = user_states[user_id]["channel_name"]
                    channel_link = user_states[user_id]["channel_link"]

                    await add_join_channel(channel_name, channel_link, channel_id)
                    del user_states[user_id]
                    await message.reply_text(f"✅ **চ্যানেল '{channel_name}' সফলভাবে যুক্ত হয়েছে!**")
                except ValueError:
                    del user_states[user_id]
                    await message.reply_text("❌ **ভুল আইডি ফরম্যাট।** অনুগ্রহ করে একটি সংখ্যা দিন। `/add_channel` দিয়ে আবার চেষ্টা করুন।")

@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client: Client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "add_channel", "step": "awaiting_name"}
    await message.reply_text("📝 **চ্যানেলটির নাম লিখুন।**")

@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client: Client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/delete_channel <link or id>`\nউদাহরণ: `/delete_channel https://t.me/MyChannel`\nঅথবা `/delete_channel -100123456789`")

    identifier_to_delete = args[1]

    if await delete_join_channel(identifier_to_delete):
        await message.reply_text(f"🗑️ **চ্যানেলটি সফলভাবে মুছে ফেলা হয়েছে।**")
    else:
        await message.reply_text(f"❌ **এই আইডি বা লিংকের কোনো চ্যানেল খুঁজে পাওয়া যায়নি।**")

@app.on_message(filters.command("restrict") & filters.private & filters.user(ADMIN_ID))
async def restrict_cmd(client: Client, message):
    global restrict_status
    restrict_status = not restrict_status
    await save_data()
    status_text = "ON" if restrict_status else "OFF"
    await message.reply_text(f"🔒 **Message forwarding restriction is now {status_text}.**")

@app.on_message(filters.command("ban") & filters.private & filters.user(ADMIN_ID))
async def ban_cmd(client: Client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/ban <user_id>`")

    try:
        user_id_to_ban = int(args[1])
        if user_id_to_ban == ADMIN_ID:
            return await message.reply_text("❌ **You cannot ban yourself.**")

        if is_user_banned(user_id_to_ban):
            return await message.reply_text("⚠️ **This user is already banned.**")

        await ban_user(user_id_to_ban)
        await message.reply_text(f"✅ **User `{user_id_to_ban}` has been banned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_ID))
async def unban_cmd(client: Client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/unban <user_id>`")

    try:
        user_id_to_unban = int(args[1])
        if not is_user_banned(user_id_to_unban):
            return await message.reply_text("⚠️ **This user is not banned.**")

        await unban_user(user_id_to_unban)
        await message.reply_text(f"✅ **User `{user_id_to_unban}` has been unbanned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("auto_delete") & filters.private & filters.user(ADMIN_ID))
async def auto_delete_cmd(client: Client, message):
    args = message.text.split(maxsplit=2)

    if len(args) < 3:
        return await message.reply_text(
            "📌 **ব্যবহার:** `/auto_delete <keyword> <time>`\n\n"
            "**সময়ের বিকল্প:**\n- `30m` (30 মিনিট)\n- `1h` (1 ঘন্টা)\n- `12h` (12 ঘন্টা)\n- `24h` (24 ঘন্টা)\n- `off` অটো-ডিলিট বন্ধ করতে।"
        )

    keyword = args[1].lower()
    time_str = args[2].lower()

    if keyword not in filters_dict:
        return await message.reply_text(f"❌ **'{keyword}' নামের কোনো ফিল্টার খুঁজে পাওয়া যায়নি।**")

    time_map = {
        '30m': 30 * 60,
        '1h': 60 * 60,
        '12h': 12 * 60 * 60,
        '24h': 24 * 60 * 60,
        'off': 0
    }

    if time_str not in time_map:
        return await message.reply_text("❌ **ভুল সময় বিকল্প।** অনুগ্রহ করে `30m`, `1h`, `12h`, `24h`, অথবা `off` ব্যবহার করুন।")

    autodelete_time = time_map[time_str]

    if autodelete_time == 0:
        if keyword in autodelete_filters:
            del autodelete_filters[keyword]
        await message.reply_text(f"🗑️ **'{keyword}' ফিল্টারের জন্য অটো-ডিলিট বন্ধ করা হয়েছে।**")
    else:
        autodelete_filters[keyword] = autodelete_time
        await message.reply_text(f"✅ **'{keyword}' ফিল্টারের জন্য অটো-ডিলিট {time_str} তে সেট করা হয়েছে।**")

    await save_data()

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client: Client, callback_query):
    user_id = callback_query.from_user.id
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined the channels!** Please send the link again to get your files.", reply_markup=None)
    else:
        buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        await app.send_message(
            chat_id=callback_query.message.chat.id,
            text="❌ **You are still not a member of all channels.** Please make sure to join all of them.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMIN_ID))
async def channel_id_cmd(client: Client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    await message.reply_text(
        "➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**\n\n"
        "আমি সেই মেসেজ থেকে চ্যানেলের আইডি বের করে দেব।"
    )

# --- Bot start up ---
async def main():
    await load_data()
    load_last_filter()
    logger.info("Starting Flask background thread...")
    Thread(target=run_flask, daemon=True).start()
    logger.info("Starting Pyrogram client...")
    await app.start()
    logger.info("Bot started. Entering idle state.")
    try:
        await idle()
    finally:
        logger.info("Stopping bot...")
        await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Fatal error in main")
