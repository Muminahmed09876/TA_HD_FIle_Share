import os
import json
import asyncio
import time
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from flask import Flask, render_template_string
from threading import Thread

# --- Bot Configuration (Using Environment Variables for Security) ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID"))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID"))

# --- MongoDB Configuration ---
MONGO_URI = os.environ.get("MONGO_URI") 
client = MongoClient(MONGO_URI)
db = client["TA_HD_File_Share"] # Updated MongoDB database name

# --- MongoDB Collections ---
filters_collection = db["filters"]
users_collection = db["users"]
channels_collection = db["channels"]
banned_users_collection = db["banned_users"]

# --- Flask App for Ping Service ---
flask_app = Flask(__name__)
@flask_app.route('/')
def home():
    """A simple status page for the bot."""
    return render_template_string(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <title>TA File Share Bot</title>
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
                    border-radius: 10px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                    display: inline-block;
                    padding: 30px 50px;
                }
                h1 {
                    color: #0088cc;
                }
                p {
                    font-size: 1.2em;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Bot is Running!</h1>
                <p>The TA File Share Bot is online and working properly.</p>
                <p>Keep sharing!</p>
            </div>
        </body>
        </html>
        """
    )

def run_flask():
    """Runs the Flask app in a separate thread."""
    flask_app.run(host='0.0.0.0', port=os.environ.get("PORT", 5000))

# --- Helper Functions ---
def get_filter(keyword):
    """Retrieves a filter from the database."""
    return filters_collection.find_one({"keyword": keyword})

def add_file_to_filter(keyword, message_id):
    """Adds a file to an existing filter."""
    filters_collection.update_one(
        {"keyword": keyword},
        {"$push": {"file_ids": message_id}},
        upsert=True
    )

def delete_filter(keyword):
    """Deletes a filter and its associated auto-delete time."""
    filters_collection.delete_one({"keyword": keyword})

def set_autodelete_time(keyword, time_in_seconds):
    """Sets the auto-delete time for a specific filter."""
    filters_collection.update_one(
        {"keyword": keyword},
        {"$set": {"autodelete_time": time_in_seconds}},
        upsert=True
    )

def remove_autodelete_time(keyword):
    """Removes auto-delete time for a specific filter."""
    filters_collection.update_one(
        {"keyword": keyword},
        {"$unset": {"autodelete_time": ""}}
    )

def is_user_banned(user_id):
    """Checks if a user is banned."""
    return banned_users_collection.find_one({"user_id": user_id}) is not None

def ban_user(user_id):
    """Bans a user."""
    banned_users_collection.insert_one({"user_id": user_id})

def unban_user(user_id):
    """Unbans a user."""
    banned_users_collection.delete_one({"user_id": user_id})

def get_join_channels():
    """Gets all required join channels."""
    return list(channels_collection.find({}))

def add_join_channel(name, link, channel_id):
    """Adds a required join channel."""
    channels_collection.insert_one({"name": name, "link": link, "id": channel_id})

def delete_join_channel(identifier):
    """Deletes a join channel by link or ID."""
    try:
        channel_id = int(identifier)
        result = channels_collection.delete_one({"$or": [{"id": channel_id}, {"link": identifier}]})
    except ValueError:
        result = channels_collection.delete_one({"link": identifier})
    return result.deleted_count > 0

async def is_user_member(client, user_id):
    """Checks if a user is a member of all required channels."""
    join_channels = get_join_channels()
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

# --- Global state variables ---
user_states = {}
restrict_status = False
active_filter_keyword = None

# --- Message Handlers ---
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    if is_user_banned(user_id):
        return await message.reply_text("❌ **You are banned from using this bot.**")

    if not users_collection.find_one({"user_id": user_id}):
        users_collection.insert_one({"user_id": user_id})
        
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
        join_channels = get_join_channels()
        if join_channels and not await is_user_member(client, user_id):
            buttons = []
            for channel in join_channels:
                buttons.append([InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])])
            
            buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
            keyboard = InlineKeyboardMarkup(buttons)
            return await message.reply_text(
                "❌ **You must join the following channels to use this bot:**",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )

        filter_data = get_filter(deep_link_keyword)
        if filter_data and filter_data.get("file_ids"):
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
            
            delete_time = filter_data.get("autodelete_time", 0)
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
            for file_id in filter_data["file_ids"]:
                try:
                    sent_msg = await app.copy_message(
                        chat_id=message.chat.id,
                        from_chat_id=int(CHANNEL_ID),
                        message_id=file_id,
                        protect_content=restrict_status
                    )
                    sent_message_ids.append(sent_msg.id)
                    await asyncio.sleep(0.5)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
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

@app.on_message(filters.channel & filters.text & filters.chat(CHANNEL_ID))
async def channel_text_handler(client, message):
    global active_filter_keyword
    text = message.text
    if text and len(text.split()) == 1:
        keyword = text.lower().replace('#', '')
        if not keyword:
            return

        active_filter_keyword = keyword
        
        existing_filter = get_filter(keyword)
        if not existing_filter:
            filters_collection.insert_one({"keyword": keyword, "file_ids": []})
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

@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client, message):
    if active_filter_keyword:
        add_file_to_filter(active_filter_keyword, message.id)
    else:
        await app.send_message(
            ADMIN_ID,
            f"⚠️ **No active filter found.** Please create a new filter with a single-word message (e.g., `#newfilter`) in the channel.",
            parse_mode=ParseMode.MARKDOWN
        )

@app.on_deleted_messages(filters.channel & filters.chat(CHANNEL_ID))
async def channel_delete_handler(client, messages):
    global active_filter_keyword
    for message in messages:
        if message.text and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if get_filter(keyword):
                delete_filter(keyword)
                await app.send_message(
                    ADMIN_ID,
                    f"🗑️ **Filter '{keyword}' has been deleted** because the original message was removed from the channel.",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            if active_filter_keyword == keyword:
                active_filter_keyword = None
                await app.send_message(
                    ADMIN_ID,
                    "📝 **Note:** The last active filter has been cleared because the filter message was deleted."
                )

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
    if not message.reply_to_message:
        return await message.reply_text("📌 **Reply to a message** with `/broadcast` to send it to all users.")
    
    sent_count = 0
    failed_count = 0
    user_list = [user['user_id'] for user in users_collection.find({})]
    total_users = len(user_list)
    
    if total_users == 0:
        return await message.reply_text("❌ **No users found in the database.**")

    progress_msg = await message.reply_text(f"📢 **Broadcasting to {total_users} users...** (0/{total_users})")
    
    for user_id in user_list:
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
    global active_filter_keyword
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Please provide a keyword to delete.**\nExample: `/delete python`")

    keyword = args[1].lower()
    if get_filter(keyword):
        delete_filter(keyword)
        if active_filter_keyword == keyword:
            active_filter_keyword = None

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
    
    if not get_filter(keyword):
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
        remove_autodelete_time(keyword)
        await message.reply_text(f"🗑️ **'{keyword}' ফিল্টারের জন্য অটো-ডিলিট বন্ধ করা হয়েছে।**")
    else:
        set_autodelete_time(keyword, autodelete_time)
        await message.reply_text(f"✅ **'{keyword}' ফিল্টারের জন্য অটো-ডিলিট {time_str} তে সেট করা হয়েছে।**")

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined the channels!** Please send the link again to get your files.", reply_markup=None)
    else:
        buttons = []
        for channel in get_join_channels():
            buttons.append([InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])])
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
    print("Starting Flask app...")
    Thread(target=run_flask).start()

    print("Starting Pyrogram client...")
    app.run()
