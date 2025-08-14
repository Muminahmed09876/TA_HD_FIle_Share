import os
import asyncio
from pyrogram import Client, filters, types
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message
)
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request

# ENV/Config
API_ID = int(os.getenv('API_ID', 'YOUR_API_ID'))
API_HASH = os.getenv('API_HASH', 'YOUR_API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN')
MONGODB_URL = os.getenv('MONGODB_URL', 'YOUR_MONGODB_URL')

# MongoDB setup
mongo = MongoClient(MONGODB_URL)
db = mongo['file_share_bot']

# Collections
users_col = db.users
filters_col = db.filters
channels_col = db.channels
config_col = db.config
logs_col = db.logs
bans_col = db.bans

# Pyrogram Client
app = Client("file_share_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Flask App for Health/Ping
flask_app = Flask(__name__)

@flask_app.route("/ping", methods=["GET"])
def ping():
    return {"status": "ok", "message": "Bot is running!", "time": datetime.utcnow().isoformat()}

# Helper functions

def is_admin(user_id):
    config = config_col.find_one({"_id": "admins"})
    if not config: return False
    return user_id in config.get("admin_ids", [])

def get_config(name, default=None):
    c = config_col.find_one({"_id": name})
    return c.get("value", default) if c else default

def set_config(name, value):
    config_col.update_one({"_id": name}, {"$set": {"value": value}}, upsert=True)

def log_event(event):
    logs_col.insert_one({
        "event": event,
        "time": datetime.utcnow()
    })

def is_banned(user_id):
    return bans_col.find_one({"user_id": user_id}) is not None

# COMMANDS

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message: Message):
    user_id = message.from_user.id
    if is_admin(user_id):
        text = "🛡️ Admin Panel\n\nCommands:\n" \
               "/filter\n/delete_filter\n/add_channel\n/delete_channel\n/auto_delete\n/forward_restrict\n/channel_id\n/broadcast\n/ban\n/unban\n/ping\n"
        await message.reply(text)
    else:
        text = "👋 Welcome to File Share Bot!\n\nSend or click your file link to access files. Make sure you’ve joined required channels if any."
        await message.reply(text)
    users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
    log_event(f"/start by {user_id}")

@app.on_message(filters.command("ping") & filters.private)
async def ping_cmd(client, message: Message):
    await message.reply(
        f"🏓 Pong!\nBot is running.\nServer Time (UTC): {datetime.utcnow().isoformat()}"
    )

@app.on_message(filters.command("filter") & filters.private)
async def filter_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return await message.reply("Only admin can use this command.")
    await message.reply("Filter-এর নাম দিন:")

    def name_filter(m): return m.from_user.id == user_id
    name_msg = await app.listen(message.chat.id, filters=name_filter)
    filter_name = name_msg.text.strip()

    if filters_col.find_one({"name": filter_name}):
        await message.reply("এই filter আগে থেকে আছে, নতুন করে ব্যবহার করা যাবে না।")
        return

    await message.reply("এখন ফাইল পাঠান (একাধিক ফাইল পাঠাতে পারেন):")
    files = []
    for _ in range(10):  # Max 10 files, can adjust
        file_msg = await app.listen(message.chat.id, filters=name_filter, timeout=60)
        if file_msg.document or file_msg.video or file_msg.photo:
            files.append(file_msg)
            await file_msg.reply("ফাইল গ্রহণ হয়েছে। আরও পাঠাতে পারেন, না হলে /done লিখুন।")
        elif file_msg.text == "/done":
            break

    if not files:
        await message.reply("কোনো ফাইল পাওয়া যায়নি।")
        return

    # Save filter to MongoDB
    file_ids = []
    for f in files:
        file_ids.append(f.message_id)
    filters_col.insert_one({
        "name": filter_name,
        "files": file_ids,
        "created_by": user_id,
        "created_at": datetime.utcnow()
    })

    # Forward files to File Store Channel
    store_channel = get_config("file_store_channel")
    if store_channel:
        for f in files:
            await f.forward(store_channel)
    link = f"https://t.me/{client.me.username}?start={filter_name}"
    await message.reply(f"Filter saved! Link:\n{link}")

@app.on_message(filters.command("delete_filter") & filters.private)
async def delete_filter_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return await message.reply("Only admin can use this command.")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("ফিল্টার নাম দিন: /delete_filter <filter_name>")
    filter_name = args[1].strip()
    res = filters_col.delete_one({"name": filter_name})
    if res.deleted_count:
        await message.reply("Filter deleted.")
    else:
        await message.reply("Filter পাওয়া যায়নি।")

@app.on_message(filters.command("add_channel") & filters.private)
async def add_channel_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    await message.reply("Channel-এর নাম দিন:")
    name_msg = await app.listen(message.chat.id, filters=lambda m: m.from_user.id == user_id)
    channel_name = name_msg.text.strip()
    await message.reply("Channel link দিন:")
    link_msg = await app.listen(message.chat.id, filters=lambda m: m.from_user.id == user_id)
    channel_link = link_msg.text.strip()
    if channels_col.find_one({"link": channel_link}):
        await message.reply("Channel আগেই add করা হয়েছে।")
        return
    channels_col.insert_one({"name": channel_name, "link": channel_link})
    await message.reply("Channel added!")

@app.on_message(filters.command("delete_channel") & filters.private)
async def delete_channel_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("Channel link দিন: /delete_channel <channel_link>")
    channel_link = args[1].strip()
    res = channels_col.delete_one({"link": channel_link})
    if res.deleted_count:
        await message.reply("Channel deleted.")
    else:
        await message.reply("Channel পাওয়া যায়নি।")

@app.on_message(filters.command("auto_delete") & filters.private)
async def auto_delete_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("ON", callback_data="auto_delete_on"),
         InlineKeyboardButton("OFF", callback_data="auto_delete_off")]
    ])
    await message.reply("Auto delete status:", reply_markup=btns)

@app.on_callback_query(filters.regex(r"auto_delete_(on|off)"))
async def auto_delete_cb(client, callback_query):
    user_id = callback_query.from_user.id
    if not is_admin(user_id): return await callback_query.answer("Only admin!")
    status = callback_query.data.split("_")[2]
    set_config("auto_delete", status == "on")
    await callback_query.answer(f"Auto delete {'enabled' if status == 'on' else 'disabled'}.")

@app.on_message(filters.command("forward_restrict") & filters.private)
async def forward_restrict_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("ON", callback_data="forward_restrict_on"),
         InlineKeyboardButton("OFF", callback_data="forward_restrict_off")]
    ])
    await message.reply("Forward restrict status:", reply_markup=btns)

@app.on_callback_query(filters.regex(r"forward_restrict_(on|off)"))
async def forward_restrict_cb(client, callback_query):
    user_id = callback_query.from_user.id
    if not is_admin(user_id): return await callback_query.answer("Only admin!")
    status = callback_query.data.split("_")[2]
    set_config("forward_restrict", status == "on")
    await callback_query.answer(f"Forward restrict {'enabled' if status == 'on' else 'disabled'}.")

@app.on_message(filters.command("ban") & filters.private)
async def ban_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("User id দিন: /ban <user_id>")
    ban_id = int(args[1].strip())
    bans_col.insert_one({"user_id": ban_id})
    await message.reply("User banned.")

@app.on_message(filters.command("unban") & filters.private)
async def unban_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply("User id দিন: /unban <user_id>")
    ban_id = int(args[1].strip())
    bans_col.delete_one({"user_id": ban_id})
    await message.reply("User unbanned.")

@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    await message.reply("Broadcast মেসেজ লিখুন:")
    msg = await app.listen(message.chat.id, filters=lambda m: m.from_user.id == user_id)
    text = msg.text
    users = [u["user_id"] for u in users_col.find()]
    for uid in users:
        try:
            await app.send_message(uid, text)
        except: continue
    await message.reply("Broadcast sent.")

@app.on_message(filters.command("channel_id") & filters.private)
async def channel_id_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    channels = channels_col.find()
    text = "Channels:\n"
    for c in channels:
        text += f"{c['name']}: {c['link']}\n"
    await message.reply(text)

@app.on_message(filters.command("add_log_channel") & filters.private)
async def add_log_channel_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    await message.reply("Log channel id দিন:")
    log_msg = await app.listen(message.chat.id, filters=lambda m: m.from_user.id == user_id)
    log_channel_id = log_msg.text.strip()
    set_config("log_channel", log_channel_id)
    await message.reply("Log channel added.")

@app.on_message(filters.command("add_file_store_channel") & filters.private)
async def add_file_store_channel_cmd(client, message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id): return await message.reply("Only admin!")
    await message.reply("File store channel id দিন:")
    store_msg = await app.listen(message.chat.id, filters=lambda m: m.from_user.id == user_id)
    store_channel_id = store_msg.text.strip()
    set_config("file_store_channel", store_channel_id)
    await message.reply("File store channel added.")

# DEP LINK & FILE ACCESS

@app.on_message(filters.private & filters.regex(r"^/start\s+(.+)"))
async def filter_access(client, message: Message):
    user_id = message.from_user.id
    if is_banned(user_id):
        return await message.reply("You are banned from using this bot.")

    filter_name = message.text.split(maxsplit=1)[1].strip()
    f = filters_col.find_one({"name": filter_name})
    if not f:
        return await message.reply("Filter পাওয়া যায়নি।")
    # Check channel join
    channels = list(channels_col.find())
    if channels:
        missing = []
        for c in channels:
            try:
                member = await app.get_chat_member(c["link"], user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    missing.append(c)
            except: missing.append(c)
        if missing:
            join_btns = [
                [InlineKeyboardButton(f"Join {c['name']}", url=c["link"])] for c in missing
            ]
            join_btns.append([InlineKeyboardButton("Try Again", url=message.text)])
            await message.reply("You must join the required channel(s) to access files.", reply_markup=InlineKeyboardMarkup(join_btns))
            return

    # Send files
    auto_delete = get_config("auto_delete", False)
    forward_restrict = get_config("forward_restrict", False)
    for fid in f["files"]:
        # File store channel থেকে নিতে চাইলে: await app.copy_message(...)
        sent = await app.send_message(user_id, f"File for filter: {filter_name}")
        if auto_delete:
            # Schedule delete after 24h
            asyncio.create_task(delete_later(sent.chat.id, sent.message_id, 24*3600))
            await app.send_message(user_id, "⏳ This file will be deleted after 24 hours.")
        # Forward restrict logic (pyrogram 2.x: disable_forward=True)
        # But Telegram API sometimes restricts - use send_document(..., disable_forward=True)
    log_channel = get_config("log_channel")
    if log_channel:
        await app.send_message(log_channel, f"User {user_id} accessed filter {filter_name}")

async def delete_later(chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except: pass

# Run Bot & Flask (use threading or gunicorn for prod)
if __name__ == "__main__":
    import threading
    def run_bot():
        app.run()
    def run_flask():
        flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    threading.Thread(target=run_bot).start()
    threading.Thread(target=run_flask).start()
