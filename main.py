import os
import json
import asyncio
import time
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from threading import Thread
from flask import Flask # <-- এই লাইনটি যোগ করুন

# --- Bot Configuration (Using Environment Variables for Security) ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH") # <-- এখানে অতিরিক্ত বন্ধনী সরানো হয়েছে
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID"))

# --- Database Configuration ---
DATABASE_FILE = "database.json"
LAST_FILTER_FILE = "last_filter.txt"

# --- Global Data Variables ---
filters_dict = {}
user_list = set()
banned_users = set()
join_channels = []
restrict_status = False
autodelete_filters = {}
user_states = {}
last_filter = None

# --- Data Management Functions ---
def save_data():
    """Saves all bot data to a JSON file."""
    data = {
        "filters_dict": filters_dict,
        "user_list": list(user_list),
        "banned_users": list(banned_users),
        "join_channels": join_channels,
        "restrict_status": restrict_status,
        "autodelete_filters": autodelete_filters,
        "user_states": user_states
    }
    with open(DATABASE_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_data():
    """Loads all bot data from a JSON file."""
    global filters_dict, user_list, banned_users, join_channels, restrict_status, autodelete_filters, user_states
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, "r") as f:
            data = json.load(f)
            filters_dict = data.get("filters_dict", {})
            user_list = set(data.get("user_list", []))
            banned_users = set(data.get("banned_users", []))
            join_channels = data.get("join_channels", [])
            restrict_status = data.get("restrict_status", False)
            autodelete_filters = data.get("autodelete_filters", {})
            user_states = data.get("user_states", {})
        print("Data loaded successfully.")
    else:
        print("No database file found. Starting with empty data.")

def save_last_filter(keyword):
    """Saves the last active filter keyword to a file."""
    with open(LAST_FILTER_FILE, "w") as f:
        f.write(keyword)

def load_last_filter():
    """Loads the last active filter from a file."""
    global last_filter
    if os.path.exists(LAST_FILTER_FILE):
        with open(LAST_FILTER_FILE, "r") as f:
            last_filter = f.read().strip()
        print(f"Last filter '{last_filter}' loaded.")
    else:
        print("No last filter file found.")

# --- Flask App for Ping Service ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    """A simple status page for the bot."""
    return f"<h1>Bot is Running!</h1><p>The TA File Share Bot is online and working properly.</p><p>Keep sharing!</p>"

def run_flask():
    """Runs the Flask app in a separate thread."""
    flask_app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))

# --- Helper Functions ---
def is_user_banned(user_id):
    """Checks if a user is banned."""
    return user_id in banned_users

def ban_user(user_id):
    """Bans a user."""
    banned_users.add(user_id)
    save_data()

def unban_user(user_id):
    """Unbans a user."""
    banned_users.discard(user_id)
    save_data()

def get_join_channels():
    """Gets all required join channels."""
    return join_channels

def add_join_channel(name, link, channel_id):
    """Adds a required join channel."""
    join_channels.append({"name": name, "link": link, "id": channel_id})
    save_data()

def delete_join_channel(identifier):
    """Deletes a join channel by link or ID."""
    global join_channels
    original_count = len(join_channels)
    try:
        channel_id = int(identifier)
        join_channels = [c for c in join_channels if c['id'] != channel_id]
    except ValueError:
        join_channels = [c for c in join_channels if c['link'] != identifier]
    
    if len(join_channels) < original_count:
        save_data()
        return True
    return False

async def is_user_member(client, user_id):
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
        except Exception as e:
            print(f"Error checking user {user_id} in channel {channel['link']}: {e}")
            return False
    return True

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    """Schedules the deletion of messages after a delay."""
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
    except Exception as e:
        print(f"Failed to delete messages from chat {chat_id}: {e}")

# --- Pyrogram Client ---
app = Client(
    "ta_file_share_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Message Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        return await message.reply_text("❌ **You are banned from using this bot.**")

    if user_id not in user_list:
        user_list.add(user_id)
        save_data()
        
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
        except Exception as e:
            print(f"Failed to send log message to channel: {e}")

    args = message.text.split(maxsplit=1)
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
            log_link_message = (
                f"🔍 **Deep Link Clicked**\n"
                f"🆔 User ID: `{user_id}`\n"
                f"👤 Full Name: `{user_full_name}`\n"
                f"🔑 Keyword: `{deep_link_keyword}`"
            )
            try:
                await client.send_message(LOG_CHANNEL_ID, log_link_message, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                print(f"Failed to log deep link activity: {e}")
            
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
                        print(f"Invalid file_id found: {file_id}")
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    if isinstance(file_id, int):
                        sent_msg = await app.copy_message(
                            chat_id=message.chat.id,
                            from_chat_id=int(CHANNEL_ID),
                            message_id=file_id,
                            protect_content=restrict_status
                        )
                        sent_message_ids.append(sent_msg.id)
                except Exception as e:
                    print(f"Error copying message {file_id}: {e}")
                    pass
            
            await message.reply_text(
                "🎉 **সকল ফাইল পাঠানো সম্পন্ন হয়েছে!** আশা করি আপনি যা খুঁজছিলেন তা পেয়েছেন।"
            )
            
            if delete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, delete_time))
        else:
            await message.reply_text("❌ **এই কিওয়ার্ডের জন্য কোনো ফাইল খুঁজে পাওয়া যায়নি।**")
        return
    
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
async def channel_text_handler(client, message):
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
            save_data()
            await app.send_message(
                ADMIN_ID,
                f"✅ **New filter created!**\n"
                f"🔗 Share link: `https://t.me/{(await app.get_me()).username}?start={keyword}`\n\n"
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
async def channel_media_handler(client, message):
    if message.from_user and message.from_user.id == ADMIN_ID and last_filter:
        keyword = last_filter
        
        # Ensure the filter exists before adding a message
        if keyword not in filters_dict:
            filters_dict[keyword] = []

        filters_dict[keyword].append(message.id)
        save_data()
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
async def channel_delete_handler(client, messages):
    global last_filter
    for message in messages:
        if message.text and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if keyword in filters_dict:
                del filters_dict[keyword]
                if keyword in autodelete_filters:
                    del autodelete_filters[keyword]
                save_data()
                await app.send_message(
                    ADMIN_ID,
                    f"🗑️ **Filter '{keyword}' has been deleted** because the original message was removed from the channel.",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            if last_filter == keyword:
                last_filter = None
                with open(LAST_FILTER_FILE, "w") as f:
                    f.write("")
                await app.send_message(
                    ADMIN_ID,
                    "📝 **Note:** The last active filter has been cleared because the filter message was deleted."
                )

# --- Other Handlers (Admin Commands) ---
@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
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
            await message.reply_to_message.copy(user_id, protect_content=True)
            sent_count += 1
        except Exception as e:
            print(f"Failed to send broadcast to user {user_id}: {e}")
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
            except Exception as e:
                print(f"Error updating progress message: {e}")
        
        await asyncio.sleep(0.1)
    
    try:
        await progress_msg.edit_text(
            f"✅ **Broadcast complete!**\n"
            f"Sent to {sent_count} users.\n"
            f"Failed to send to {failed_count} users."
        )
    except:
        await message.reply_text(
            f"✅ **Broadcast complete!**\n"
            f"Sent to {sent_count} users.\n"
            f"Failed to send to {failed_count} users."
        )

@app.on_message(filters.command("delete") & filters.private & filters.user(ADMIN_ID))
async def delete_cmd(client, message):
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
            with open(LAST_FILTER_FILE, "w") as f:
                f.write("")

        save_data()
        await message.reply_text(
            f"🗑️ **Filter '{keyword}' and its associated files have been deleted.**",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(f"❌ **Filter '{keyword}' not found.**")

@app.on_message(filters.private & filters.user(ADMIN_ID) & filters.text & ~filters.command(["add_channel", "delete_channel", "start", "broadcast", "delete", "ban", "unban", "restrict", "auto_delete", "channel_id"]))
async def handle_conversational_input(client, message):
    user_id = message.from_user.id
    if user_id in user_states:
        state = user_states[user_id]
        
        if state["command"] == "channel_id_awaiting_message":
            if message.forward_from_chat:
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
                    
                    add_join_channel(channel_name, channel_link, channel_id)
                    del user_states[user_id]
                    await message.reply_text(f"✅ **চ্যানেল '{channel_name}' সফলভাবে যুক্ত হয়েছে!**")
                except ValueError:
                    del user_states[user_id]
                    await message.reply_text("❌ **ভুল আইডি ফরম্যাট।** অনুগ্রহ করে একটি সংখ্যা দিন। `/add_channel` দিয়ে আবার চেষ্টা করুন।")

@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "add_channel", "step": "awaiting_name"}
    await message.reply_text("📝 **চ্যানেলটির নাম লিখুন।**")

@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/delete_channel <link or id>`\nউদাহরণ: `/delete_channel https://t.me/MyChannel`\nঅথবা `/delete_channel -100123456789`")
    
    identifier_to_delete = args[1]
    
    if delete_join_channel(identifier_to_delete):
        await message.reply_text(f"🗑️ **চ্যানেলটি সফলভাবে মুছে ফেলা হয়েছে।**")
    else:
        await message.reply_text(f"❌ **এই আইডি বা লিংকের কোনো চ্যানেল খুঁজে পাওয়া যায়নি।**")

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
        return await message.reply_text("📌 **Usage:** `/ban <user_id>`")
    
    try:
        user_id_to_ban = int(args[1])
        if user_id_to_ban == ADMIN_ID:
            return await message.reply_text("❌ **You cannot ban yourself.**")
        
        if is_user_banned(user_id_to_ban):
            return await message.reply_text("⚠️ **This user is already banned.**")
        
        ban_user(user_id_to_ban)
        await message.reply_text(f"✅ **User `{user_id_to_ban}` has been banned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_ID))
async def unban_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/unban <user_id>`")
    
    try:
        user_id_to_unban = int(args[1])
        if not is_user_banned(user_id_to_unban):
            return await message.reply_text("⚠️ **This user is not banned.**")
        
        unban_user(user_id_to_unban)
        await message.reply_text(f"✅ **User `{user_id_to_unban}` has been unbanned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("auto_delete") & filters.private & filters.user(ADMIN_ID))
async def auto_delete_cmd(client, message):
    args = message.text.split(maxsplit=2)

    if len(args) < 3:
        return await message.reply_text("📌 **ব্যবহার:** `/auto_delete <keyword> <time>`\n\n**সময়ের বিকল্প:**\n- `30m` (30 মিনিট)\n- `1h` (1 ঘন্টা)\n- `12h` (12 ঘন্টা)\n- `24h` (24 ঘন্টা)\n- `off` অটো-ডিলিট বন্ধ করতে।")
    
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
    
    save_data()

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
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
async def channel_id_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    await message.reply_text(
        "➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**\n\n"
        "আমি সেই মেসেজ থেকে চ্যানেলের আইডি বের করে দেব।"
    )

# --- Bot start up ---
if __name__ == "__main__":
    load_data()
    load_last_filter()
    print("Starting Flask app...")
    Thread(target=run_flask).start()

    print("Starting Pyrogram client...")
    app.run()
