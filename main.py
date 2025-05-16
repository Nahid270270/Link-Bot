from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread
import os
import re
from datetime import datetime
import asyncio

# Environment Variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", 10))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/CTGMovieOfficial")
START_PIC = os.getenv("START_PIC", "https://i.ibb.co/prnGXMr3/photo-2025-05-16-05-15-45-7504908428624527364.jpg")

# Pyrogram Client
app = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB Setup
mongo = MongoClient(DATABASE_URL)
db = mongo["movie_bot"]
movies_col = db["movies"]
feedback_col = db["feedback"]
stats_col = db["stats"]
users_col = db["users"]
settings_col = db["settings"]

# Ensure global_notify setting exists
if not settings_col.find_one({"key": "global_notify"}):
    settings_col.insert_one({"key": "global_notify", "value": True})

# Flask App for health check
flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return "Bot is running!"

def run():
    flask_app.run(host="0.0.0.0", port=8080)

# Helper functions
def extract_year(text):
    match = re.search(r"(19|20)\d{2}", text)
    return match.group() if match else None

def extract_language(text):
    langs = ["Bengali", "Bangla", "Hindi", "English"]
    return next((lang for lang in langs if lang.lower() in text.lower()), "Unknown")

def clean_text(text):
    return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

# Save new channel posts to DB & notify users
@app.on_message(filters.chat(CHANNEL_ID))
async def save_post(_, msg: Message):
    text = msg.text or msg.caption
    if not text:
        return

    movie = {
        "message_id": msg.id,
        "title": text,
        "date": msg.date,
        "type": "movie",
        "year": extract_year(text),
        "language": extract_language(text)
    }
    movies_col.update_one({"message_id": msg.id}, {"$set": movie}, upsert=True)

    setting = settings_col.find_one({"key": "global_notify"})
    if setting and setting["value"]:
        for user in users_col.find({"notify": {"$ne": False}}):
            try:
                await app.send_message(
                    user["_id"],
                    f"নতুন মুভি আপলোড হয়েছে:\n{text.splitlines()[0][:100]}\n\nএখনই সার্চ করে দেখুন!"
                )
            except:
                pass

# /start command handler
@app.on_message(filters.command("start") & (filters.private | filters.group))
async def start(_, msg):
    users_col.update_one({"_id": msg.from_user.id}, {"$set": {"joined": datetime.utcnow()}}, upsert=True)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Update Channel", url=UPDATE_CHANNEL)],
        [InlineKeyboardButton("Contact Admin", url="https://t.me/ctgmovies23")]
    ])
    reply = await msg.reply_photo(photo=START_PIC, caption="Send me a movie name to search.", reply_markup=buttons)
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /feedback command handler
@app.on_message(filters.command("feedback") & filters.private)
async def feedback(_, msg):
    if len(msg.command) < 2:
        reply = await msg.reply("Please write something after /feedback.")
    else:
        feedback_col.insert_one({
            "user": msg.from_user.id,
            "text": msg.text.split(None, 1)[1],
            "time": datetime.utcnow()
        })
        reply = await msg.reply("Thanks for your feedback!")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /broadcast command handler (admin only)
@app.on_message(filters.command("broadcast") & filters.user(ADMIN_IDS))
async def broadcast(_, msg):
    if len(msg.command) < 2:
        reply = await msg.reply("Usage: /broadcast Your message here")
    else:
        count = 0
        for user in users_col.find():
            try:
                await app.send_message(user["_id"], msg.text.split(None, 1)[1])
                count += 1
            except:
                pass
        reply = await msg.reply(f"Broadcast sent to {count} users.")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /stats command handler (admin only)
@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def stats(_, msg):
    reply = await msg.reply(
        f"Users: {users_col.count_documents({})}\n"
        f"Movies: {movies_col.count_documents({})}\n"
        f"Feedbacks: {feedback_col.count_documents({})}"
    )
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /notify command to toggle notifications for all users (admin only)
@app.on_message(filters.command("notify") & filters.user(ADMIN_IDS))
async def notify(_, msg):
    if len(msg.command) < 2 or msg.command[1] not in ["on", "off"]:
        reply = await msg.reply("Usage: /notify on or /notify off")
    else:
        users_col.update_many({}, {"$set": {"notify": msg.command[1] == "on"}})
        reply = await msg.reply(f"Notification turned {msg.command[1].upper()} for all users.")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /globalnotify command to toggle global notification (admin only)
@app.on_message(filters.command("globalnotify") & filters.user(ADMIN_IDS))
async def globalnotify(_, msg):
    if len(msg.command) < 2 or msg.command[1] not in ["on", "off"]:
        reply = await msg.reply("Usage: /globalnotify on or /globalnotify off")
    else:
        settings_col.update_one({"key": "global_notify"}, {"$set": {"value": msg.command[1] == "on"}})
        reply = await msg.reply(f"Global Notify turned {msg.command[1].upper()}")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /delete_all command to clear movies (admin only)
@app.on_message(filters.command("delete_all") & filters.user(ADMIN_IDS))
async def delete_all(_, msg):
    deleted = movies_col.delete_many({}).deleted_count
    reply = await msg.reply(f"{deleted} টি মুভি মুছে ফেলা হয়েছে।")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# /delete_movie command to delete a single movie by message_id (admin only)
@app.on_message(filters.command("delete_movie") & filters.user(ADMIN_IDS))
async def delete_one(_, msg):
    try:
        mid = int(msg.command[1])
        result = movies_col.delete_one({"message_id": mid})
        reply = await msg.reply("Deleted successfully." if result.deleted_count else "Movie not found.")
    except:
        reply = await msg.reply("Usage: /delete_movie message_id")
    await asyncio.sleep(30)
    await reply.delete()
    await msg.delete()

# Search handler
@app.on_message(filters.text & (filters.private | filters.group))
async def search(_, msg):
    raw_query = msg.text.strip()
    query = clean_text(raw_query)

    users_col.update_one({"_id": msg.from_user.id}, {"$set": {"last_search": datetime.utcnow()}}, upsert=True)

    all_movies = list(movies_col.find({}, {"title": 1, "message_id": 1}))
    exact_match = []
    suggestions = []

    for movie in all_movies:
        title = movie.get("title", "")
        title_clean = clean_text(title)

        if title_clean == query:
            exact_match.append(movie)
        elif query in title_clean:
            suggestions.append(movie)

    if exact_match:
        try:
            for m in exact_match[:RESULTS_COUNT]:
                fmsg = await app.forward_messages(msg.chat.id, CHANNEL_ID, m["message_id"])
                await asyncio.sleep(30)
                await fmsg.delete()
            await asyncio.sleep(30)
            await msg.delete()
        except:
            err = await msg.reply("মুভি পাঠাতে সমস্যা হয়েছে।")
            await asyncio.sleep(30)
            await err.delete()
            await msg.delete()
        return

    elif suggestions:
        buttons = []
        for movie in suggestions[:RESULTS_COUNT]:
            title = movie.get("title", "Unknown")
            mid = movie.get("message_id")
            buttons.append([InlineKeyboardButton(title[:40], callback_data=f"movie_{mid}")])
        reply = await msg.reply("আপনার মুভির নাম মিলতে পারে, নিচের থেকে সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
        await asyncio.sleep(30)
        await reply.delete()
        await msg.delete()
        return

    else:
        reply = await msg.reply("কোনও ফলাফল পাওয়া যায়নি। অ্যাডমিনকে জানানো হয়েছে।")
        await asyncio.sleep(30)
        await reply.delete()
        await msg.delete()

        btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("✔ মুভি আছে", callback_data=f"has_{msg.from_user.id}")],
            [InlineKeyboardButton("❌ নেই", callback_data=f"no_{msg.from_user.id}")],
            [InlineKeyboardButton("⏳ আসবে", callback_data=f"soon_{msg.from_user.id}")],
            [InlineKeyboardButton("✏️ ভুল নাম", callback_data=f"wrong_{msg.from_user.id}")]
        ])
        for admin_id in ADMIN_IDS:
            try:
                await app.send_message(
                    admin_id,
                    f"❗ ইউজার `{msg.from_user.id}` `{msg.from_user.first_name}` খুঁজেছে: **{raw_query}**\n\nফলাফল পাওয়া যায়নি। নিচে বাটন থেকে উত্তর দিন।",
                    reply_markup=btn
                )
            except:
                pass

# Callback Query handler
@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
    data = cq.data
    if data.startswith("movie_"):
        mid = int(data.split("_")[1])
        try:
            fmsg = await app.forward_messages(cq.message.chat.id, CHANNEL_ID, mid)
            await asyncio.sleep(30)
            await fmsg.delete()
            await cq.message.delete()
            await cq.answer()
        except:
            err = await cq.message.reply("মুভি পাঠাতে সমস্যা হয়েছে।")
            await asyncio.sleep(30)
            await err.delete()
            await cq.message.delete()
            await cq.answer()

    elif "_" in data:
        action, user_id = data.split("_")
        uid = int(user_id)
        responses = {
            "has": "✔ মুভিটি ডাটাবেজে আছে। নামটি সঠিকভাবে লিখে আবার চেষ্টা করুন।",
            "no": "❌ এই মুভিটি ডাটাবেজে নেই।",
            "soon": "⏳ এই মুভিটি শিগগির আসবে।",
            "wrong": "✏️ নামটি ভুল হয়েছে। আবার চেষ্টা করুন।"
        }
        if action in responses:
            try:
                reply = await app.send_message(uid, responses[action])
                await cq.answer("রিপ্লাই পাঠানো হয়েছে", show_alert=True)
                await asyncio.sleep(30)
                await reply.delete()
            except:
                await cq.answer("ইউজারকে মেসেজ পাঠানো যায়নি", show_alert=True)

if __name__ == "__main__":
    Thread(target=run).start()
    app.run()
