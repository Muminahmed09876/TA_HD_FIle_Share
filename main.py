import os
import asyncio
import time
import threading
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask, render_template_string
import requests

# --- Load Environment Variables ---
load_dotenv()

# --- Bot Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.environ.get("PORT"))

CHANNEL_ID = -1002619816346
LOG_CHANNEL_ID = -1002623880704

# --- MongoDB Configuration ---
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = "TA_HD_File_Share"
COLLECTION_NAME = "bot_data"

# --- In-memory data structures ---
filters_dict = {}
user_list = set()
last_filter = None
banned_users = set()
restrict_status = False
autodelete_time = 0
deep_link_keyword = None
user_states = {}
join_channels = []

# --- Database Client and Collection ---
mongo_client = None
db = None
collection = None

# --- Flask Web Server ---
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Status</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background-color: #f0f2f5;
                color: #333;
                text-align: center;
                padding-top: 50px;
            }
            .container {
                background-color: #fff;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                display: inline-block;
            }
            h1 {
                color: #28a745;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>TA File Share Bot is running! ✅</h1>
            <p>This page confirms that the bot's web server is active.</p>
        </div>
    </body>
    </html>
    """
    return render_template_string(html_content)

# Ping service to keep the bot alive
def ping_service():
    if not RENDER_EXTERNAL_HOSTNAME:
        print("Render URL is not set. Ping service is disabled.")
        return

    url = f"http://{RENDER_EXTERNAL_HOSTNAME}"
    while True:
        try:
            response = requests.get(url, timeout=10)
            print(f"Pinged {url} | Status Code: {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"Error pinging {url}: {e}")
        time.sleep(600)

# --- Database Functions (Updated) ---
def connect_to_mongodb():
    global mongo_client, db, collection
    try:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client[DB_NAME]
        collection = db[COLLECTION_NAME]
        print("Successfully connected to MongoDB.")
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")
        exit(1)

def save_data():
    global filters_dict, user_list, last_filter, banned_users, restrict_status, autodelete_time, user_states, join_channels
    
    str_user_states = {str(uid): state for uid, state in user_states.items()}

    data = {
        "filters_dict": filters_dict,
        "user_list": list(user_list),
        "last_filter": last_filter,
        "banned_users": list(banned_users),
        "restrict_status": restrict_status,
        "autodelete_time": autodelete_time,
        "user_states": str_user_states,
        "join_channels": join_channels
    }
    collection.update_one({"_id": "bot_data"}, {"$set": data}, upsert=True)
    print("Data saved successfully to MongoDB.")

def load_data():
    global filters_dict, user_list, last_filter, banned_users, restrict_status, autodelete_time, user_states, join_channels
    data = collection.find_one({"_id": "bot_data"})
    if data:
        filters_dict = data.get("filters_dict", {})
        user_list = set(data.get("user_list", []))
        banned_users = set(data.get("banned_users", []))
        last_filter = data.get("last_filter", None)
        restrict_status = data.get("restrict_status", False)
        autodelete_time = data.get("autodelete_time", 0)
        loaded_user_states = data.get("user_states", {})
        user_states = {int(uid): state for uid, state in loaded_user_states.items()}
        join_channels = data.get("join_channels", [])
        print("Data loaded successfully from MongoDB.")
    else:
        print("No data found in MongoDB. Starting with empty data.")
        save_data()

# --- Pyrogram Client ---
app = Client(
    "ta_file_share_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Helper Functions (Pyrogram) ---
async def check_user_membership(client, user_id):
    """Checks if a user is a member of all required channels."""
    required_to_join = []
    if not join_channels:
        return True, None

    for channel in join_channels:
        try:
            member = await client.get_chat_member(channel['id'], user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                required_to_join.append(channel)
        except UserNotParticipant:
            required_to_join.append(channel)
        except Exception as e:
            print(f"Error checking membership for channel {channel['id']}: {e}")
            
    if required_to_join:
        return False, required_to_join
    return True, None


async def delete_messages_later(chat_id, message_ids, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
        print(f"Successfully deleted messages {message_ids} in chat {chat_id}.")
    except Exception as e:
        print(f"Error deleting messages {message_ids} in chat {chat_id}: {e}")

# --- Message Handlers (Pyrogram) ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    global deep_link_keyword, autodelete_time
    user_id = message.from_user.id
    user_list.add(user_id)
    save_data()
    
    if user_id in banned_users:
        sent_msg = await message.reply_text("❌ **You are banned from using this bot.**")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        return

    user = message.from_user
    log_message = (
        f"➡️ **New User**\n"
        f"🆔 User ID: `{user_id}`\n"
        f"👤 Full Name: `{user.first_name} {user.last_name or ''}`"
    )
    if user.username:
        log_message += f"\n🔗 Username: @{user.username}"
    try:
        log_msg = await client.send_message(LOG_CHANNEL_ID, log_message, parse_mode=ParseMode.MARKDOWN)
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(log_msg.chat.id, [log_msg.id], autodelete_time))
    except Exception as e:
        print(f"Failed to send log message: {e}")
    
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        deep_link_keyword = args[1].lower()
        log_link_message = (
            f"🔗 **New Deep Link Open!**\n\n"
            f"🆔 User ID: `{user.id}`\n"
            f"👤 User Name: `{user.first_name} {user.last_name or ''}`\n"
            f"🔗 Link: `https://t.me/{(await client.get_me()).username}?start={deep_link_keyword}`"
        )
        if user.username:
            log_link_message += f"\nUsername: @{user.username}"
        try:
            log_msg = await client.send_message(LOG_CHANNEL_ID, log_link_message, parse_mode=ParseMode.MARKDOWN)
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(log_msg.chat.id, [log_msg.id], autodelete_time))
        except Exception as e:
            print(f"Failed to log deep link message: {e}")

    is_member, required_to_join = await check_user_membership(client, user_id)
    if not is_member:
        buttons = []
        for channel in required_to_join:
            buttons.append([InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])])
        
        bot_username = (await client.get_me()).username
        try_again_url = f"https://t.me/{bot_username}?start={deep_link_keyword}" if deep_link_keyword else f"https://t.me/{bot_username}"
        buttons.append([InlineKeyboardButton("🔄 Try Again", url=try_again_url)])
        keyboard = InlineKeyboardMarkup(buttons)
        
        sent_msg = await message.reply_text(
            "❌ **You must join the following channels to use this bot:**",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        return

    if deep_link_keyword:
        keyword = deep_link_keyword
        if keyword in filters_dict and filters_dict[keyword]:
            if autodelete_time > 0:
                minutes = autodelete_time // 60
                hours = autodelete_time // 3600
                if hours > 0:
                    delete_time_str = f"{hours} hour{'s' if hours > 1 else ''}"
                else:
                    delete_time_str = f"{minutes} minute{'s' if minutes > 1 else ''}"
                sent_msg = await message.reply_text(f"✅ **Files found!** Sending now. Please note, these files will be automatically deleted in **{delete_time_str}**.", parse_mode=ParseMode.MARKDOWN)
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
            else:
                sent_msg = await message.reply_text(f"✅ **Files found!** Sending now...")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
            sent_message_ids = []
            for file_id in filters_dict[keyword]:
                try:
                    sent_msg = await app.copy_message(message.chat.id, CHANNEL_ID, file_id, protect_content=restrict_status)
                    sent_message_ids.append(sent_msg.id)
                    await asyncio.sleep(0.5)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    sent_msg = await app.copy_message(message.chat.id, CHANNEL_ID, file_id, protect_content=restrict_status)
                    sent_message_ids.append(sent_msg.id)
                except Exception as e:
                    print(f"Error copying message {file_id}: {e}")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, autodelete_time))

            sent_msg = await message.reply_text("🎉 **All files sent!**")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

        else:
            sent_msg = await message.reply_text("❌ **No files found for this keyword.**")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

        deep_link_keyword = None
        return
    
    if user_id == ADMIN_ID:
        admin_commands = (
            "🌟 **Welcome, Admin! Here are your commands:**\n\n"
            "**/broadcast** - Reply to a message with this command to broadcast it to all users.\n"
            "**/delete <keyword>** - Delete a filter and its associated files.\n"
            "**/restrict** - Toggle message forwarding restriction (ON/OFF).\n"
            "**/ban <user_id>** - Ban a user.\n"
            "**/unban <user_id>** - Unban a user.\n"
            "**/auto_delete <time>** - Set auto-delete time for files (e.g., 30m, 1h, 12h, 24h, off).\n"
            "**/channel_id** - Get the ID of a channel by forwarding a message from it.\n"
            "**/add_channel** - Add a new mandatory join channel.\n"
            "**/delete_channel** - Delete a mandatory join channel.\n"
            "**/list_channels** - List all mandatory join channels."
        )
        sent_msg = await message.reply_text(admin_commands, parse_mode=ParseMode.MARKDOWN)
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
    else:
        sent_msg = await message.reply_text("👋 **Welcome!** You can access files via special links.")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "add_channel_name"}
    save_data()
    sent_msg = await message.reply_text("➡️ **Please send the name of the channel.**")
    if autodelete_time > 0:
        asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client, message):
    global join_channels
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        sent_msg = await message.reply_text("📌 **Please provide the channel ID or link to delete.**\n**Usage:** `/delete_channel -10012345678` or `/delete_channel t.me/example`")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        return

    query = args[1].strip()
    original_channel_count = len(join_channels)
    
    # Filter out channels that match the query
    join_channels = [c for c in join_channels if str(c['id']) != query and query not in c['link']]
    
    if len(join_channels) < original_channel_count:
        save_data()
        sent_msg = await message.reply_text(f"🗑️ **Channel matching `{query}` has been deleted.**")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
    else:
        sent_msg = await message.reply_text("❌ **No channel found with that ID or link.**")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

@app.on_message(filters.command("list_channels") & filters.private & filters.user(ADMIN_ID))
async def list_channels_cmd(client, message):
    if not join_channels:
        sent_msg = await message.reply_text("There are no mandatory join channels added yet.")
    else:
        channel_list_text = "📚 **Mandatory Join Channels:**\n"
        for channel in join_channels:
            channel_list_text += f"\n- **Name:** {channel['name']}\n  - **ID:** `{channel['id']}`\n  - **Link:** {channel['link']}"
        sent_msg = await message.reply_text(channel_list_text, parse_mode=ParseMode.MARKDOWN)
    if autodelete_time > 0:
        asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))


@app.on_message(filters.text & filters.private & filters.user(ADMIN_ID) & ~filters.command(["start", "add_channel", "delete_channel", "list_channels", "broadcast", "delete", "restrict", "ban", "unban", "auto_delete", "channel_id"]))
async def add_channel_state_handler(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        state = user_states[user_id]["command"]
        if state == "add_channel_name":
            user_states[user_id]["name"] = message.text
            user_states[user_id]["command"] = "add_channel_id"
            save_data()
            sent_msg = await message.reply_text("➡️ **Now, please send the channel's ID.** (e.g., -10012345678)")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

        elif state == "add_channel_id":
            try:
                channel_id = int(message.text)
                user_states[user_id]["id"] = channel_id
                user_states[user_id]["command"] = "add_channel_link"
                save_data()
                sent_msg = await message.reply_text("➡️ **Finally, please send the channel's link.**")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

            except ValueError:
                sent_msg = await message.reply_text("❌ **Invalid channel ID.** Please try again with a number.")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

        elif state == "add_channel_link":
            channel_link = message.text
            new_channel = {
                "name": user_states[user_id]["name"],
                "id": user_states[user_id]["id"],
                "link": channel_link
            }
            
            # Check for duplicates before adding
            if any(c['id'] == new_channel['id'] for c in join_channels):
                sent_msg = await message.reply_text("⚠️ **This channel is already in the list.**")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
            else:
                join_channels.append(new_channel)
                save_data()
                sent_msg = await message.reply_text("✅ **Channel added successfully!**")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

            del user_states[user_id]
            save_data()

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    await callback_query.answer("Checking membership...", show_alert=True)
    
    is_member, required_to_join = await check_user_membership(client, user_id)

    if is_member:
        await callback_query.message.edit_text("✅ **You have successfully joined!**\n\n**Please go back to the chat and send your link again.**", parse_mode=ParseMode.MARKDOWN)
    else:
        buttons = []
        for channel in required_to_join:
            buttons.append([InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])])

        bot_username = (await client.get_me()).username
        try_again_url = f"https://t.me/{bot_username}"
        buttons.append([InlineKeyboardButton("🔄 Try Again", url=try_again_url)])
        keyboard = InlineKeyboardMarkup(buttons)
        await callback_query.message.edit_text("❌ **You are still not a member.**", reply_markup=keyboard)


@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMIN_ID))
async def channel_id_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    save_data()
    sent_msg = await message.reply_text("➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**")
    if autodelete_time > 0:
        asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
    
@app.on_message(filters.forwarded & filters.private & filters.user(ADMIN_ID))
async def forwarded_message_handler(client, message):
    user_id = message.from_user.id
    if user_id in user_states and user_states[user_id].get("command") == "channel_id_awaiting_message":
        if message.forward_from_chat:
            channel_id = message.forward_from_chat.id
            sent_msg = await message.reply_text(f"✅ **Channel ID:** `{channel_id}`", parse_mode=ParseMode.MARKDOWN)
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        else:
            sent_msg = await message.reply_text("❌ **এটি একটি চ্যানেল মেসেজ নয়।**")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        del user_states[user_id]
        save_data()

@app.on_message(filters.channel & filters.text & filters.chat(CHANNEL_ID))
async def channel_text_handler(client, message):
    global last_filter
    text = message.text
    if text and len(text.split()) == 1:
        keyword = text.lower().replace('#', '')
        if not keyword:
            return
        last_filter = keyword
        save_data()
        if keyword not in filters_dict:
            filters_dict[keyword] = []
            save_data()
            sent_msg = await app.send_message(
                LOG_CHANNEL_ID,
                f"✅ **New filter created!**\n🔗 Share link: `https://t.me/{(await app.get_me()).username}?start={keyword}`",
                parse_mode=ParseMode.MARKDOWN
            )
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
        else:
            sent_msg = await app.send_message(LOG_CHANNEL_ID, f"⚠️ **Filter '{keyword}' is already active.**")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client, message):
    if last_filter:
        keyword = last_filter
        if keyword not in filters_dict:
            filters_dict[keyword] = []
        filters_dict[keyword].append(message.id)
        save_data()
    else:
        sent_msg = await app.send_message(LOG_CHANNEL_ID, "⚠️ **No active filter found.**")
        if autodelete_time > 0:
            asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))

@app.on_deleted_messages(filters.channel & filters.chat(CHANNEL_ID))
async def channel_delete_handler(client, messages):
    global last_filter
    for message in messages:
        if message.text and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if keyword in filters_dict:
                del filters_dict[keyword]
                if keyword == last_filter:
                    last_filter = None
                save_data()
                sent_msg = await app.send_message(LOG_CHANNEL_ID, f"🗑️ **Filter '{keyword}' has been deleted.**")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
            if last_filter == keyword:
                last_filter = None
                sent_msg = await app.send_message(LOG_CHANNEL_ID, "📝 **Note:** The last active filter has been cleared.")
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(sent_msg.chat.id, [sent_msg.id], autodelete_time))
                save_data()

# --- Run Services ---
def run_flask_and_pyrogram():
    connect_to_mongodb()
    load_data()
    flask_thread = threading.Thread(target=lambda: app_flask.run(host="0.0.0.0", port=PORT, use_reloader=False))
    flask_thread.start()
    ping_thread = threading.Thread(target=ping_service)
    ping_thread.start()
    print("Starting TA File Share Bot...")
    app.run()

if __name__ == "__main__":
    run_flask_and_pyrogram()
