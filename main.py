import os
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from aiohttp import web

# --- Set up logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Bot Configuration ---
try:
    API_ID = int(os.getenv("API_ID"))
    API_HASH = os.getenv("API_HASH")
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
    MONGODB_URI = os.getenv("MONGODB_URI")
    CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
    LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID"))
except (ValueError, TypeError) as e:
    logger.error(f"Environment variables are missing or invalid: {e}")
    exit(1)

# --- MongoDB Data Structures ---
db_client = None
db = None
filters_collection = None
users_collection = None
admin_data_collection = None

# --- In-memory data structures ---
filters_dict = {}
user_list = set()
last_filter = None
banned_users = set()
join_channels = []
restrict_status = False
autodelete_filters = {}
deep_link_keyword = None
user_states = {}

# --- Helper Functions ---
def save_admin_data():
    """Saves admin-specific data to MongoDB."""
    if admin_data_collection:
        data = {
            "last_filter": last_filter,
            "join_channels": join_channels,
            "restrict_status": restrict_status,
            "autodelete_filters": autodelete_filters,
            "user_states": user_states
        }
        admin_data_collection.update_one({"_id": "admin_settings"}, {"$set": data}, upsert=True)
        logger.info("Admin data saved successfully to MongoDB.")

def save_user_data(user_id, is_banned=False):
    """Saves or updates a user's status in MongoDB."""
    if users_collection:
        users_collection.update_one({"_id": user_id}, {"$set": {"banned": is_banned}}, upsert=True)
        logger.info(f"User {user_id} data saved successfully to MongoDB.")

def save_filter_data(keyword, file_ids):
    """Saves or updates a filter's files in MongoDB."""
    if filters_collection:
        filters_collection.update_one({"_id": keyword}, {"$set": {"files": file_ids}}, upsert=True)
        logger.info(f"Filter '{keyword}' saved successfully to MongoDB.")

async def load_data_from_mongodb():
    """Loads all data from MongoDB into in-memory structures."""
    global filters_dict, user_list, banned_users, join_channels, restrict_status, autodelete_filters, user_states, last_filter
    
    if not db_client:
        logger.error("MongoDB connection not established. Cannot load data.")
        return

    logger.info("Loading data from MongoDB...")

    filters_cursor = filters_collection.find({})
    for doc in filters_cursor:
        filters_dict[doc["_id"]] = doc.get("files", [])
    
    users_cursor = users_collection.find({})
    for doc in users_cursor:
        user_id = doc["_id"]
        user_list.add(user_id)
        if doc.get("banned", False):
            banned_users.add(user_id)

    admin_data_doc = admin_data_collection.find_one({"_id": "admin_settings"})
    if admin_data_doc:
        last_filter = admin_data_doc.get("last_filter")
        join_channels = admin_data_doc.get("join_channels", [])
        restrict_status = admin_data_doc.get("restrict_status", False)
        autodelete_filters = admin_data_doc.get("autodelete_filters", {})
        user_states = admin_data_doc.get("user_states", {})

    logger.info("Data loaded from MongoDB successfully.")

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
        except Exception as e:
            logger.error(f"Error checking user {user_id} in channel {channel['link']}: {e}")
            return False
    return True

async def delete_messages_later(chat_id, message_ids, delay_seconds):
    """Schedules the deletion of messages after a delay."""
    await asyncio.sleep(delay_seconds)
    try:
        await app.delete_messages(chat_id, message_ids)
        logger.info(f"Successfully deleted {len(message_ids)} messages from chat {chat_id} after {delay_seconds} seconds.")
    except Exception as e:
        logger.error(f"Failed to delete messages from chat {chat_id}: {e}")

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
    global deep_link_keyword
    user_id = message.from_user.id
    logger.info(f"User {user_id} sent /start command.")

    if user_id in banned_users:
        return await message.reply_text("❌ **You are banned from using this bot.**")

    if user_id not in user_list:
        user_list.add(user_id)
        save_user_data(user_id)
    
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
        logger.error(f"Failed to send log message to channel: {e}")
    
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        deep_link_keyword = args[1].lower()
    
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
            logger.error(f"Failed to log deep link activity: {e}")
        
        if keyword in filters_dict and filters_dict[keyword]:
            delete_time = autodelete_filters.get(keyword, 0)
            if delete_time > 0:
                await message.reply_text(
                    f"✅ **ফাইল পাওয়া গেছে!** এই ফাইলগুলো স্বয়ংক্রিয়ভাবে {int(delete_time / 60)} মিনিটের মধ্যে মুছে যাবে।",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await message.reply_text(f"✅ **ফাইল পাওয়া গেছে!** ফাইল পাঠানো হচ্ছে...", parse_mode=ParseMode.MARKDOWN)
            
            sent_message_ids = []
            for file_id in filters_dict[keyword]:
                try:
                    sent_msg = await app.copy_message(chat_id=message.chat.id, from_chat_id=CHANNEL_ID, message_id=file_id, protect_content=restrict_status)
                    sent_message_ids.append(sent_msg.id)
                    await asyncio.sleep(0.5)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    sent_msg = await app.copy_message(chat_id=message.chat.id, from_chat_id=CHANNEL_ID, message_id=file_id, protect_content=restrict_status)
                    sent_message_ids.append(sent_msg.id)
                except Exception as e:
                    logger.error(f"Error copying message {file_id}: {e}")
                    pass
            
            await message.reply_text("🎉 **সকল ফাইল পাঠানো সম্পন্ন হয়েছে!** আশা করি আপনি যা খুঁজছিলেন তা পেয়েছেন।")
            
            if delete_time > 0:
                asyncio.create_task(delete_messages_later(message.chat.id, sent_message_ids, delete_time))
        else:
            await message.reply_text("❌ **এই কিওয়ার্ডের জন্য কোনো ফাইল খুঁজে পাওয়া যায়নি।**")
        deep_link_keyword = None
        return
    
    if user_id == ADMIN_ID:
        await message.reply_text(
            "🌟 **Welcome, Admin!** 🌟\n\nThis bot is your personal file-sharing hub.\n\n**Channel Workflow:**\n📂 **Create Filter**: Send a single-word message in the channel (e.g., `#python`).\n💾 **Add Files**: Any media sent after that will be added to the filter.\n🗑️ **Delete Filter**: Delete the original single-word message to remove the filter.\n\n**Commands:**\n• `/broadcast` to send a message to all users.\n• `/delete <keyword>` to remove a filter and its files.\n• `/ban <user_id>` to ban a user.\n• `/unban <user_id>` to unban a user.\n• `/add_channel` to add a required join channel.\n• `/delete_channel <link or id>` to delete a channel from join list.\n• `/restrict` to toggle the channel join requirement.\n• `/auto_delete <time>` to set auto-delete time for the active filter.\n• `/channel_id` to get a channel ID by forwarding a message to the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.reply_text(
            "👋 **Welcome!**\n\nThis bot is a file-sharing service. You can access files by using a special link provided by the admin.\n\nHave a great day!",
            parse_mode=ParseMode.MARKDOWN
        )

@app.on_message(filters.channel & filters.text & filters.chat(CHANNEL_ID))
async def channel_text_handler(client, message):
    global last_filter
    logger.info(f"Received text message in channel {CHANNEL_ID}: {message.text}")
    text = message.text
    if text and len(text.split()) == 1:
        keyword = text.lower().replace('#', '')
        if not keyword:
            return

        last_filter = keyword
        save_admin_data()
        
        if keyword not in filters_dict:
            filters_dict[keyword] = []
            save_filter_data(keyword, [])
            await app.send_message(
                ADMIN_ID,
                f"✅ **New filter created!**\n🔗 Share link: `https://t.me/{(await app.get_me()).username}?start={keyword}`\n\nAny media you send now will be added to this filter.",
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
    logger.info(f"Received media message in channel {CHANNEL_ID}.")
    if last_filter:
        keyword = last_filter
        if keyword not in filters_dict:
            filters_dict[keyword] = []

        filters_dict[keyword].append(message.id)
        save_filter_data(keyword, filters_dict[keyword])
    else:
        await app.send_message(
            ADMIN_ID,
            f"⚠️ **No active filter found.** Please create a new filter with a single-word message (e.g., `#newfilter`) in the channel.",
            parse_mode=ParseMode.MARKDOWN
        )

@app.on_deleted_messages(filters.channel & filters.chat(CHANNEL_ID))
async def channel_delete_handler(client, messages):
    global last_filter
    for message in messages:
        if message.text and len(message.text.split()) == 1:
            keyword = message.text.lower().replace('#', '')
            if keyword in filters_dict:
                del filters_dict[keyword]
                if filters_collection:
                    filters_collection.delete_one({"_id": keyword})
                if keyword in autodelete_filters:
                    del autodelete_filters[keyword]
                
                await app.send_message(
                    ADMIN_ID,
                    f"🗑️ **Filter '{keyword}' has been deleted** because the original message was removed from the channel.",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            if last_filter == keyword:
                last_filter = None
                
            save_admin_data()

            await app.send_message(
                ADMIN_ID,
                "📝 **Note:** The last active filter has been cleared because the filter message was deleted."
            )

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
    logger.info(f"Admin {ADMIN_ID} initiated broadcast.")
    if not message.reply_to_message:
        return await message.reply_text("📌 **Reply to a message** with `/broadcast` to send it to all users.")
    
    users_to_broadcast = [user_id for user_id in user_list if user_id not in banned_users]
    total_users = len(users_to_broadcast)
    
    if total_users == 0:
        return await message.reply_text("❌ **No users found in the database.**")

    progress_msg = await message.reply_text(f"📢 **Broadcasting to {total_users} users...** (0/{total_users})")
    
    sent_count = 0
    failed_count = 0

    for user_id in users_to_broadcast:
        try:
            await message.reply_to_message.copy(user_id, protect_content=True)
            sent_count += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await message.reply_to_message.copy(user_id, protect_content=True)
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send broadcast to user {user_id}: {e}")
            failed_count += 1
        
        if (sent_count + failed_count) % 10 == 0 and sent_count + failed_count > 0:
            try:
                await progress_msg.edit_text(
                    f"📢 **Broadcasting...**\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}\nTotal: {total_users}"
                )
            except MessageNotModified:
                pass
            except Exception as e:
                logger.error(f"Error updating progress message: {e}")
        
        await asyncio.sleep(0.1)
    
    try:
        await progress_msg.edit_text(
            f"✅ **Broadcast complete!**\nSent to {sent_count} users.\nFailed to send to {failed_count} users."
        )
    except:
        await message.reply_text(
            f"✅ **Broadcast complete!**\nSent to {sent_count} users.\nFailed to send to {failed_count} users."
        )

@app.on_message(filters.command("delete") & filters.private & filters.user(ADMIN_ID))
async def delete_cmd(client, message):
    global last_filter
    logger.info(f"Admin {ADMIN_ID} used /delete command.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Please provide a keyword to delete.**\nExample: `/delete python`")

    keyword = args[1].lower()
    if keyword in filters_dict:
        del filters_dict[keyword]
        if filters_collection:
            filters_collection.delete_one({"_id": keyword})
        if keyword in autodelete_filters:
            del autodelete_filters[keyword]
        
        if last_filter == keyword:
            last_filter = None

        save_admin_data()
        await message.reply_text(f"🗑️ **Filter '{keyword}' and its associated files have been deleted.**", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(f"❌ **Filter '{keyword}' not found.**")

@app.on_message(filters.private & filters.user(ADMIN_ID) & ~filters.command(["start", "broadcast", "delete", "ban", "unban", "add_channel", "delete_channel", "restrict", "auto_delete", "channel_id"]))
async def handle_conversational_input(client, message):
    user_id = message.from_user.id
    logger.info(f"Admin {user_id} sent a conversational message. State: {user_states.get(user_id)}")
    
    if user_id in user_states:
        state = user_states[user_id]
        
        if state["command"] == "channel_id_awaiting_message":
            if message.forward_from_chat:
                chat_id = message.forward_from_chat.id
                chat_type = message.forward_from_chat.type
                chat_title = message.forward_from_chat.title if message.forward_from_chat.title else "N/A"
                
                response = (
                    f"✅ **সফলভাবে চ্যানেল আইডি পাওয়া গেছে!**\n\n🆔 **Chat ID:** `{chat_id}`\n📝 **Chat Type:** `{chat_type}`\n🔖 **Chat Title:** `{chat_title}`"
                )
                await message.reply_text(response, parse_mode=ParseMode.MARKDOWN)
            else:
                await message.reply_text("❌ **ভুল মেসেজ!**\n\nঅনুগ্রহ করে একটি চ্যানেল থেকে **সরাসরি ফরওয়ার্ড করা** মেসেজ পাঠান।")
            del user_states[user_id]
            save_admin_data()
            return

        if state["command"] == "add_channel":
            if state["step"] == "awaiting_name":
                user_states[user_id]["channel_name"] = message.text
                user_states[user_id]["step"] = "awaiting_link"
                await message.reply_text("🔗 **এবার চ্যানেলের লিংক দিন।** (যেমন: `https://t.me/channel` অথবা `t.me/channel`)")
                save_admin_data()
            elif state["step"] == "awaiting_link":
                channel_link = message.text
                if not (channel_link.startswith('https://t.me/') or channel_link.startswith('t.me/')):
                    del user_states[user_id]
                    save_admin_data()
                    await message.reply_text("❌ **ভুল লিংক ফরম্যাট।** `/add_channel` দিয়ে আবার চেষ্টা করুন।")
                    return
                user_states[user_id]["channel_link"] = channel_link
                user_states[user_id]["step"] = "awaiting_id"
                await message.reply_text("🆔 **এবার চ্যানেলের আইডি দিন।** (যেমন: `-100123456789`)")
                save_admin_data()
            elif state["step"] == "awaiting_id":
                try:
                    channel_id = int(message.text)
                    channel_name = user_states[user_id]["channel_name"]
                    channel_link = user_states[user_id]["channel_link"]
                    
                    join_channels.append({'name': channel_name, 'link': channel_link, 'id': channel_id})
                    del user_states[user_id]
                    save_admin_data()
                    await message.reply_text(f"✅ **চ্যানেল '{channel_name}' সফলভাবে যুক্ত হয়েছে!**")
                except ValueError:
                    del user_states[user_id]
                    save_admin_data()
                    await message.reply_text("❌ **ভুল আইডি ফরম্যাট।** অনুগ্রহ করে একটি সংখ্যা দিন। `/add_channel` দিয়ে আবার চেষ্টা করুন।")

@app.on_message(filters.command("add_channel") & filters.private & filters.user(ADMIN_ID))
async def add_channel_cmd(client, message):
    user_id = message.from_user.id
    logger.info(f"Admin {user_id} used /add_channel command.")
    user_states[user_id] = {"command": "add_channel", "step": "awaiting_name"}
    save_admin_data()
    await message.reply_text("📝 **চ্যানেলটির নাম লিখুন।**")

@app.on_message(filters.command("delete_channel") & filters.private & filters.user(ADMIN_ID))
async def delete_channel_cmd(client, message):
    global join_channels
    logger.info(f"Admin {ADMIN_ID} used /delete_channel command.")
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
        save_admin_data()
        await message.reply_text(f"🗑️ **চ্যানেলটি সফলভাবে মুছে ফেলা হয়েছে।**")
    else:
        await message.reply_text(f"❌ **এই আইডি বা লিংকের কোনো চ্যানেল খুঁজে পাওয়া যায়নি।**")
    
@app.on_message(filters.command("restrict") & filters.private & filters.user(ADMIN_ID))
async def restrict_cmd(client, message):
    global restrict_status
    logger.info(f"Admin {ADMIN_ID} used /restrict command.")
    restrict_status = not restrict_status
    save_admin_data()
    status_text = "ON" if restrict_status else "OFF"
    await message.reply_text(f"🔒 **Message forwarding restriction is now {status_text}.**")
    
@app.on_message(filters.command("ban") & filters.private & filters.user(ADMIN_ID))
async def ban_cmd(client, message):
    logger.info(f"Admin {ADMIN_ID} used /ban command.")
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
        save_user_data(user_id_to_ban, is_banned=True)
        await message.reply_text(f"✅ **User `{user_id_to_ban}` has been banned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMIN_ID))
async def unban_cmd(client, message):
    logger.info(f"Admin {ADMIN_ID} used /unban command.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("📌 **Usage:** `/unban <user_id>`")
    
    try:
        user_id_to_unban = int(args[1])
        if user_id_to_unban not in banned_users:
            return await message.reply_text("⚠️ **This user is not banned.**")
        
        banned_users.remove(user_id_to_unban)
        save_user_data(user_id_to_unban, is_banned=False)
        await message.reply_text(f"✅ **User `{user_id_to_unban}` has been unbanned.**")
    except ValueError:
        await message.reply_text("❌ **Invalid User ID.** Please provide a numeric user ID.")

@app.on_message(filters.command("auto_delete") & filters.private & filters.user(ADMIN_ID))
async def auto_delete_cmd(client, message):
    global last_filter
    logger.info(f"Admin {ADMIN_ID} used /auto_delete command.")
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
            save_admin_data()
            await message.reply_text(f"🗑️ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট বন্ধ করা হয়েছে।**")
        else:
            await message.reply_text(f"⚠️ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট আগেই বন্ধ ছিল।**")
    else:
        autodelete_filters[last_filter] = autodelete_time
        save_admin_data()
        await message.reply_text(f"✅ **সক্রিয় ফিল্টারের জন্য অটো-ডিলিট {time_str} তে সেট করা হয়েছে।**")

@app.on_callback_query(filters.regex("check_join_status"))
async def check_join_status_callback(client, callback_query):
    user_id = callback_query.from_user.id
    logger.info(f"User {user_id} clicked 'Try Again' button.")
    if await is_user_member(client, user_id):
        await callback_query.message.edit_text("✅ **You have successfully joined the channels!** Please send the link again to get your files.", reply_markup=None)
    else:
        buttons = []
        for channel in join_channels:
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
    logger.info(f"Admin {user_id} used /channel_id command.")
    user_states[user_id] = {"command": "channel_id_awaiting_message"}
    save_admin_data()
    await message.reply_text("➡️ **অনুগ্রহ করে একটি চ্যানেল থেকে একটি মেসেজ এখানে ফরওয়ার্ড করুন।**\n\nআমি সেই মেসেজ থেকে চ্যানেলের আইডি বের করে দেব।")

# --- Web Server for Pinging (Keep-Alive) ---
async def handle_ping(request):
    """Simple handler for pinging service."""
    return web.Response(text="Bot is awake!")

async def start_web_server():
    """Starts the aiohttp web server."""
    port = int(os.getenv("PORT", 8080))
    app_web = web.Application()
    app_web.router.add_get("/", handle_ping)
    app_runner = web.AppRunner(app_web)
    await app_runner.setup()
    site = web.TCPSite(app_runner, '0.0.0.0', port)
    logger.info(f"Web server started on port {port}")
    await site.start()

# --- Bot start up ---
async def main():
    global db_client, db, filters_collection, users_collection, admin_data_collection
    
    logger.info("Starting TA File Share Bot...")

    try:
        db_client = MongoClient(MONGODB_URI)
        db_name = "ta_file_share"
        db = db_client.get_database(db_name)
        
        filters_collection = db.get_collection("filters")
        users_collection = db.get_collection("users")
        admin_data_collection = db.get_collection("admin_data")
        logger.info("Connected to MongoDB successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        exit(1)

    await load_data_from_mongodb()
    
    await asyncio.gather(
        app.start(),
        start_web_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
