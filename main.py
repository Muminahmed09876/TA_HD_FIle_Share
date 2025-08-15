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
PORT = int(os.environ.get("PORT", 5000))

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
            body {font-family: Arial, sans-serif;background-color: #f0f2f5;color: #333;text-align: center;padding-top: 50px;}
            .container {background-color: #fff;padding: 30px;border-radius: 10px;box-shadow: 0 4px 8px rgba(0,0,0,0.1);display: inline-block;}
            h1 {color: #28a745;}
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

# --- Ping service to keep the bot alive ---
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
            print(f"Error checking user {user_id} in channel {channel['link']}: {e}")
            return False
    return True

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
        print(f"Deleted {len(message_ids)} messages from chat {chat_id}.")
    except Exception as e:
        print(f"Failed to delete messages from chat {chat_id}: {e}")

# --- Message Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    global last_filter
    user_id = message.from_user.id
    user_list.add(user_id)
    save_data()
    if user_id in banned_users:
        return await message.reply_text("❌ You are banned from using this bot.")
    
    # Handle deep link
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        keyword = args[1].lower()
        user_states[user_id] = {"command": "deep_link", "keyword": keyword}
        save_data()
    
    if not await is_user_member(client, user_id):
        buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        return await message.reply_text("❌ You must join the required channels to use this bot.", reply_markup=keyboard)
    
    # After join, check if deep link exists
    if user_id in user_states and user_states[user_id].get("command") == "deep_link":
        keyword = user_states[user_id]["keyword"]
        del user_states[user_id]
        save_data()
        if keyword in filters_dict and filters_dict[keyword]:
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
                    print(f"Error sending file {file_id}: {e}")
            await message.reply_text("🎉 All files sent!")
            if autodelete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, autodelete_time))
        else:
            await message.reply_text("❌ No files found for this keyword.")
        return

    if user_id == ADMIN_ID:
        await message.reply_text("🌟 Welcome Admin!")
    else:
        await message.reply_text("👋 Welcome! You can access files via special links.")

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_user_member(client, user_id):
        # Check if user has deep link saved
        if user_id in user_states and user_states[user_id].get("command") == "deep_link":
            keyword = user_states[user_id]["keyword"]
            del user_states[user_id]
            save_data()
            fake_start_msg = type('obj', (object,), {"chat": callback_query.message.chat,"from_user": callback_query.from_user,"text": f"/start {keyword}"})
            await start_cmd(client, fake_start_msg)
        else:
            await callback_query.message.edit_text("✅ You have successfully joined! Please send the link again.")
    else:
        buttons = [[InlineKeyboardButton(f"✅ Join {c['name']}", url=c['link'])] for c in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        await app.send_message(callback_query.message.chat.id, "❌ You are still not a member.", reply_markup=keyboard)

# --- Run Flask and Pyrogram ---
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
