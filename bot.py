from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient, ASCENDING
from flask import Flask
from threading import Thread
import os
import re
from datetime import datetime
import asyncio
import urllib.parse
from fuzzywuzzy import process
from concurrent.futures import ThreadPoolExecutor

# Configs
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
RESULTS_COUNT = int(os.getenv("RESULTS_COUNT", 10))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
UPDATE_CHANNEL = os.getenv("UPDATE_CHANNEL", "https://t.me/CTGMovieOfficial")
START_PIC = os.getenv("START_PIC", "https://i.ibb.co/prnGXMr3/photo-2025-05-16-05-15-45-7504908428624527364.jpg")

app = Client("movie_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# MongoDB setup
mongo = MongoClient(DATABASE_URL)
db = mongo["movie_bot"]
movies_col = db["movies"]
feedback_col = db["feedback"]
stats_col = db["stats"]
users_col = db["users"]
settings_col = db["settings"]

# Indexing - Optimized for faster search
# FIX: Removed 'language' option as it's disallowed in your Atlas tier.
# MongoDB will use its default text index language (usually English).
movies_col.create_index([("title", "text")]) # Primary index for text search
movies_col.create_index("message_id")
movies_col.create_index("language")
movies_col.create_index([("title_clean", ASCENDING)])
movies_col.create_index([("language", ASCENDING), ("title_clean", ASCENDING)])

# Flask App for health check
flask_app = Flask(__name__)
@flask_app.route("/")
def home():
    return "Bot is running!"
Thread(target=lambda: flask_app.run(host="0.0.0.0", port=8080)).start()

# Initialize a global ThreadPoolExecutor for running blocking functions (like fuzzywuzzy)
thread_pool_executor = ThreadPoolExecutor(max_workers=5)

# Helpers
def clean_text(text):
    return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

# FIX: Modified extract_language to return None if language is not found.
# This prevents "Unknown" values from being inserted into the 'language' field.
def extract_language(text):
    langs = ["Bengali", "Hindi", "English"]
    return next((lang for lang in langs if lang.lower() in text.lower()), None)

async def delete_message_later(chat_id, message_id, delay=600):
    await asyncio.sleep(delay)
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception as e:
        if "MESSAGE_ID_INVALID" not in str(e) and "MESSAGE_DELETE_FORBIDDEN" not in str(e):
            print(f"Error deleting message {message_id} in chat {chat_id}: {e}")

# IMPORTANT: This function must be synchronous (not async) because it will be run in ThreadPoolExecutor
def find_corrected_matches(query_clean, all_movie_titles_data, score_cutoff=70, limit=5):
    if not all_movie_titles_data:
        return []

    choices = [item["title_clean"] for item in all_movie_titles_data]
    
    matches_raw = process.extract(query_clean, choices, limit=limit)

    corrected_suggestions = []
    for matched_clean_title, score in matches_raw:
        if score >= score_cutoff:
            for movie_data in all_movie_titles_data:
                if movie_data["title_clean"] == matched_clean_title:
                    corrected_suggestions.append({
                        "title": movie_data["original_title"],
                        "message_id": movie_data["message_id"],
                        "language": movie_data["language"]
                    })
                    break
    return corrected_suggestions

# Main message processing for new posts in channel
@app.on_message(filters.chat(CHANNEL_ID))
async def save_post(_, msg: Message):
    text = msg.text or msg.caption
    if not text:
        return

    movie_to_save = {
        "message_id": msg.id,
        "title": text,
        "date": msg.date,
        "year": extract_year(text),
        "language": extract_language(text), # Will now be None if not found
        "title_clean": clean_text(text)
    }
    
    # Use update_one with upsert=True to insert or update the movie
    # This also helps in avoiding duplicate entries if the bot restarts and re-processes old messages
    result = movies_col.update_one({"message_id": msg.id}, {"$set": movie_to_save}, upsert=True)

    # Check if a new document was inserted (not just updated) to send notification
    if result.upserted_id is not None:
        setting = settings_col.find_one({"key": "global_notify"})
        if setting and setting.get("value"):
            for user in users_col.find({"notify": {"$ne": False}}):
                try:
                    await app.send_message(
                        user["_id"],
                        f"নতুন মুভি আপলোড হয়েছে:\n**{text.splitlines()[0][:100]}**\nএখনই সার্চ করে দেখুন!"
                    )
                    await asyncio.sleep(0.05)
                except Exception as e:
                    if "PEER_ID_INVALID" in str(e) or "USER_IS_BOT" in str(e) or "USER_DEACTIVATED_REQUIRED" in str(e):
                        print(f"Skipping notification to invalid/blocked user {user['_id']}: {e}")
                    else:
                        print(f"Failed to send notification to user {user['_id']}: {e}")

@app.on_message(filters.command("start"))
async def start(_, msg: Message):
    users_col.update_one(
        {"_id": msg.from_user.id},
        {"$set": {"joined": datetime.utcnow(), "notify": True}},
        upsert=True
    )
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("Update Channel", url=UPDATE_CHANNEL)],
        [InlineKeyboardButton("Contact Admin", url="https://t.me/ctgmovies23")]
    ])
    await msg.reply_photo(photo=START_PIC, caption="Send me a movie name to search.", reply_markup=btns)

@app.on_message(filters.command("feedback") & filters.private)
async def feedback(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Please write something after /feedback.")
    feedback_col.insert_one({
        "user": msg.from_user.id,
        "text": msg.text.split(None, 1)[1],
        "time": datetime.utcnow()
    })
    m = await msg.reply("Thanks for your feedback!")
    asyncio.create_task(delete_message_later(m.chat.id, m.id, delay=30))

@app.on_message(filters.command("broadcast") & filters.user(ADMIN_IDS))
async def broadcast(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("Usage: /broadcast Your message here")
    count = 0
    message_to_send = msg.text.split(None, 1)[1]
    for user in users_col.find():
        try:
            await app.send_message(user["_id"], message_to_send)
            count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            if "PEER_ID_INVALID" in str(e) or "USER_IS_BLOCKED" in str(e) or "USER_BOT" in str(e) or "USER_DEACTIVATED_REQUIRED" in str(e):
                print(f"Skipping broadcast to invalid/blocked user {user['_id']}: {e}")
            else:
                print(f"Failed to broadcast to user {user['_id']}: {e}")
    await msg.reply(f"Broadcast sent to {count} users.")

@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def stats(_, msg: Message):
    await msg.reply(
        f"Users: {users_col.count_documents({})}\n"
        f"Movies: {movies_col.count_documents({})}\n"
        f"Feedbacks: {feedback_col.count_documents({})}"
    )

@app.on_message(filters.command("notify") & filters.user(ADMIN_IDS))
async def notify_command(_, msg: Message):
    if len(msg.command) != 2 or msg.command[1] not in ["on", "off"]:
        return await msg.reply("ব্যবহার: /notify on  অথবা  /notify off")
    new_value = True if msg.command[1] == "on" else False
    settings_col.update_one(
        {"key": "global_notify"},
        {"$set": {"value": new_value}},
        upsert=True
    )
    status = "enabled" if new_value else "disabled"
    await msg.reply(f"✅ Global notifications {status}!")

@app.on_message(filters.command("delete_movie") & filters.user(ADMIN_IDS))
async def delete_specific_movie(_, msg: Message):
    if len(msg.command) < 2:
        return await msg.reply("অনুগ্রহ করে মুভির টাইটেল দিন। ব্যবহার: `/delete_movie <Movie Title>`")
    
    movie_title_to_delete = msg.text.split(None, 1)[1].strip()
    
    movie_to_delete = movies_col.find_one({"$text": {"$search": movie_title_to_delete}})

    if not movie_to_delete:
        cleaned_title_to_delete = clean_text(movie_title_to_delete)
        movie_to_delete = movies_col.find_one({"title_clean": {"$regex": f"^{re.escape(cleaned_title_to_delete)}$", "$options": "i"}})

    if movie_to_delete:
        movies_col.delete_one({"_id": movie_to_delete["_id"]})
        await msg.reply(f"মুভি **{movie_to_delete['title']}** সফলভাবে ডিলিট করা হয়েছে।")
    else:
        await msg.reply(f"**{movie_title_to_delete}** নামের কোনো মুভি খুঁজে পাওয়া যায়নি।")

@app.on_message(filters.command("delete_all_movies") & filters.user(ADMIN_IDS))
async def delete_all_movies_command(_, msg: Message):
    confirmation_button = InlineKeyboardMarkup([
        [InlineKeyboardButton("হ্যাঁ, সব ডিলিট করুন", callback_data="confirm_delete_all_movies")],
        [InlineKeyboardButton("না, বাতিল করুন", callback_data="cancel_delete_all_movies")]
    ])
    await msg.reply("আপনি কি নিশ্চিত যে আপনি ডাটাবেস থেকে **সব মুভি** ডিলিট করতে চান? এই প্রক্রিয়াটি অপরিবর্তনীয়!", reply_markup=confirmation_button)

@app.on_message(filters.text)
async def search(_, msg: Message):
    raw_query = msg.text.strip()
    user_id = msg.from_user.id

    users_col.update_one(
        {"_id": user_id},
        {"$set": {"last_search": datetime.utcnow()}},
        upsert=True
    )

    loading_message = await msg.reply("🔎 লোড হচ্ছে, অনুগ্রহ করে অপেক্ষা করুন...")

    # --- Optimized Search Logic ---

    # 1. Try to find direct matches using MongoDB's text index
    direct_suggestions = list(movies_col.find(
        {"$text": {"$search": raw_query}},
        {"title": 1, "message_id": 1, "language": 1, "score": {"$meta": "textScore"}}
    ).sort([("score", {"$meta": "textScore"})]).limit(RESULTS_COUNT))

    if direct_suggestions:
        await loading_message.delete()
        buttons = []
        for m in direct_suggestions:
            buttons.append([InlineKeyboardButton(m["title"][:40], callback_data=f"movie_{m['message_id']}")])
        
        lang_buttons = [
            InlineKeyboardButton("Bengali", callback_data=f"lang_Bengali_{clean_text(raw_query)}"),
            InlineKeyboardButton("Hindi", callback_data=f"lang_Hindi_{clean_text(raw_query)}"),
            InlineKeyboardButton("English", callback_data=f"lang_English_{clean_text(raw_query)}")
        ]
        buttons.append(lang_buttons)
        
        m = await msg.reply("আপনার মুভির নাম মিলতে পারে, নিচের থেকে সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
        asyncio.create_task(delete_message_later(m.chat.id, m.id))
        return

    # 2. If no direct matches from text index, try using title_clean for exact phrase/starts-with match
    query_clean = clean_text(raw_query)
    direct_clean_suggestions = list(movies_col.find(
        {"title_clean": {"$regex": f"^{re.escape(query_clean)}", "$options": "i"}},
        {"title": 1, "message_id": 1, "language": 1}
    ).limit(RESULTS_COUNT))

    if direct_clean_suggestions:
        await loading_message.delete()
        buttons = []
        for m in direct_clean_suggestions:
            buttons.append([InlineKeyboardButton(m["title"][:40], callback_data=f"movie_{m['message_id']}")])
        
        lang_buttons = [
            InlineKeyboardButton("Bengali", callback_data=f"lang_Bengali_{query_clean}"),
            InlineKeyboardButton("Hindi", callback_data=f"lang_Hindi_{query_clean}"),
            InlineKeyboardButton("English", callback_data=f"lang_English_{query_clean}")
        ]
        buttons.append(lang_buttons)
        
        m = await msg.reply("আপনার মুভির নাম মিলতে পারে, নিচের থেকে সিলেক্ট করুন:", reply_markup=InlineKeyboardMarkup(buttons))
        asyncio.create_task(delete_message_later(m.chat.id, m.id))
        return

    # 3. If still no results, perform a limited fuzzy search on potential matches
    potential_fuzzy_matches_cursor = movies_col.find(
        {"title_clean": {"$regex": query_clean, "$options": "i"}},
        {"title": 1, "message_id": 1, "language": 1, "title_clean": 1}
    ).limit(50)

    potential_fuzzy_matches = list(potential_fuzzy_matches_cursor)

    fuzzy_data_for_matching = [
        {"title_clean": m["title_clean"], "original_title": m["title"], "message_id": m["message_id"], "language": m["language"]}
        for m in potential_fuzzy_matches
    ]
    
    loop = asyncio.get_running_loop()
    corrected_suggestions = await loop.run_in_executor(
        thread_pool_executor,
        find_corrected_matches,
        query_clean,
        fuzzy_data_for_matching,
        70,
        RESULTS_COUNT
    )

    if corrected_suggestions:
        await loading_message.delete()
        buttons = []
        for m in corrected_suggestions[:RESULTS_COUNT]:
            buttons.append([InlineKeyboardButton(m["title"][:40], callback_data=f"movie_{m['message_id']}")])

        lang_buttons = [
            InlineKeyboardButton("Bengali", callback_data=f"lang_Bengali_{query_clean}"),
            InlineKeyboardButton("Hindi", callback_data=f"lang_Hindi_{query_clean}"),
            InlineKeyboardButton("English", callback_data=f"lang_English_{query_clean}")
        ]
        buttons.append(lang_buttons)

        m = await msg.reply(
            "আপনার সার্চের সাথে সরাসরি কোনো ফলাফল মেলেনি। আপনি কি এটি বোঝাতে চেয়েছিলেন?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        asyncio.create_task(delete_message_later(m.chat.id, m.id))
        return

    # 4. If no direct matches and no spell correction suggestions
    await loading_message.delete()
    
    Google_Search_url = "https://www.google.com/search?q=" + urllib.parse.quote(raw_query)
    google_button = InlineKeyboardMarkup([
        [InlineKeyboardButton("Search on Google", url=Google_Search_url)]
    ])
    
    alert = await msg.reply(
        "দুঃখিত! আপনার খোঁজা মুভিটি খুঁজে পাওয়া যায়নি। অ্যাডমিনকে জানানো হয়েছে। নিচের বাটনে ক্লিক করে গুগলে সার্চ করতে পারেন।",
        reply_markup=google_button
    )
    asyncio.create_task(delete_message_later(alert.chat.id, alert.id))

    btn_admin_request = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ মুভি আছে", callback_data=f"has_{msg.chat.id}_{msg.id}_{raw_query}"),
            InlineKeyboardButton("❌ নেই", callback_data=f"no_{msg.chat.id}_{msg.id}_{raw_query}")
        ],
        [
            InlineKeyboardButton("⏳ শীঘ্রই আসবে", callback_data=f"soon_{msg.chat.id}_{msg.id}_{raw_query}"),
            InlineKeyboardButton("✏️ ভুল নাম", callback_data=f"wrong_{msg.chat.id}_{msg.id}_{raw_query}")
        ]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            await app.send_message(
                admin_id,
                f"❗ ইউজার `{msg.from_user.id}` (`{msg.from_user.first_name}`) খুঁজেছে: **{raw_query}**\nফলাফল পাওয়া যায়নি। নিচে বাটন থেকে উত্তর দিন।",
                reply_markup=btn_admin_request
            )
        except Exception as e:
            print(f"Failed to send admin message to {admin_id}: {e}")

@app.on_callback_query()
async def callback_handler(_, cq: CallbackQuery):
    data = cq.data

    if data == "confirm_delete_all_movies":
        movies_col.delete_many({})
        await cq.message.edit_text("✅ ডাটাবেস থেকে সব মুভি সফলভাবে ডিলিট করা হয়েছে।")
        await cq.answer("সব মুভি ডিলিট করা হয়েছে।")
    elif data == "cancel_delete_all_movies":
        await cq.message.edit_text("❌ সব মুভি ডিলিট করার প্রক্রিয়া বাতিল করা হয়েছে।")
        await cq.answer("বাতিল করা হয়েছে।")

    elif data.startswith("movie_"):
        mid = int(data.split("_")[1])
        try:
            fwd = await app.forward_messages(cq.message.chat.id, CHANNEL_ID, mid)
            asyncio.create_task(delete_message_later(cq.message.chat.id, fwd.id))
            await cq.answer("মুভি পাঠানো হয়েছে।")
        except Exception as e:
            await cq.answer("মুভিটি ফরওয়ার্ড করা যায়নি।", show_alert=True)
            print(f"Error forwarding message: {e}")

    elif data.startswith("lang_"):
        _, lang, query_clean = data.split("_", 2)
        
        potential_lang_matches_cursor = movies_col.find(
            {"language": lang, "title_clean": {"$regex": query_clean, "$options": "i"}},
            {"title": 1, "message_id": 1, "title_clean": 1}
        ).limit(50)

        potential_lang_matches = list(potential_lang_matches_cursor)
        
        fuzzy_data_for_matching_lang = [
            {"title_clean": m["title_clean"], "original_title": m["title"], "message_id": m["message_id"], "language": lang}
            for m in potential_lang_matches
        ]
        
        loop = asyncio.get_running_loop()
        matches_filtered_by_lang = await loop.run_in_executor(
            thread_pool_executor,
            find_corrected_matches,
            query_clean,
            fuzzy_data_for_matching_lang,
            70,
            RESULTS_COUNT
        )

        if matches_filtered_by_lang:
            buttons = [
                [InlineKeyboardButton(m["title"][:40], callback_data=f"movie_{m['message_id']}")]
                for m in matches_filtered_by_lang[:RESULTS_COUNT]
            ]
            await cq.message.edit_text(
                f"ফলাফল ({lang}) - নিচের থেকে সিলেক্ট করুন:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await cq.answer("এই ভাষায় কিছু পাওয়া যায়নি।", show_alert=True)
        await cq.answer()

    elif "_" in data:
        parts = data.split("_", 3)
        if len(parts) == 4:
            action, uid, mid, raw_query = parts
            uid = int(uid)
            responses = {
                "has": f"✅ @{cq.from_user.username or cq.from_user.first_name} জানিয়েছেন যে **{raw_query}** মুভিটি ডাটাবেজে আছে। সঠিক নাম লিখে আবার চেষ্টা করুন।",
                "no": f"❌ @{cq.from_user.username or cq.from_user.first_name} জানিয়েছেন যে **{raw_query}** মুভিটি ডাটাবেজে নেই।",
                "soon": f"⏳ @{cq.from_user.username or cq.from_user.first_name} জানিয়েছেন যে **{raw_query}** মুভিটি শীঘ্রই আসবে।",
                "wrong": f"✏️ @{cq.from_user.username or cq.from_user.first_name} বলছেন যে আপনি ভুল নাম লিখেছেন: **{raw_query}**।"
            }
            if action in responses:
                try:
                    m = await app.send_message(uid, responses[action])
                    asyncio.create_task(delete_message_later(m.chat.id, m.id, delay=30))
                    await cq.answer("অ্যাডমিনের পক্ষ থেকে উত্তর পাঠানো হয়েছে।")
                except Exception as e:
                    await cq.answer("ইউজারকে বার্তা পাঠাতে সমস্যা হয়েছে।", show_alert=True)
                    print(f"Error sending admin feedback message: {e}")
            else:
                await cq.answer("অকার্যকর কলব্যাক ডেটা।", show_alert=True)
        else:
            await cq.answer("অকার্যকর কলব্যাক ডেটা।", show_alert=True)

if __name__ == "__main__":
    print("Bot is starting...")
    app.run()
