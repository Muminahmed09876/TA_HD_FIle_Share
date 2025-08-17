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
PORT = int(os.environ.get("PORT", "5000"))

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
join_channels = []
restrict_status = False
autodelete_time = 0 
deep_link_keyword = None
user_states = {}

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

# --- Database Functions ---
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
    global filters_dict, user_list, last_filter, banned_users, join_channels, restrict_status, autodelete_time, user_states
    str_user_states = {str(uid): state for uid, state in user_states.items()}
    data = {
        "filters_dict": filters_dict,
        "user_list": list(user_list),
        "last_filter": last_filter,
        "banned_users": list(banned_users),
        "join_channels": join_channels,
        "restrict_status": restrict_status,
        "autodelete_time": autodelete_time,
        "user_states": str_user_states
    }
    collection.update_one({"_id": "bot_data"}, {"$set": data}, upsert=True)
    print("Data saved successfully to MongoDB.")

def load_data():
    global filters_dict, user_list, last_filter, banned_users, join_channels, restrict_status, autodelete_time, user_states
    data = collection.find_one({"_id": "bot_data"})
    if data:
        filters_dict = data.get("filters_dict", {})
        user_list = set(data.get("user_list", []))
        banned_users = set(data.get("banned_users", []))
        last_filter = data.get("last_filter", None)
        join_channels = data.get("join_channels", [])
        restrict_status = data.get("restrict_status", False)
        autodelete_time = data.get("autodelete_time", 0)
        loaded_user_states = data.get("user_states", {})
        user_states = {int(uid): state for uid, state in loaded_user_states.items()}
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

# --- Helper Functions ---
async def is_user_member(client, user_id):
    if not join_channels:
        return True
    for channel in join_channels:
        try:
            member = await client.get_chat_member(chat_id=channel['id'], user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except UserNotParticipant:
            return False
        except Exception as e:
            print(f"Error checking user {user_id} in channel {channel['id']}: {e}")
            return False
    return True

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
        print(f"Successfully deleted {len(message_ids)} messages from chat {chat_id} after {delay_seconds} seconds.")
    except Exception as e:
        print(f"Failed to delete messages from chat {chat_id}: {e}")

# --- Message Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    global deep_link_keyword, autodelete_time
    user_id = message.from_user.id
    user_list.add(user_id)
    save_data()
    
    if user_id in banned_users:
        return await message.reply_text("❌ **You are banned from using this bot.**")

    user = message.from_user
    log_message = (
        f"➡️ **New User**\n"
        f"🆔 User ID: `{user_id}`\n"
        f"👤 Full Name: `{user.first_name} {user.last_name or ''}`"
    )
    if user.username:
        log_message += f"\n🔗 Username: @{user.username}"
    try:
        await client.send_message(LOG_CHANNEL_ID, log_message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"Failed to send log message: {e}")
    
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        deep_link_keyword = args[1].lower()

    if not await is_user_member(client, user_id):
        buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        return await message.reply_text(
            "❌ **You must join the following channels to use this bot:**",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

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
                await message.reply_text(f"✅ **Files found!** Sending now. Please note, these files will be automatically deleted in **{delete_time_str}**.", parse_mode=ParseMode.MARKDOWN)
            else:
                await message.reply_text(f"✅ **Files found!** Sending now...")
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
            await message.reply_text("🎉 **All files sent!**")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, autodelete_time))
        else:
            await message.reply_text("❌ **No files found for this keyword.**")
        deep_link_keyword = None
        return
    
    if user_id == ADMIN_ID:
        await message.reply_text("🌟 **Welcome, Admin!** Check commands in the code.")
    else:
        await message.reply_text("👋 **Welcome!** You can access files via special links.")

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
            await app.send_message(
                ADMIN_ID,
                f"✅ **New filter created!**\n🔗 Share link: `https://t.me/{(await app.get_me()).username}?start={keyword}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await app.send_message(ADMIN_ID, f"⚠️ **Filter '{keyword}' is already active.**")

@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client, message):
    if last_filter:
        keyword = last_filter
        if keyword not in filters_dict:
            filters_dict[keyword] = []
        filters_dict[keyword].append(message.id)
        save_data()
    else:
        await app.send_message(ADMIN_ID, "⚠️ **No active filter found.**")

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
                await app.send_message(ADMIN_ID, f"🗑️ **Filter '{keyword}' has been deleted.**")
            if last_filter == keyword:
                last_filter = None
                await app.send_message(ADMIN_ID, "📝 **Note:** The last active filter has been cleared.")
                save_data()

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
    if not message.reply_to_message:
        return await message.reply_text("📌 **Reply to a message** with `/broadcast`.")
    sent_count = 0
    failed_count = 0
    total_users = len(user_list)
    progress_msg = await message.reply_text(f"📢 **Broadcasting to {total_users} users...** (0/{total_users})")
    for user_id in list(user_list):
        try:
            if user_id in banned_users:
                continue
            await message.reply_to_message.copy(user_id, protect_content=True)
            sent_count += 1
        except Exception as e:
            print(f"Failed to send broadcast to user {user_id}: {e}")
            failed_count += 1
        if (sent_count + failed_count) % 10 == 0:
            try:
                await progress_msg.edit_text(
                    f"📢 **Broadcasting...**\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}\nTotal: {total_users}"
                )
            except MessageNotModified:
                pass
        await asyncio.sleep(0.1)
    await progress_msg.edit_text(f"✅ **Broadcast complete!**\nSent to {sent_count} users.\nFailed to send to {failed_count} users.")

@app.on_message(filters.command("delete") & filters.private & filters.user(ADMIN_ID))
async def delete_cmd(client, message):
    global last_filter
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Please provide a keyword to delete.**")
    keyword = args[1].lower()
    if keyword in filters_dict:
        del filters_dict[keyword]
        if last_filter == keyword:
            last_filter = None
        save_data()
        await message.reply_text(f"🗑️ **Filter '{keyword}' and its associated files have been deleted.**")
    else:
        await message.reply_text(f"❌ **Filter '{keyword}' not found.**")

@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command(["add_channel", "delete_channel", "start", "broadcast", "delete", "ban", "unban", "restrict", "auto_delete", "channel_id"]))
async def handle_conversational_input(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        state = user_states[user_id]
        if state["command"] == "add_channel":
            if state["step"] == "awaiting_name":
                channel_name = message.text
                user_states[user_id]["name"] = channel_name
                user_states[user_id]["step"] = "awaiting_link_or_id"
                save_data()
                return await message.reply_text("🔗 **এখন চ্যানেলটির লিংক বা আইডি দিন।**")
            elif state["step"] == "awaiting_link_or_id":
                channel_id_or_link = message.text
                chat_id = None
                try:
                    chat_id = int(channel_id_or_link)
                except ValueError:
                    chat_id = channel_id_or_link.strip().replace("https://t.me/", "")
                    if chat_id.startswith("@"):
                        chat_id = chat_id[1:]
                    # এখানে chat_id এখন username, কিন্তু channel id দরকার!
                    return await message.reply_text(
                        "⚠️ **Please forward a message from your channel using `/channel_id` for accurate channel ID!**")
                channel_name = user_states[user_id]["name"]
                join_channels.append({
                    "name": channel_name,
                    "link": f"https://t.me/c/{str(chat_id)[4:]}" if str(chat_id).startswith("-100") else f"https://t.me/{chat_id}",
                    "id": int(chat_id)
                })
                del user_states[user_id]
                save_data()
                await message.reply_text(f"✅ **চ্যানেল `{channel_name}` সফলভাবে যুক্ত করা হয়েছে।**", parse_mode=ParseMode.MARKDOWN)

        elif state["command"] == "channel_id_awaiting_message":
            if message.forward_from_chat:
                chat_id = message.forward_from_chat.id
                await message.reply_text(f"✅ **Channel ID:** `{chat_id}`", parse_mode=ParseMode.MARKDOWN)
            else:
                await message.reply_text("❌ **Invalid message. Please forward directly from the channel.**")
            del user_states[user_id]
            save_data()
            return

@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "add_channel", "step": "awaiting_name"}
    save_data()
    await message.reply_text("📝 **চ্যানেলটির নাম লিখুন।**")

@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client, message):
    global join_channels
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/delete_channel <link or id>`", parse_mode=ParseMode.MARKDOWN)
    identifier_to_delete = args[1]
    found = False
    new_join_channels = []
    for channel in join_channels:
        if str(channel.get('id')) == identifier_to_delete or channel['link'] == identifier_to_delete:
            found = True
        else:
            new_join_channels.append(channel)
    if found:
        join_channels = new_join_channels
        save_data()
        await message.reply_text("🗑️ **চ্যানেলটি সফলভাবে মুছে ফেলা হয়েছে।**")
    else:
        await message.reply_text("❌ **এই আইডি বা লিংকের কোনো চ্যানেল খুঁজে পাওয়া যায়নি।**")

@app.on_message(filters.command("restrict") & filters.private & filters.user(ADMIN_ID))
async def restrict_cmd(client, message):
    global restrict_status
    restrict_status = not restrict_status
    save_data()
    status_text = "ON" if restrict_status else "OFF"
    await message.reply_text(f"🔒 **Message forwarding restriction is now {status_text}.**")
    
@app.on_message(filters.command("ban") & filters.private & filters.user(ADMIN_ID))
async def ban_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/ban <user_id>`", parse_mode=ParseMode.MARKDOWN)
    try:
        user_id_to_ban = int(args[1])
        if user_id_to_ban in banned_users:
            return await message.reply_text("⚠️ **This user is already banned.**")
        banned_users.add(user_id_to_ban)
        save_data()
        await message.reply_text(f"✅ **User `{user_id_to_ban}` has been banned.**", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.**")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_ID))
async def unban_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/unban <user_id>`", parse_mode=ParseMode.MARKDOWN)
    try:
        user_id_to_unban = int(args[1])
        if user_id_to_unban not in banned_users:
            return await message.reply_text("⚠️ **This user is not banned.**")
        banned_users.remove(user_id_to_unban)
        save_data()
        await message.reply_text(f"✅ **User `{user_id_to_unban}` has been unbanned.**", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.**")

@app.on_message(filters.command("auto_delete") & filters.private & filters.user(ADMIN_ID))
async def auto_delete_cmd(client, message):
    global autodelete_time
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/auto_delete <time>`")
    time_str = args[1].lower()
    time_map = {'30m': 1800, '1h': 3600, '12h': 43200, '24h': 86400, 'off': 0}
    if time_str not in time_map:
        return await message.reply_text("❌ **ভুল সময় বিকল্প।**")
    autodelete_time = time_map[time_str]
    save_data()
    if autodelete_time == 0:
        await message.reply_text(f"🗑️ **অটো-ডিলিট বন্ধ করা হয়েছে।**")
    else:
        await message.reply_text(f"✅ **অটো-ডিলিট {time_str} তে সেট করা হয়েছে।**")

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined!** Please send the link again.")
    else:
        buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        await app.send_message(callback_query.message.chat.id, "❌ **You are still not a member.**", reply_markup=keyboard)

@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMIN_ID))
async def channel_id_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    save_data()
    await message.reply_text("➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**")

@app.on_message(filters.private & filters.user(ADMIN_ID))
async def handle_channel_id_forward(client, message):
    user_id = message.from_user.id
    if user_states.get(user_id, {}).get("command") == "channel_id_awaiting_message":
        if message.forward_from_chat:
            chat_id = message.forward_from_chat.id
            await message.reply_text(f"✅ **Channel ID:** `{chat_id}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text("❌ **Invalid message. Please forward directly from the channel.**")
        del user_states[user_id]
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
