import os
import asyncio
import time
import threading
from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask, render_template_string
import requests
from datetime import datetime, timedelta
import re
from pathlib import Path
from pyrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

# --- Load Environment Variables ---
load_dotenv()

# --- Bot Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.environ.get("PORT"))
GPLINKS_API_TOKEN = "b6347ee03a71f80e57831c7dcd5a5503c960e7c5"

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

# --- Join Channels Configuration ---
# Your original code used these variables. They are included here to avoid changes.
CHANNEL_ID_2 = -1002628995632
CHANNEL_LINK = "https://t.me/TA_HD_How_To_Download"
join_channels = [{"id": CHANNEL_ID_2, "name": "Backup Channel", "link": CHANNEL_LINK}]

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
    global filters_dict, user_list, last_filter, banned_users, restrict_status, autodelete_time, user_states
    
    str_user_states = {str(uid): state for uid, state in user_states.items()}

    data = {
        "filters_dict": filters_dict,
        "user_list": list(user_list),
        "last_filter": last_filter,
        "banned_users": list(banned_users),
        "restrict_status": restrict_status,
        "autodelete_time": autodelete_time,
        "user_states": str_user_states
    }
    collection.update_one({"_id": "bot_data"}, {"$set": data}, upsert=True)
    print("Data saved successfully to MongoDB.")

def load_data():
    global filters_dict, user_list, last_filter, banned_users, restrict_status, autodelete_time, user_states
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
async def is_member(client, user_id):
    try:
        member = await client.get_chat_member(CHANNEL_ID_2, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Error Aa Gayi Hai Bhai: {str(e)}")
        return False

# This function is not used in the final version but kept as per your original code.
async def check_access(update, client):
    if not await is_member(client, update.effective_user.id):
        Keyboard = [
            [InlineKeyboardButton('Join Our Channel', url=CHANNEL_LINK)],
            [InlineKeyboardButton('Verify', callback_data='verify_membership')]
        ]
        await update.message.reply_text(
            "Bhai Meri Channel Ko Join Karle",
            reply_markup=InlineKeyboardMarkup(Keyboard)
        )
        return False
    return True

# This function is not used in the final version but kept as per your original code.
async def handle_callback(client, callback_query):
    query = callback_query
    await query.answer()

    if query.data == 'verify_membership':
        if await is_member(client, query.from_user.id):
            await query.edit_message_text("You Joined")
        else:
            await query.edit_message_text("You Didnt Joined")

# This function is not used in the final version but kept as per your original code.
async def start_ptb(update, context):
    if not await check_access(update, context):
        return
    await context.bot.send_message(chat_id=update.effective_chat.id,text="This Is TraxDinosaur")
    
async def is_user_member(client, user_id):
    try:
        await client.get_chat_member(CHANNEL_ID_2, user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        print(f"Error checking membership: {e}")
        return False

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
        print(f"Successfully deleted messages {message_ids} in chat {chat_id}.")
    except Exception as e:
        print(f"Error deleting messages {message_ids} in chat {chat_id}: {e}")

async def create_short_link(long_url, alias=None):
    base_url = "https://api.gplinks.com/api"
    
    # Calculate expiration time one hour from now
    expiration_time = datetime.now() + timedelta(hours=1)
    
    params = {
        "api": GPLINKS_API_TOKEN,
        "url": long_url,
        "alias": alias,
        "Year": expiration_time.year,
        "Month": expiration_time.month,
        "Date": expiration_time.day,
        "hours": expiration_time.hour,
        "Min": expiration_time.minute
    }
    
    try:
        response = requests.get(base_url, params=params)
        response.raise_for_status()
        result = response.json()
        
        if result.get("status") == "success":
            return result.get("shortenedUrl")
        else:
            print(f"GPlinks API Error: {result.get('message')}")
            return None
    except requests.RequestException as e:
        print(f"Error calling GPlinks API: {e}")
        return None

# --- New Feature Global Variables and Helper Functions ---
SUBSCRIBERS = set()
USER_THUMBS = {}
USER_THUMB_TIME = {}
USER_CAPTIONS = {}
SET_THUMB_REQUEST = set()
SET_CAPTION_REQUEST = set()
EDIT_CAPTION_MODE = set()
USER_COUNTERS = {}
TASKS = {}
TMP = Path("./downloads")
TMP.mkdir(exist_ok=True)

def is_admin(user_id):
    return user_id == ADMIN_ID

def parse_time(time_str):
    seconds = 0
    time_str = time_str.lower()
    matches = re.findall(r"(\d+)([smhd])", time_str)
    for value, unit in matches:
        value = int(value)
        if unit == 's':
            seconds += value
        elif unit == 'm':
            seconds += value * 60
        elif unit == 'h':
            seconds += value * 3600
        elif unit == 'd':
            seconds += value * 86400
    return seconds

async def set_bot_commands():
    commands = [
        BotCommand("start", "bot শুরু করুন ও কমান্ড দেখুন"),
        BotCommand("help", "সাহায্য"),
        BotCommand("upload_url", "URL থেকে ফাইল আপলোড"),
        BotCommand("setthumb", "থাম্বনেইল সেট করুন"),
        BotCommand("view_thumb", "থাম্বনেইল দেখুন"),
        BotCommand("del_thumb", "থাম্বনেইল মুছে ফেলুন"),
        BotCommand("set_caption", "ক্যাপশন সেট করুন"),
        BotCommand("view_caption", "ক্যাপশন দেখুন"),
        BotCommand("edit_caption_mode", "শুধু ক্যাপশন এডিট করার মোড টগল করুন"),
        BotCommand("rename", "ফাইল রিনেম করুন"),
        BotCommand("broadcast", "ব্রডকাস্ট"),
        BotCommand("delete", "ফিল্টার ডিলিট করুন"),
        BotCommand("restrict", "মেসেজ ফরওয়ার্ডিং সীমাবদ্ধতা টগল করুন"),
        BotCommand("ban", "ইউজারকে ব্যান করুন"),
        BotCommand("unban", "ইউজারকে আনব্যান করুন"),
        BotCommand("auto_delete", "অটো-ডিলিট সময় সেট করুন"),
        BotCommand("channel_id", "চ্যানেল আইডি দেখুন")
    ]
    await app.set_bot_commands(commands, scope=BotCommandScopeDefault())

def delete_caption_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("ক্যাপশন মুছে ফেলুন", callback_data="delete_caption")]])

async def handle_url_download_and_upload(c, m: Message, url):
    # This is a placeholder for the actual download and upload logic.
    # The actual implementation would go here. For now, it just sends a message.
    await m.reply_text(f"আমি এখন `{url}` থেকে ফাইল ডাউনলোড করে আপলোড করব।")

async def process_file_and_upload(c, m: Message, file_path, original_name, messages_to_delete):
    # Placeholder for the file processing and upload logic.
    await m.reply_text(f"`{original_name}` নামে ফাইল আপলোড হচ্ছে।")
    
# --- Message Handlers (Pyrogram) ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    global deep_link_keyword, autodelete_time
    user_id = message.from_user.id
    user_list.add(user_id)
    save_data()
    
    if user_id in banned_users:
        return await message.reply_text("❌ **You are banned from using this bot.**")

    # New feature: add user to SUBSCRIBERS and set bot commands
    SUBSCRIBERS.add(message.chat.id)
    await set_bot_commands()
    text_message = (
        "👋 **Welcome!** You can access files via special links."
    )
    
    args = message.text.split(maxsplit=1)
    keyword = None
    
    if len(args) > 1:
        # Check for deep links from GPlinks
        if args[1].lower().startswith("get_"):
            parts = args[1].lower().split('_')
            if len(parts) >= 2:
                keyword = parts[1]
                
                # New logic: Check if the timestamp has expired
                if len(parts) > 2:
                    try:
                        timestamp = int(parts[2])
                        current_time = int(time.time())
                        if current_time - timestamp > 3600: # 3600 seconds = 1 hour
                            return await message.reply_text("❌ **এই লিংকটি মেয়াদ উত্তীর্ণ হয়ে গেছে।**", parse_mode=ParseMode.MARKDOWN)
                    except (ValueError, IndexError):
                        pass # Ignore if timestamp is invalid
        else:
            # Handle standard deep links
            deep_link_keyword = args[1].lower()
            keyword = deep_link_keyword
        
        if keyword:
            log_link_message = (
                f"🔗 **New Deep Link Open!**\n\n"
                f"🆔 User ID: `{user_id}`\n"
                f"👤 User Name: `{message.from_user.first_name} {message.from_user.last_name or ''}`\n"
                f"🔗 Link: `https://t.me/{(await client.get_me()).username}?start={args[1]}`"
            )
            if message.from_user.username:
                log_link_message += f"\nUsername: @{message.from_user.username}"
            try:
                await client.send_message(LOG_CHANNEL_ID, log_link_message, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                print(f"Failed to log deep link message: {e}")
    
    if not await is_user_member(client, user_id):
        # The key change is to use a URL button instead of a callback for "Try Again"
        # This will open the deep link and re-trigger the bot's start command.
        bot_username = (await client.get_me()).username
        try_again_url = f"https://t.me/{bot_username}?start={deep_link_keyword}" if deep_link_keyword else f"https://t.me/{bot_username}"
        
        buttons = [[InlineKeyboardButton(f"✅ Join TA_HD_How_To_Download", url=CHANNEL_LINK)]]
        buttons.append([InlineKeyboardButton("🔄 Try Again", url=try_again_url)])
        keyboard = InlineKeyboardMarkup(buttons)
        
        return await message.reply_text(
            "❌ **You must join the following channels to use this bot:**",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    if keyword:
        if keyword in filters_dict and filters_dict[keyword]:
            
            # If the user's deep link is a "get" link, send the files.
            if args[1].lower().startswith("get_"):
                sent_messages = []
                
                # Determine the initial message based on autodelete_time
                initial_message = "✅ Files found! Sending now..."
                if autodelete_time > 0:
                    time_map = {1800: "30 minutes", 3600: "1 hour", 43200: "12 hours", 86400: "24 hours"}
                    time_str = time_map.get(autodelete_time, f"{autodelete_time} seconds")
                    initial_message = f"✅ Files found! Sending now. Please note, these files will be automatically deleted in **{time_str}**."
                
                await message.reply_text(initial_message, parse_mode=ParseMode.MARKDOWN)

                try:
                    for msg_id in filters_dict[keyword]:
                        msg = await client.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=CHANNEL_ID,
                            message_id=msg_id
                        )
                        sent_messages.append(msg.id)
                        await asyncio.sleep(0.5) # Add a small delay
                    
                    await message.reply_text("🎉 All files sent!", parse_mode=ParseMode.MARKDOWN)
                
                except Exception as e:
                    await message.reply_text("❌ **An error occurred while trying to retrieve the files.** Please try again later.")
                    print(f"Error retrieving files: {e}")
                
                # New logic: If auto-delete is ON, delete the files
                if autodelete_time > 0:
                    asyncio.create_task(delete_messages_later(message.chat.id, sent_messages, autodelete_time))

            # If it's a regular deep link, generate and send the short link.
            else:
                timestamp = int(time.time())
                files_url = f"https://t.me/{(await client.get_me()).username}?start=get_{keyword}_{timestamp}"
                short_url = await create_short_link(files_url)
                
                if short_url:
                    buttons = [
                        [InlineKeyboardButton("Open Link", url=short_url)],
                        [InlineKeyboardButton("How to open the link", url="https://t.me/TA_HD_How_To_Download")]
                    ]
                    keyboard = InlineKeyboardMarkup(buttons)
                    
                    # Store the sent message object
                    sent_link_message = await message.reply_text(
                        f"✅ **Files found!**\n\nClick the button below to get your files. This link will expire in **1 hour**.\n\n🔗 Short Link: `{short_url}`",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.MARKDOWN,
                    )

                    # Schedule the deletion of the link message only
                    if autodelete_time > 0:
                        asyncio.create_task(delete_messages_later(sent_link_message.chat.id, [sent_link_message.id], autodelete_time))
                        
                else:
                    await message.reply_text("❌ **Failed to generate a short link. Please try again later.**")

        else:
            # This is the "no files found" message
            await message.reply_text("❌ **No files found for this keyword.**")
            
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
            "**/channel_id** - Get the ID of a channel by forwarding a message from it."
        )
        await message.reply_text(admin_commands, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(text_message, parse_mode=ParseMode.MARKDOWN)

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
                LOG_CHANNEL_ID,
                f"✅ **New filter created!**\n🔗 Share link: `https://t.me/{(await app.get_me()).username}?start={keyword}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await app.send_message(LOG_CHANNEL_ID, f"⚠️ **Filter '{keyword}' is already active.**")

@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client, message):
    if last_filter:
        keyword = last_filter
        if keyword not in filters_dict:
            filters_dict[keyword] = []
        filters_dict[keyword].append(message.id)
        save_data()
    else:
        await app.send_message(LOG_CHANNEL_ID, "⚠️ **No active filter found.**")

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
                await app.send_message(LOG_CHANNEL_ID, f"🗑️ **Filter '{keyword}' has been deleted.**")
            if last_filter == keyword:
                last_filter = None
                await app.send_message(LOG_CHANNEL_ID, "📝 **Note:** The last active filter has been cleared.")
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
    await callback_query.answer("Checking membership...", show_alert=True)
    
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined!**\n\n**Please go back to the chat and send your link again.**", parse_mode=ParseMode.MARKDOWN)
    else:
        buttons = [[InlineKeyboardButton(f"✅ Join TA_HD_How_To_Download", url=CHANNEL_LINK)]]
        
        # The key change here is using a URL button to automatically re-open the bot.
        bot_username = (await client.get_me()).username
        try_again_url = f"https://t.me/{bot_username}" # Opens the bot without any keyword

        buttons.append([InlineKeyboardButton("🔄 Try Again", url=try_again_url)])
        keyboard = InlineKeyboardMarkup(buttons)
        await callback_query.message.edit_text("❌ **You are still not a member.**", reply_markup=keyboard)

@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMIN_ID))
async def channel_id_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    save_data()
    await message.reply_text("➡️ **অনুগ্রহ করে একটি চ্যানেল অথবা গ্রুপ থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**")
    
@app.on_message(filters.forwarded & filters.private & filters.user(ADMIN_ID))
async def forwarded_message_handler(client, message):
    user_id = message.from_user.id
    if user_id in user_states and user_states[user_id].get("command") == "channel_id_awaiting_message":
        if message.forward_from_chat:
            chat_type = message.forward_from_chat.type
            chat_id = message.forward_from_chat.id
            message_id = message.forward_from_message_id
            user_chat_id = message.chat.id
            
            response_text = ""
            if chat_type == ChatType.CHANNEL:
                response_text = f"✅ **এটি একটি চ্যানেল।**\n\n**Channel ID:** `{chat_id}`\n**File ID (Message ID):** `{message_id}`\n**আপনার Chat ID:** `{user_chat_id}`"
            elif chat_type in [ChatType.SUPERGROUP, ChatType.GROUP]:
                response_text = f"✅ **এটি একটি গ্রুপ।**\n\n**Group ID:** `{chat_id}`\n**File ID (Message ID):** `{message_id}`\n**আপনার Chat ID:** `{user_chat_id}`"
            else:
                response_text = "❌ **এটি কোনো চ্যানেল বা গ্রুপ মেসেজ নয়।**"
            
            await message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text("❌ **এটি কোনো ফরওয়ার্ডেড মেসেজ নয়।**")
            
        del user_states[user_id]
        save_data()

# --- NEW FEATURES: Command Handlers ---

# start command handler with new features
@app.on_message(filters.command("start") & filters.private)
async def start_handler(c, m: Message):
    await set_bot_commands()
    SUBSCRIBERS.add(m.chat.id)
    text = (
        "Hi! আমি URL uploader bot.\n\n"
        "নোট: বটের অনেক কমান্ড শুধু অ্যাডমিন (owner) চালাতে পারবে।\n\n"
        "Commands:\n"
        "/upload_url <url> - URL থেকে ডাউনলোড ও Telegram-এ আপলোড (admin only)\n"
        "/setthumb - একটি ছবি পাঠান, সেট হবে আপনার থাম্বনেইল (admin only)\n"
        "/view_thumb - আপনার থাম্বনেইল দেখুন (admin only)\n"
        "/del_thumb - আপনার থাম্বনেইল মুছে ফেলুন (admin only)\n"
        "/set_caption - একটি ক্যাপশন সেট করুন (admin only)\n"
        "/view_caption - আপনার ক্যাপশন দেখুন (admin only)\n"
        "/edit_caption_mode - শুধু ক্যাপশন এডিট করার মোড টগল করুন (admin only)\n"
        "/rename <newname.ext> - reply করা ভিডিও রিনেম করুন (admin only)\n"
        "/broadcast <text> - ব্রডকাস্ট (শুধুমাত্র অ্যাডমিন)\n"
        "/help - সাহায্য"
    )
    await m.reply_text(text)

# help command handler
@app.on_message(filters.command("help") & filters.private)
async def help_handler(c, m):
    await start_handler(c, m)

# setthumb command handler
@app.on_message(filters.command("setthumb") & filters.private)
async def setthumb_prompt(c, m):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    
    uid = m.from_user.id
    if len(m.command) > 1:
        time_str = " ".join(m.command[1:])
        seconds = parse_time(time_str)
        if seconds > 0:
            USER_THUMB_TIME[uid] = seconds
            await m.reply_text(f"থাম্বনেইল তৈরির সময় সেট হয়েছে: {seconds} সেকেন্ড।")
        else:
            await m.reply_text("সঠিক ফরম্যাটে সময় দিন। উদাহরণ: `/setthumb 5s`, `/setthumb 1m`, `/setthumb 1m 30s`")
    else:
        SET_THUMB_REQUEST.add(uid)
        await m.reply_text("একটি ছবি পাঠান (photo) — সেট হবে আপনার থাম্বনেইল।")

# view_thumb command handler
@app.on_message(filters.command("view_thumb") & filters.private)
async def view_thumb_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    thumb_path = USER_THUMBS.get(uid)
    thumb_time = USER_THUMB_TIME.get(uid)
    
    if thumb_path and Path(thumb_path).exists():
        await c.send_photo(chat_id=m.chat.id, photo=thumb_path, caption="এটা আপনার সেভ করা থাম্বনেইল।")
    elif thumb_time:
        await m.reply_text(f"আপনার থাম্বনেইল তৈরির সময় সেট করা আছে: {thumb_time} সেকেন্ড।")
    else:
        await m.reply_text("আপনার কোনো থাম্বনেইল বা থাম্বনেইল তৈরির সময় সেভ করা নেই। /setthumb দিয়ে সেট করুন।")

# del_thumb command handler
@app.on_message(filters.command("del_thumb") & filters.private)
async def del_thumb_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    thumb_path = USER_THUMBS.get(uid)
    if thumb_path and Path(thumb_path).exists():
        try:
            Path(thumb_path).unlink()
        except Exception:
            pass
        USER_THUMBS.pop(uid, None)
    
    if uid in USER_THUMB_TIME:
        USER_THUMB_TIME.pop(uid)

    if not (thumb_path or uid in USER_THUMB_TIME):
        await m.reply_text("আপনার কোনো থাম্বনেইল সেভ করা নেই।")
    else:
        await m.reply_text("আপনার থাম্বনেইল/থাম্বনেইল তৈরির সময় মুছে ফেলা হয়েছে।")

# set_caption command handler
@app.on_message(filters.command("set_caption") & filters.private)
async def set_caption_prompt(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    SET_CAPTION_REQUEST.add(m.from_user.id)
    # Reset counter data when a new caption is about to be set
    USER_COUNTERS.pop(m.from_user.id, None)
    await m.reply_text("ক্যাপশন দিন। কোড - [01 (+01, 01u)], [re (480p, 720p, 1080p)]")

# view_caption command handler
@app.on_message(filters.command("view_caption") & filters.private)
async def view_caption_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    uid = m.from_user.id
    caption = USER_CAPTIONS.get(uid)
    if caption:
        await m.reply_text(f"আপনার সেভ করা ক্যাপশন:\n\n`{caption}`", reply_markup=delete_caption_keyboard())
    else:
        await m.reply_text("আপনার কোনো ক্যাপশন সেভ করা নেই। /set_caption দিয়ে সেট করুন।")

# edit_caption_mode command handler
@app.on_message(filters.command("edit_caption_mode") & filters.private)
async def toggle_edit_caption_mode(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return

    if uid in EDIT_CAPTION_MODE:
        EDIT_CAPTION_MODE.discard(uid)
        await m.reply_text("edit video caption mod off.\nএখন থেকে আপলোড করা ভিডিওর রিনেম ও থাম্বনেইল পরিবর্তন হবে, এবং সেভ করা ক্যাপশন যুক্ত হবে।")
    else:
        EDIT_CAPTION_MODE.add(uid)
        await m.reply_text("edit video caption mod on.\nএখন থেকে শুধু সেভ করা ক্যাপশন ভিডিওতে যুক্ত হবে। ভিডিওর নাম এবং থাম্বনেইল একই থাকবে।")

# upload_url command handler
@app.on_message(filters.command("upload_url") & filters.private)
async def upload_url_cmd(c, m: Message):
    if not is_admin(m.from_user.id):
        await m.reply_text("আপনার অনুমতি নেই এই কমান্ড চালানোর।")
        return
    if not m.command or len(m.command) < 2:
        await m.reply_text("ব্যবহার: /upload_url <url>\nউদাহরণ: /upload_url https://example.com/file.mp4")
        return
    url = m.text.split(None, 1)[1].strip()
    asyncio.create_task(handle_url_download_and_upload(c, m, url))

# rename command handler
@app.on_message(filters.command("rename") & filters.private)
async def rename_cmd(c, m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        await m.reply_text("আপনার অনুমতি নেই।")
        return
    if not m.reply_to_message or not (m.reply_to_message.video or m.reply_to_message.document):
        await m.reply_text("ভিডিও/ডকুমেন্ট ফাইলের reply দিয়ে এই কমান্ড দিন।\nUsage: /rename new_name.mp4")
        return
    if len(m.command) < 2:
        await m.reply_text("নতুন ফাইল নাম দিন। উদাহরণ: /rename new_video.mp4")
        return
    new_name = m.text.split(None, 1)[1].strip()
    new_name = re.sub(r"[\\/*?\"<>|:]", "_", new_name)
    await m.reply_text(f"ভিডিও রিনেম করা হবে: {new_name}\n(রিনেম করতে reply করা ফাইলটি পুনরায় ডাউনলোড করে আপলোড করা হবে)")

    cancel_event = asyncio.Event()
    TASKS.setdefault(uid, []).append(cancel_event)
    try:
        status_msg = await m.reply_text("রিনেমের জন্য ফাইল ডাউনলোড করা হচ্ছে...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", "cancel_download")]]))
    except Exception:
        status_msg = await m.reply_text("রিনেমের জন্য ফাইল ডাউনলোড করা হচ্ছে...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", "cancel_download")]]))
    tmp_out = TMP / f"rename_{uid}_{int(datetime.now().timestamp())}_{new_name}"
    try:
        await m.reply_to_message.download(file_name=str(tmp_out))
        try:
            await status_msg.edit_text("ডাউনলোড সম্পন্ন, এখন নতুন নাম দিয়ে আপলোড হচ্ছে...", reply_markup=None)
        except Exception:
            await m.reply_text("ডাউনলোড সম্পন্ন, এখন নতুন নাম দিয়ে আপলোড হচ্ছে...", reply_markup=None)
        await process_file_and_upload(c, m, tmp_out, original_name=new_name, messages_to_delete=[status_msg.id])
    except Exception as e:
        await m.reply_text(f"রিনেম ত্রুটি: {e}")
    finally:
        try:
            TASKS[uid].remove(cancel_event)
        except Exception:
            pass

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
