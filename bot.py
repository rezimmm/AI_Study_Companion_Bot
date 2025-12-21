
# Built By Rezim Titoria

import os
import asyncio
import aiohttp
import fitz

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.filters import Command


BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing in .env")
    exit()

if not GEMINI_KEY:
    print("❌ GEMINI_KEY missing in .env")
    exit()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()

user_mode = {}
user_pdf_text = {}
user_chapters = {}
user_bookmarks = {}      # <-- chapter-based bookmarks
user_quiz = {}

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧠 Summarize PDF")],
        [KeyboardButton(text="📚 MCQ / Quiz")],
        [KeyboardButton(text="🔖 Bookmarks")],
        [KeyboardButton(text="ℹ Help")]
    ],
    resize_keyboard=True
)


# ---------------- GEMINI ----------------
async def ask_gemini(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={
            "contents":[{"parts":[{"text":prompt}]}]
        }) as res:
            data = await res.json()

            if "candidates" not in data:
                return "⚠ AI failed."

            return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------- PDF ----------------
async def extract_pdf(msg: Message):
    file = await bot.get_file(msg.document.file_id)
    file_path = file.file_path
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as res:
            data = await res.read()

    text = ""
    pdf = fitz.open(stream=data, filetype="pdf")
    for p in pdf:
        text += p.get_text()

    return text


# --------------- START -----------------
@router.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "👋 Welcome to AI Study Companion\nChoose an option below 👇",
        reply_markup=menu
    )


# ------------ MODES -------------
@router.message(F.text == "🧠 Summarize PDF")
async def enable_summary(msg: Message):
    user_mode[msg.from_user.id] = "summary"
    await msg.answer("📄 Upload PDF to summarize by chapters.")


@router.message(F.text == "📚 MCQ / Quiz")
async def enable_quiz(msg: Message):
    user_mode[msg.from_user.id] = "quiz"
    await msg.answer("📄 Upload PDF to generate MCQ quiz.")


# ------------ HANDLE PDF -------------
@router.message(F.document)
async def handle_pdf(msg: Message):
    uid = msg.from_user.id

    if uid not in user_mode:
        await msg.answer("⚠ Please choose:\n🧠 Summary or 📚 Quiz first")
        return

    await msg.answer("⏳ Reading PDF...")

    text = await extract_pdf(msg)

    if not text.strip():
        await msg.answer("❌ Failed to read PDF")
        return

    user_pdf_text[uid] = text

    # ================= SUMMARY MODE =================
    if user_mode[uid] == "summary":
        await msg.answer("📖 Splitting chapters...")

        chapters = text.split("Chapter")
        if len(chapters) <= 1:
            chapters = [text[i:i+5000] for i in range(0,len(text),5000)]

        user_chapters[uid] = chapters

        buttons = []
        for i in range(len(chapters)):
            buttons.append([InlineKeyboardButton(
                text=f"Chapter {i+1}",
                callback_data=f"ch_{i}"
            )])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await msg.answer("📌 Select chapter to summarize:", reply_markup=kb)
        return


    # ================= QUIZ MODE =================
    if user_mode[uid] == "quiz":
        await msg.answer("🧠 Generating Quiz...")

        prompt = f"""
        Create exactly 5 MCQ.
        STRICT FORMAT:
        Q: question
        A) option
        B) option
        C) option
        D) option
        Answer: A

        Text:
        {text[:6000]}
        """

        quiz_text = await ask_gemini(prompt)

        questions = []
        blocks = quiz_text.split("Q:")
        for b in blocks:
            if "Answer" in b:
                lines = b.strip().split("\n")
                try:
                    q = lines[0]
                    A = lines[1].replace("A)","").strip()
                    B = lines[2].replace("B)","").strip()
                    C = lines[3].replace("C)","").strip()
                    D = lines[4].replace("D)","").strip()
                    ans = b.split("Answer:")[1].strip()[0]

                    questions.append({
                        "q": q,"A":A,"B":B,"C":C,"D":D,"ans":ans
                    })
                except:
                    continue

        user_quiz[uid] = {"index":0,"score":0,"questions":questions}

        await send_question(msg.chat.id, uid)



# -------- SEND QUESTION ----------
async def send_question(chat_id, uid):
    quiz = user_quiz[uid]

    if quiz["index"] >= len(quiz["questions"]):
        await bot.send_message(chat_id,
            f"🏁 Quiz Finished!\n🎯 Score: {quiz['score']} / {len(quiz['questions'])}")
        return

    q = quiz["questions"][quiz["index"]]

    text = f"""
Q{quiz['index']+1}: {q['q']}

A) {q['A']}
B) {q['B']}
C) {q['C']}
D) {q['D']}
"""

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="A", callback_data="ans_A"),
             InlineKeyboardButton(text="B", callback_data="ans_B")],
            [InlineKeyboardButton(text="C", callback_data="ans_C"),
             InlineKeyboardButton(text="D", callback_data="ans_D")]
        ])

    await bot.send_message(chat_id,text,reply_markup=kb)


# -------- ANSWER CHECK ----------
@router.callback_query(F.data.startswith("ans_"))
async def check_answer(call: CallbackQuery):
    uid = call.from_user.id
    quiz = user_quiz[uid]

    chosen = call.data[-1]
    correct = quiz["questions"][quiz["index"]]["ans"]

    if chosen == correct:
        quiz["score"] += 1
        await call.message.answer("✅ Correct!")
    else:
        await call.message.answer(f"❌ Wrong! Correct = {correct}")

    quiz["index"] += 1
    await send_question(call.message.chat.id, uid)


# ---------- SUMMARY + BOOKMARK ----------
@router.callback_query(F.data.startswith("ch_"))
async def summarize_chapter(call: CallbackQuery):
    uid = call.from_user.id
    index = int(call.data.split("_")[1])

    text = user_chapters[uid][index]

    prompt = f"Summarize clearly in bullet points:\n{text[:6000]}"
    result = await ask_gemini(prompt)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔖 Save Bookmark",
                callback_data=f"bm_{index}"
            )]
        ]
    )

    await call.message.answer(result, reply_markup=kb)

    # temporarily store summary
    if uid not in user_bookmarks:
        user_bookmarks[uid] = {}

    user_bookmarks[uid][index] = result



# ---------- SAVE BOOKMARK ----------
@router.callback_query(F.data.startswith("bm_"))
async def save_bookmark(call: CallbackQuery):
    uid = call.from_user.id
    ch = int(call.data.split("_")[1])

    await call.message.answer(f"✅ Chapter {ch+1} bookmarked successfully!")



# ---------- VIEW BOOKMARKS ----------
@router.message(F.text == "🔖 Bookmarks")
async def show_bookmarks(msg: Message):
    uid = msg.from_user.id

    if uid not in user_bookmarks or len(user_bookmarks[uid]) == 0:
        await msg.answer("📭 No bookmarks saved yet.")
        return

    text = "📚 **Your Saved Bookmarks**\n\n"

    for ch, summary in user_bookmarks[uid].items():
        text += f"🔖 Chapter {ch+1}\n{summary[:300]}...\n\n"

    await msg.answer(text)


# ---------- HELP ----------
@router.message(F.text == "ℹ Help")
async def help_msg(msg: Message):
    await msg.answer(
        "📌 How to use AI Study Companion:\n\n"
        "1️⃣ Choose *Summarize* or *Quiz*\n"
        "2️⃣ Upload your PDF\n"
        "3️⃣ Select chapter to summarize OR answer quiz\n"
        "4️⃣ Save bookmarks for quick revision 🔖\n\n"
        "💬 For any query or support contact:\n"
        "👉 @rezimmm"
    )



# ---------- RUN ----------
# ---------- RUN ----------
async def main():
    print("🚀 Starting bot...")
    dp.include_router(router)

    print("🧹 Deleting webhook...")
    await bot.delete_webhook(drop_pending_updates=True)

    print("🤖 Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())


