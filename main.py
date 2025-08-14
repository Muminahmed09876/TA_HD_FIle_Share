import os
import json
import asyncio
import time
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web

# --- Bot Configuration from Environment Variables ---
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# The bot MUST be an admin in this channel.
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", 0))
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))

# --- Database Configuration ---
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = "TA_HD_File_Share" # <--- এখানে ডাটাবেজের নাম পরিবর্তন করা হয়েছে।

# --- In-memory data structures ---
# These will now be loaded from and saved to MongoDB
filters_dict = {}  
user_list = set()  
banned_users = set()
join_channels = []  
restrict_status = False
autodelete_filters = {}
last_filter = None
user_states = {}

# --- Database Client ---
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client[DATABASE_NAME]
users_collection = db.users
filters_collection = db.filters
settings_collection = db.settings

# --- Helper Functions (Updated for MongoDB) ---
async def save_settings():
    """Saves bot settings to the database."""
    settings_doc = {
        "_id": "bot_settings",
        "join_channels": join_channels,
        "restrict_status": restrict_status,
        "last_filter": last_filter,
        "user_states": user_states
    }
    await settings_collection.update_one({"_id": "bot_settings"}, {"$set": settings_doc}, upsert=True)

async def load_settings():
    """Loads bot settings from the database."""
    global join_channels, restrict_status, last_filter, user_states
    settings_doc = await settings_collection.find_one({"_id": "bot_settings"})
    if settings_doc:
        join_channels = settings_doc.get("join_channels", [])
        restrict_status = settings_doc.get("restrict_status", False)
        last_filter = settings_doc.get("last_filter")
        user_states = settings_doc.get("user_states", {})
    
async def load_filters():
    """Loads all filters from the database."""
    global filters_dict, autodelete_filters
    filters_cursor = filters_collection.find({})
    filters_dict = {}
    autodelete_filters = {}
    async for filter_doc in filters_cursor:
        keyword = filter_doc["keyword"]
        filters_dict[keyword] = filter_doc["file_ids"]
        if filter_doc.get("autodelete_time"):
            autodelete_filters[keyword] = filter_doc["autodelete_time"]

async def save_filter(keyword, file_ids, autodelete_time=None):
    """Saves or updates a single filter in the database."""
    filter_doc = {
        "keyword": keyword,
        "file_ids": file_ids
    }
    if autodelete_time is not None:
        filter_doc["autodelete_time"] = autodelete_time
    await filters_collection.update_one({"keyword": keyword}, {"$set": filter_doc}, upsert=True)

async def delete_filter_from_db(keyword):
    """Deletes a filter from the database."""
    await filters_collection.delete_one({"keyword": keyword})

async def load_users():
    """Loads all users and banned users from the database."""
    global user_list, banned_users
    users_cursor = users_collection.find({})
    user_list = set()
    banned_users = set()
    async for user_doc in users_cursor:
        user_id = user_doc["user_id"]
        user_list.add(user_id)
        if user_doc.get("is_banned"):
            banned_users.add(user_id)

async def update_user(user_id, is_banned=False):
    """Updates a user's status in the database."""
    await users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": is_banned}},
        upsert=True
    )

async def is_user_member(client, user_id):
    """Checks if a user is a member of all required channels."""
    if not join_channels:
        return True
    
    for channel in join_channels:
        try:
            member = await client.get_chat_member(chat_id=channel['id'], user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except UserNotParticipant:
            return False
        except Exception:
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

## Handler for the /start command
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    global last_filter
    user_id = message.from_user.id

    await update_user(user_id)
    await load_users() # Reload banned users for real-time check

    if user_id in banned_users:
        return await message.reply_text("❌ **You are banned from using this bot.**")

    # Log user information to the log channel
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

    # Check for join channel restriction
    if restrict_status and not await is_user_member(client, user_id):
        buttons = [[InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])] for channel in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        return await message.reply_text(
            "❌ **You must join the following channels to use this bot:**",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    # Handle deep links for file sharing
    if deep_link_keyword:
        keyword = deep_link_keyword
        log_link_message = (
            f"🔍 **Deep Link Clicked**\n"
            f"🆔 User ID: `{user_id}`\n"
            f"👤 Full Name: `{user_full_name}`\n"
            f"🔑 Keyword: `{keyword}`"
        )
        try:
            await client.send_message(LOG_CHANNEL_ID, log_link_message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"Failed to log deep link activity: {e}")

        if keyword in filters_dict and filters_dict[keyword]:
            delete_time = autodelete_filters.get(keyword, 0)
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
            for file_id in filters_dict[keyword]:
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
            
            await message.reply_text(
                "🎉 **সকল ফাইল পাঠানো সম্পন্ন হয়েছে!** আশা করি আপনি যা খুঁজছিলেন তা পেয়েছেন।"
            )
            
            if delete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, delete_time))
        else:
            await message.reply_text("❌ **এই কিওয়ার্ডের জন্য কোনো ফাইল খুঁজে পাওয়া যায়নি।**")
        return
    
    # Admin start message
    if user_id == ADMIN_ID:
        await message.reply_text(
            "🌟 **Welcome, Admin!** 🌟\n\n"
            "This bot is your personal file-sharing hub.\n\n"
            "**Channel Workflow:**\n"
            "📂 **Create Filter**: Send a single-word message in the channel (e.g., `#python`).\n"
            "💾 **Add Files**: Any media sent after that will be added to the filter.\n"
            "🗑️ **Delete Filter**: Delete the original single-word message to remove the filter.\n\n"
            "**Commands:**\n"
            "• `/broadcast` to send a message to all users.\n"
            "• `/delete <keyword>` to remove a filter and its files.\n"
            "• `/ban <user_id>` to ban a user.\n"
            "• `/unban <user_id>` to unban a user.\n"
            "• `/add_channel` to add a required join channel.\n"
            "• `/delete_channel <link or id>` to delete a channel from join list.\n"
            "• `/restrict` to toggle the channel join requirement.\n"
            "• `/auto_delete <time>` to set auto-delete time for the active filter.\n"
            "• `/channel_id` to get a channel ID by forwarding a message to the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    # Regular user start message
    else:
        await message.reply_text(
            "👋 **Welcome!**\n\n"
            "This bot is a file-sharing service. You can access files "
            "by using a special link provided by the admin.\n\n"
            "Have a great day!",
            parse_mode=ParseMode.MARKDOWN
        )

## Handler for channel messages (Filter Management)
@app.on_message(filters.channel & filters.text & filters.chat(CHANNEL_ID))
async def channel_text_handler(client, message):
    global last_filter, filters_dict
    text = message.text
    if text and len(text.split()) == 1:
        keyword = text.lower().replace('#', '')
        if not keyword:
            return

        last_filter = keyword
        await save_settings()
        
        if keyword not in filters_dict:
            filters_dict[keyword] = []
            await save_filter(keyword, [])
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

## Handler for new media in the channel
@app.on_message(filters.channel & filters.media & filters.chat(CHANNEL_ID))
async def channel_media_handler(client, message):
    if last_filter:
        keyword = last_filter
        
        if keyword not in filters_dict:
            filters_dict[keyword] = []
            
        filters_dict[keyword].append(message.id)
        await save_filter(keyword, filters_dict[keyword], autodelete_filters.get(keyword))
        
    else:
        await app.send_message(
            ADMIN_ID,
            f"⚠️ **No active filter found.** Please create a new filter with a single-word message (e.g., `#newfilter`) in the channel.",
            parse_mode=ParseMode.MARKDOWN
        )

## Handler for message deletion in the channel (to delete filters)
@app.on_deleted_messages(filters.channel & filters.chat(CHANNEL_ID))
async def channel_delete_handler(client, messages):
    global last_filter, filters_dict, autodelete_filters
    for message in messages:
        if message.text and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if keyword in filters_dict:
                await delete_filter_from_db(keyword)
                await load_filters()  # Reload filters after deletion
                await app.send_message(
                    ADMIN_ID,
                    f"🗑️ **Filter '{keyword}' has been deleted** because the original message was removed from the channel.",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            if last_filter == keyword:
                last_filter = None
                await save_settings()
                await app.send_message(
                    ADMIN_ID,
                    "📝 **Note:** The last active filter has been cleared because the filter message was deleted."
                )

## Handler for the /broadcast command
@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
    if not message.reply_to_message:
        return await message.reply_text("📌 **Reply to a message** with `/broadcast` to send it to all users.")
    
    sent_count = 0
    failed_count = 0
    total_users = len(user_list)
    
    if total_users == 0:
        return await message.reply_text("❌ **No users found in the database.**")

    progress_msg = await message.reply_text(f"📢 **Broadcasting to {total_users} users...** (0/{total_users})")
    
    for user_id in list(user_list):
        if user_id in banned_users:
            continue
        try:
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

# --- New Admin Commands ---

## Conversational handler for text messages
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
            await save_settings()
            return

        if state["command"] == "add_channel":
            if state["step"] == "awaiting_name":
                user_states[user_id]["channel_name"] = message.text
                user_states[user_id]["step"] = "awaiting_link"
                await message.reply_text("🔗 **এবার চ্যানেলের লিংক দিন।** (যেমন: `https://t.me/channel` অথবা `t.me/channel`)")
                await save_settings()
            elif state["step"] == "awaiting_link":
                channel_link = message.text
                if not (channel_link.startswith('https://t.me/') or channel_link.startswith('t.me/')):
                    del user_states[user_id]
                    await save_settings()
                    await message.reply_text("❌ **ভুল লিংক ফরম্যাট।** `/add_channel` দিয়ে আবার চেষ্টা করুন।")
                    return
                user_states[user_id]["channel_link"] = channel_link
                user_states[user_id]["step"] = "awaiting_id"
                await message.reply_text("🆔 **এবার চ্যানেলের আইডি দিন।** (যেমন: `-100123456789`)")
                await save_settings()
            elif state["step"] == "awaiting_id":
                try:
                    channel_id = int(message.text)
                    channel_name = user_states[user_id]["channel_name"]
                    channel_link = user_states[user_id]["channel_link"]
                    
                    join_channels.append({'name': channel_name, 'link': channel_link, 'id': channel_id})
                    await save_settings()
                    del user_states[user_id]
                    await message.reply_text(f"✅ **চ্যানেল '{channel_name}' সফলভাবে যুক্ত হয়েছে!**")
                except ValueError:
                    del user_states[user_id]
                    await save_settings()
                    await message.reply_text("❌ **ভুল আইডি ফরম্যাট।** অনুগ্রহ করে একটি সংখ্যা দিন। `/add_channel` দিয়ে আবার চেষ্টা করুন।")

## Add channel conversational command
@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "add_channel", "step": "awaiting_name"}
    await save_settings()
    await message.reply_text("📝 **চ্যানেলটির নাম লিখুন।**")

## Delete channel command
@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client, message):
    global join_channels
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/delete_channel <link or id>`\nউদাহরণ: `/delete_channel https://t.me/MyChannel`\nঅথবা `/delete_channel -100123456789`")
    
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
        await save_settings()
        await message.reply_text(f"🗑️ **চ্যানেলটি সফলভাবে মুছে ফেলা হয়েছে।**")
    else:
        await message.reply_text(f"❌ **এই আইডি বা লিংকের কোনো চ্যানেল খুঁজে পাওয়া যায়নি।**")
    
## Toggle channel join restriction
@app.on_message(filters.command("restrict") & filters.private & filters.user(ADMIN_ID))
async def restrict_cmd(client, message):
    global restrict_status
    restrict_status = not restrict_status
    await save_settings()
    status_text = "ON" if restrict_status else "OFF"
    await message.reply_text(f"🔒 **Message forwarding restriction is now {status_text}.**")
    
## Ban a user
@app.on_message(filters.command("ban") & filters.private & filters.user(ADMIN_ID))
async def ban_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/ban <user_id>`")
    
    try:
        user_id_to_ban = int(args[1])
        if user_id_to_ban == ADMIN_ID:
            return await message.reply_text("❌ **You cannot ban yourself.**")
        
        if user_id_to_ban in banned_users:
            return await message.reply_text("⚠️ **This user is already banned.**")
        
        banned_users.add(user_id_to_ban)
        await update_user(user_id_to_ban, is_banned=True)
        await message.reply_text(f"✅ **User `{user_id_to_ban}` has been banned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

## Unban a user
@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_ID))
async def unban_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/unban <user_id>`")
    
    try:
        user_id_to_unban = int(args[1])
        if user_id_to_unban not in banned_users:
            return await message.reply_text("⚠️ **This user is not banned.**")
        
        banned_users.remove(user_id_to_unban)
        await update_user(user_id_to_unban, is_banned=False)
        await message.reply_text(f"✅ **User `{user_id_to_unban}` has been unbanned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

## Set auto-delete time for a filter (Updated Command)
@app.on_message(filters.command("auto_delete") & filters.private & filters.user(ADMIN_ID))
async def auto_delete_cmd(client, message):
    global last_filter, autodelete_filters
    args = message.text.split(maxsplit=1)

    if not last_filter:
        return await message.reply_text("❌ **কোনো সক্রিয় ফিল্টার নেই।** প্রথমে একটি নতুন ফিল্টার তৈরি করুন।")

    if len(args) < 2:
        return await message.reply_text("📌 **ব্যবহার:** `/auto_delete <time>`\n\n**সময়ের বিকল্প:**\n- `30m` (30 মিনিট)\n- `1h` (1 ঘন্টা)\n- `12h` (12 ঘন্টা)\n- `24h` (24 ঘন্টা)\n- `off` অটো-ডিলিট বন্ধ করতে।")
    
    time_str = args[1].lower()
    
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
        if last_filter in autodelete_filters:
            del autodelete_filters[last_filter]
            await save_filter(last_filter, filters_dict.get(last_filter, []), autodelete_time)
            await message.reply_text(f"🗑️ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট বন্ধ করা হয়েছে।**")
        else:
            await message.reply_text(f"⚠️ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট আগেই বন্ধ ছিল।**")
    else:
        autodelete_filters[last_filter] = autodelete_time
        await save_filter(last_filter, filters_dict.get(last_filter, []), autodelete_time)
        await message.reply_text(f"✅ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট {time_str} তে সেট করা হয়েছে।**")

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined the channels!** Please send the link again to get your files.", reply_markup=None)
    else:
        buttons = [[InlineKeyboardButton(f"✅ Join {channel['name']}", url=channel['link'])] for channel in join_channels]
        buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_status")])
        keyboard = InlineKeyboardMarkup(buttons)
        await app.send_message(
            chat_id=callback_query.message.chat.id,
            text="❌ **You are still not a member of all channels.** Please make sure to join all of them.",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

# New Command: Interactive Channel ID finder
@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMIN_ID))
async def channel_id_cmd(client, message):
    user_id = message.from_user.id
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    await save_settings()
    await message.reply_text(
        "➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**\n\n"
        "আমি সেই মেসেজ থেকে চ্যানেলের আইডি বের করে দেব।"
    )

# --- Web server for ping service ---
async def ping_handler(request):
    """Simple handler for health checks."""
    return web.Response(text="Bot is running!")

async def start_web_server():
    """Starts the web server."""
    app_runner = web.AppRunner(web.Application())
    app_runner.router.add_get('/ping', ping_handler)
    await app_runner.setup()
    site = web.TCPSite(app_runner, '0.0.0.0', 8080)
    await site.start()
    print("Web server started on port 8080.")

# --- Bot start up ---
async def main():
    print("Loading data from MongoDB...")
    await load_users()
    await load_filters()
    await load_settings()
    print("Starting TA File Share Bot...")
    await asyncio.gather(start_web_server(), app.run())

if __name__ == "__main__":
    asyncio.run(main())
