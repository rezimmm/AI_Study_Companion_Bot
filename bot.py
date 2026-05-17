import os
import asyncio
from urllib import request, response
import uuid
import aiohttp
import fitz
from starlette.middleware.sessions import SessionMiddleware
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import Command
from fastapi import FastAPI, Request, HTTPException, Form, File, UploadFile
import uvicorn
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
import json
from datetime import datetime

import random
OTP_STORE = {}

from db import get_settings, update_settings

import sqlite3
import time

def init_db():
    conn = sqlite3.connect("analytics.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS latency(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_ms INTEGER,
        ts INTEGER
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS uptime(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT,
        ts INTEGER
    )
    """)

    conn.commit()
    conn.close()

init_db()

def log_latency(ms):
    conn = sqlite3.connect("analytics.db")
    c = conn.cursor()
    c.execute("INSERT INTO latency(time_ms, ts) VALUES (?, strftime('%s','now'))", (ms,))
    conn.commit()
    conn.close()


PDF_STORAGE = {}

app = FastAPI()

ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "dev_fallback_secret")

app.add_middleware(
    SessionMiddleware,
    secret_key=ADMIN_SESSION_SECRET,
    session_cookie="admin_session",     # Optional name
    https_only=False                    # Set True if you use HTTPS always
)


@app.middleware("http")
async def block_query_key(request: Request, call_next):
    query = dict(request.query_params)

    # Block ANY ?key= usage
    if "key" in query:
        return RedirectResponse("/admin/login", status_code=302)

    return await call_next(request)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("ADMIN_SESSION_SECRET", "fallback_dev_secret")
)

import fitz

def extract_chapters(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    toc = doc.get_toc(simple=True)

    chapters = []
    for item in toc:
        level, title, page = item
        if level == 1:  # Only main chapters
            chapters.append({
                "title": title,
                "page": page
            })

    return chapters

def get_chapter_text(pdf_bytes, start_page, end_page):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""

    for page in range(start_page-1, end_page-1):
        text += doc.load_page(page).get_text()

    return text

def detect_chapters(text):
    """Fallback chapter detection when PDF has no TOC"""
    chapters = []
    lines = text.split("\n")
    
    for i, line in enumerate(lines):
        line_lower = line.lower().strip()
        if line_lower.startswith("chapter") or line_lower.startswith("section"):
            chapters.append({
                "title": line.strip(),
                "page": i // 50 + 1  # Approximate page number
            })
    
    if not chapters:
        # If no chapters found, split into equal parts
        chapters = [{"title": f"Section {i+1}", "page": i+1} for i in range(5)]
    
    return chapters

def format_real_toc(toc):
    """Format real TOC from PDF"""
    chapters = []
    for level, title, page in toc:
        if level == 1:
            chapters.append({
                "title": title,
                "page": page
            })
    return chapters

from fastapi import Request

@app.get("/get_chapters")
async def get_chapters(request: Request):
    session_id = request.session.get("pdf_id")
    if not session_id or session_id not in PDF_STORAGE:
        return []

    return PDF_STORAGE[session_id]["chapters"]

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
WEB_URL = os.getenv("WEB_URL", "https://ai-study-companion-bot.onrender.com")

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
user_bookmarks = {}  # <-- chapter-based bookmarks
user_quiz = {}
PDF_STORAGE = {}  # <-- storage for uploaded PDFs

menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧠 Summarise PDF")],
        [KeyboardButton(text="📚 Quiz")],
        [KeyboardButton(text="🔖 Bookmark")],
        [KeyboardButton(text="ℹ About")],
    ],
    resize_keyboard=True,
)


# ---------------- GEMINI ----------------
async def ask_gemini(prompt: str):
    import aiohttp, asyncio

    if not prompt or not prompt.strip():
        return "⚠ Gemini Error: Empty prompt sent to API."

    MODEL = "models/gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/{MODEL}:generateContent?key={GEMINI_KEY}"

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    async with aiohttp.ClientSession() as session:
        for attempt in range(3):   # retry up to 3 times
            async with session.post(url, json=payload) as res:
                data = await res.json()

                # ---------- SUCCESS ----------
                if "candidates" in data:
                    return data["candidates"][0]["content"]["parts"][0]["text"]

                # ---------- MODEL BUSY ----------
                if "error" in data:
                    err = str(data["error"]).lower()

                    # 503 / overload → temporary → retry
                    if "unavailable" in err or "overloaded" in err:
                        await asyncio.sleep(2)
                        continue

                    # return error so safe_ai_call() can handle quota maintenance
                    return f"ERROR::{err}"

        return "ERROR::service unavailable after retries"

# ---------------- PDF ----------------
import fitz
from pdf2image import convert_from_bytes
import pytesseract
import io

# ================= Utilities =================
import aiohttp
import os
from fastapi import File, UploadFile
import shutil

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)):

    # -------- Maintenance Lock --------
    settings = get_settings()
    if settings["maintenance_mode"]:
        return templates.TemplateResponse(
            "maintenance.html",
            {
                "request": request,
                "reason": settings["reason"],
                "time": settings["last_time"]
            },
        )

    # -------- Validate --------
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF allowed")

    # Optional safety limit (prevents crashes)
    if file.size and file.size > 15_000_000:     # 15MB
        raise HTTPException(status_code=400, detail="PDF too large")

    # -------- Save Temporary --------
    session_id = str(uuid.uuid4())
    temp_path = f"/tmp/{session_id}.pdf"

    with open(temp_path, "wb") as f:
        f.write(await file.read())

    # -------- Extract Text --------
    try:
        doc = fitz.open(temp_path)
        text = ""
        for page in doc:
            text += page.get_text("text")
        total_pages = len(doc)
        doc.close()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF Processing Failed: {e}")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    # -------- Store In Memory --------
    PDF_STORAGE[session_id] = {
    "filename": file.filename,
    "text": text,
    "bytes": open(temp_path, "rb").read(),
    "summary": None,
    "quiz": None,
    "chapters": [],
    "total_pages": total_pages
    }


    # -------- Session Save --------
    request.session["pdf_id"] = session_id

    # -------- Return Page --------
    return templates.TemplateResponse(
        "uploaded.html",
        {
            "request": request,
            "filename": file.filename,
            "length": len(text),
            "pages": total_pages
        },
    )

async def safe_ai_call(func, *args, **kwargs):
    from db import update_settings
    from aiogram import Bot
    import time

    BOT_TOKEN = "7506160769:AAFeAbK7jNdZBoVaObK0PmWcDWzNgpTnpeo"    # <-- you already have bot globally, so do NOT hardcode token
    bot = Bot(token=BOT_TOKEN)

    try:
        # Prevent empty prompt crash
        if args and isinstance(args[0], str) and not args[0].strip():
            return "❌ AI request failed: Empty text received."

        result = await func(*args, **kwargs)

        # Gemini retry system may return this style error text
        if isinstance(result, str) and result.startswith("ERROR::"):
            err = result.lower()

            # ----- QUOTA DEAD -----
            if "quota" in err or "billing" in err or "limit" in err:
                update_settings(
                    maintenance=True,
                    reason="API QUOTA EXHAUSTED"
                )

                try:
                    await bot.send_message(
                        5659835220,
                        "🚨 *MAINTENANCE TRIGGERED*\n"
                        "Reason: API Quota Finished\n"
                        "Bot locked to prevent crashes.\n\n"
                        "⏳ Auto restore after 24 Hours.",
                        parse_mode="Markdown"
                    )
                except:
                    pass

                return None

            # ----- MODEL BUSY -----
            if "unavailable" in err or "overloaded" in err:
                return "⚠ AI service is currently overloaded. Please try again."

        # If AI returned empty
        if not result:
            return "⚠ AI returned no response. Try again."

        return result


    except Exception as e:
        err = str(e).lower()
        print("⚠ SAFE AI ERROR:", err)

        # ---------- QUOTA / BILLING ----------
        if "quota" in err or "limit" in err or "billing" in err or "resource_exhausted" in err:
            update_settings(
                maintenance=True,
                reason="API QUOTA EXHAUSTED",
            )

            try:
                await bot.send_message(
                    5659835220,
                    "🚨 *MAINTENANCE TRIGGERED*\n"
                    "Reason: API Quota Finished\n"
                    "Bot locked to prevent crashes.\n\n"
                    "⏳ Auto restore after 24 Hours.",
                    parse_mode="Markdown"
                )
            except:
                pass

            return None

        # ---------- INVALID PROMPT ----------
        if "invalid_argument" in err:
            return "❌ AI rejected the request. PDF may be empty or corrupted."

        # ---------- NETWORK ----------
        if "timeout" in err or "connection" in err:
            return "🌐 Network error communicating with AI. Try again."

        # ---------- Unknown ----------
        return "❌ Unexpected AI error occurred. Please try later."


from fastapi.responses import HTMLResponse

@app.get("/summarize", response_class=HTMLResponse)
async def summarize_pdf(request: Request):
    session_id = request.session.get("pdf_id")

    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(status_code=404, detail="PDF session expired or not found")

    data = PDF_STORAGE[session_id]

    # If summary already exists → reuse
    if data.get("summary"):
        return templates.TemplateResponse(
            "summary.html",
            {
                "request": request,
                "summary": data["summary"],
                "filename": data.get("filename", "Uploaded PDF")
            }
        )

    # Otherwise generate new
    text = data["text"]
    summary = await gemini_summary(text)

    if not summary:
        summary = "❌ Failed to generate summary."

    data["summary"] = summary

    return templates.TemplateResponse(
        "summary.html",
        {
            "request": request,
            "summary": summary,
            "filename": data.get("filename", "Uploaded PDF")
        }
    )

@app.get("/chapters", response_class=HTMLResponse)
async def chapters(request: Request):
    session_id = request.session.get("pdf_id")

    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(404, "PDF expired")

    data = PDF_STORAGE[session_id]

    # If already detected before → reuse
    if data.get("chapters"):
        return templates.TemplateResponse(
            "chapters.html",
            {
                "request": request,
                "chapters": data["chapters"],
                "filename": data.get("filename", "Uploaded PDF")
            }
        )

    # -------- OPEN PDF --------
    doc = fitz.open(stream=data["text"].encode("utf-8"), filetype="pdf") \
        if "bytes" not in data else fitz.open(stream=data["bytes"], filetype="pdf")

    toc = doc.get_toc()

    # -------- IF REAL TOC EXISTS --------
    if toc and len(toc) > 0:
        chapters = []
        for t in toc:
            level, title, page = t
            chapters.append({
                "title": title,
                "page": page - 1
            })

        data["chapters"] = chapters
        doc.close()

    else:
        # -------- FALLBACK CUSTOM DETECTION --------
        data["chapters"] = detect_chapters(data["text"])
        doc.close()

    return templates.TemplateResponse(
        "chapters.html",
        {
            "request": request,
            "chapters": data["chapters"],
            "filename": data.get("filename", "Uploaded PDF")
        }
    )

@app.get("/summarize_chapter", response_class=HTMLResponse)
async def summarize_chapter(request: Request, index: int):
    session_id = request.session.get("pdf_id")

    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(status_code=404, detail="PDF session expired or not found")

    data = PDF_STORAGE[session_id]

    if "chapters" not in data or not data["chapters"]:
        raise HTTPException(status_code=400, detail="No chapters detected in PDF")

    chapters = data["chapters"]

    # safety check
    if index < 0 or index >= len(chapters):
        raise HTTPException(status_code=400, detail="Invalid chapter index")

    start_page = chapters[index]["page"]
    end_page = chapters[index+1]["page"] if index + 1 < len(chapters) else data["total_pages"]

    text = get_chapter_text(data["bytes"], start_page, end_page)

    summary = await gemini_summary(text)

    if not summary:
        summary = "❌ Failed to generate summary."

    return templates.TemplateResponse(
        "summary.html",
        {
            "request": request,
            "filename": chapters[index]["title"],
            "summary": summary
        }
    )

# ---------- DOWNLOAD FILE ----------

async def download_file(file_path: str, filename: str):
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    save_path = f"/tmp/{filename}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            with open(save_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    f.write(chunk)

    return save_path

from fastapi.responses import PlainTextResponse

@app.get("/download", response_class=PlainTextResponse)
async def download_summary(request: Request):
    session_id = request.session.get("pdf_id")
    data = PDF_STORAGE[session_id]

    summary = data.get("summary", "No summary generated yet")

    return PlainTextResponse(
        summary,
        headers={"Content-Disposition": "attachment; filename=summary.txt"}
    )


async def extract_pdf(msg):
    file = await bot.get_file(msg.document.file_id)

    # Download safely to /tmp
    path = await download_file(file.file_path, msg.document.file_name)

    text = ""

    # ---------- Try Normal Text Extraction ----------
    doc = fitz.open(path)
    for page in doc:
        content = page.get_text("text")
        if content.strip():
            text += content + "\n"
    doc.close()

    # ---------- Extract Table of Contents ----------
    toc = []
    try:
        doc = fitz.open(path)
        raw_toc = doc.get_toc(simple=True)

        for level, title, page in raw_toc:
            if level == 1:   # main chapters only
                toc.append({
                    "title": title,
                    "page": page
                })

        doc.close()
    except:
        toc = []

    # ---------- If text is good, return (NO OCR needed) ----------
    if len(text.strip()) > 500:
        pdf_bytes = open(path, "rb").read()
        os.remove(path)

        return {
            "text": text,
            "chapters": toc,
            "bytes": pdf_bytes
        }

    # ---------- OCR Only If Needed ----------
    pages = convert_from_bytes(open(path, "rb").read(), dpi=200)

    ocr_text = []
    for img in pages:
        ocr_text.append(pytesseract.image_to_string(img))

    pdf_bytes = open(path, "rb").read()
    os.remove(path)

    return {
        "text": "\n".join(ocr_text),
        "chapters": toc,
        "bytes": pdf_bytes
    }


# ---------- TEXT CHUNKING ----------
def chunk_text(text, chunk_size=2000):
    chunks = []
    words = text.split()

    current = []
    size = 0

    for word in words:
        size += len(word) + 1
        current.append(word)

        if size >= chunk_size:
            chunks.append(" ".join(current))
            current = []
            size = 0

    if current:
        chunks.append(" ".join(current))

    return chunks


# --------------- START -----------------
@router.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        "👋 Welcome to AI Study Companion\nChoose an option below 👇", reply_markup=menu
    )


@router.message(F.text == "🧠 Summarise PDF")
@router.message(Command("summarize", "summarise"))
async def set_summary(msg: Message):
    uid = msg.from_user.id
    user_mode[uid] = "summary"
    await msg.answer("📥 Upload PDF to summarize by chapters.")


@router.message(F.text == "📚 Quiz")
@router.message(Command("quiz"))
async def set_quiz(msg: Message):
    uid = msg.from_user.id
    user_mode[uid] = "quiz"
    await msg.answer("📥 Upload PDF to generate quiz questions.")


# ------------ MODES -------------
from aiogram.exceptions import TelegramBadRequest

MAX_SIZE = 20 * 1024 * 1024  # 20MB Telegram Bot Limit


@router.message(F.document)
async def handle_pdf(msg: Message):
    uid = msg.from_user.id

    if uid not in user_mode:
        await msg.answer("⚠ Please choose:\n🧠 Summary or 📚 Quiz first")
        return

    doc = msg.document

    # ---------- SAFEST SIZE CHECK ----------
    MAX_SIZE = 20 * 1024 * 1024   # 20MB limit

    if not doc.file_size:
        # Telegram sometimes hides file size on big files
        await msg.answer(
            "⚠ I couldn't detect this PDF size.\n\n"
            "👉 If it's larger than 20MB, please upload it here:\n"
            f"{WEB_URL}/upload\n\n"
            "If it's below 20MB, try sending again 👍"
        )
        return

    if doc.file_size > MAX_SIZE:
        await msg.answer(
            "❌ This PDF is larger than 20MB.\n\n"
            "Telegram does not allow bots to give bots such big files.\n\n"
            "👉 Upload here instead:\n"
            f"{WEB_URL}/upload\n\n"
            "After upload, I will process it 😊"
        )
        return

    await msg.answer("⏳ Reading PDF...")

    try:
        text = await extract_pdf(msg)

    except TelegramBadRequest as e:
        if "file is too big" in str(e):
            await msg.answer(
                "❌ Telegram refused to send this file.\n\n"
                "👉 Upload here instead:\n"
                f"{WEB_URL}/upload"
            )
        else:
            await msg.answer("⚠️ Something went wrong while reading the PDF.")
        return

    if not text.strip():
        await msg.answer("❌ Failed to read PDF content.")
        return

    user_pdf_text[uid] = text
    await msg.answer("✅ PDF processed successfully! Continue…")

    # ================= SUMMARY MODE =================
    if user_mode[uid] == "summary":
        await msg.answer("📖 Splitting chapters...")

        chapters = text.split("Chapter")
        if len(chapters) <= 1:
            chapters = [text[i : i + 5000] for i in range(0, len(text), 5000)]

        user_chapters[uid] = chapters

        buttons = []
        for i in range(len(chapters)):
            buttons.append(
                [InlineKeyboardButton(text=f"Chapter {i + 1}", callback_data=f"ch_{i}")]
            )

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await msg.answer("📌 Select chapter to summarize:", reply_markup=kb)
        return

    # ================= QUIZ MODE =================
    if user_mode[uid] == "quiz":
        await msg.answer("🧠 Generating Quiz...")

        chunks = chunk_text(text, 5000)
        all_questions = []

        for chunk in chunks:
            prompt = f"""
            Generate exactly 5 MCQ.
            STRICT FORMAT:
            Q: question
            A) option
            B) option
            C) option
            D) option
            Answer: A

            Text:
            {chunk}
            """

            quiz_text = await safe_ai_call(ask_gemini, prompt)
            if not quiz_text:
                return templates.TemplateResponse("maintenance.html", {"request": request})


            blocks = quiz_text.split("Q:")
            for b in blocks:
                if "Answer" in b:
                    lines = b.strip().split("\n")
                    try:
                        q = lines[0]
                        A = lines[1].replace("A)", "").strip()
                        B = lines[2].replace("B)", "").strip()
                        C = lines[3].replace("C)", "").strip()
                        D = lines[4].replace("D)", "").strip()
                        ans = b.split("Answer:")[1].strip()[0]

                        all_questions.append(
                            {"q": q, "A": A, "B": B, "C": C, "D": D, "ans": ans}
                        )
                    except:
                        continue

        if not all_questions:
            await msg.answer("❌ Failed to generate quiz.")
            return

        user_quiz[uid] = {"index": 0, "score": 0, "questions": all_questions[:20]}

        await msg.answer(f"✅ Generated {len(user_quiz[uid]['questions'])} Questions!")
        await send_question(msg.chat.id, uid)


# -------- SEND QUESTION ----------
async def send_question(chat_id, uid):
    quiz = user_quiz[uid]

    if quiz["index"] >= len(quiz["questions"]):
        await bot.send_message(
            chat_id,
            f"🏁 Quiz Finished!\n🎯 Score: {quiz['score']} / {len(quiz['questions'])}",
        )
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
            [
                InlineKeyboardButton(text="A", callback_data="ans_A"),
                InlineKeyboardButton(text="B", callback_data="ans_B"),
            ],
            [
                InlineKeyboardButton(text="C", callback_data="ans_C"),
                InlineKeyboardButton(text="D", callback_data="ans_D"),
            ],
        ]
    )

    await bot.send_message(chat_id, text, reply_markup=kb)


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
            [InlineKeyboardButton(text="🔖 Save Bookmark", callback_data=f"bm_{index}")]
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
@router.message(F.text == "🔖 Bookmark")
@router.message(Command("bookmarks"))
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
@router.message(F.text == "ℹ About")
@router.message(Command("help", "about"))
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


# ---------------- ADMIN DASHBOARD ----------------
from fastapi import FastAPI, Request, HTTPException, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

ADMIN_KEY = os.getenv("ADMIN_KEY", "admin123")
templates = Jinja2Templates(directory="templates")

SESSION_TIMEOUT = 3600 * 6   # 6 hours


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):

        if request.query_params.get("otp") == "1":
            return templates.TemplateResponse("otp.html", {"request": request})

        return templates.TemplateResponse("login.html", {"request": request})


@app.post("/admin/login")
async def login(request: Request, key: str = Form(...)):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid key")

    # Save IP + Start Session Skeleton
    client_ip = request.client.host
    request.session["ip"] = client_ip
    request.session["login_time"] = int(time.time())

    # ---------- Generate OTP ----------
    otp = random.randint(100000, 999999)
    request.session["pending_otp"] = otp
    request.session["otp_time"] = int(time.time())

    # ---------- SEND OTP (Telegram Recommended) ----------
    try:
        await bot.send_message(
            5659835220,          # <-- your admin telegram ID
            f"🔐 Admin Login Attempt\n\nYour OTP: *{otp}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        print("Failed to send OTP:", e)

    # Redirect to OTP page
    return RedirectResponse("/admin/login?otp=1", status_code=302)

@app.post("/admin/verify_otp")
async def verify_otp(request: Request, otp: str = Form(...)):
    session = request.session

    real = session.get("pending_otp")
    created = session.get("otp_time", 0)

    if not real:
        raise HTTPException(401, "No OTP session found")

    # Expire after 5 minutes
    if int(time.time()) - created > 300:
        session.clear()
        raise HTTPException(401, "OTP expired")

    if str(real) != otp:
        raise HTTPException(401, "Invalid OTP")

    # SUCCESS 🎉
    session["auth"] = True
    session.pop("pending_otp", None)

    return RedirectResponse("/admin", status_code=302)

@app.get("/admin/resend_otp")
async def resend_otp(request: Request):
    session = request.session

    # Ensure OTP stage exists
    if "pending_otp" not in session:
        return {"status": False, "msg": "No OTP session active"}

    # Prevent spam: 30s cooldown
    import time
    last = session.get("last_otp_time", 0)

    if time.time() - last < 30:
        return {"status": False, "msg": "Please wait before requesting again"}

    # Generate new OTP
    import random
    otp = random.randint(100000, 999999)

    session["pending_otp"] = otp
    session["otp_time"] = int(time.time())
    session["last_otp_time"] = int(time.time())

    # Send to Telegram
    try:
        await bot.send_message(
            5659835220,    # <-- YOUR TELEGRAM ADMIN ID
            f"🔐 New OTP Requested\nYour OTP: *{otp}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        print("Failed to send OTP:", e)
        return {"status": False, "msg": "Failed to send OTP"}

    return {"status": True, "msg": "OTP Resent Successfully"}

# ---------- VERIFY SESSION ----------
import time
from starlette.responses import RedirectResponse

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

# ---------- DASHBOARD ----------
from datetime import datetime
from db import get_settings
from fastapi.responses import HTMLResponse, RedirectResponse


@app.get("/admin", response_class=HTMLResponse)
async def dashboard(request: Request):
    resp = verify_session(request)
    if isinstance(resp, RedirectResponse):
        return resp

    users = list(user_mode.keys()) if "user_mode" in globals() else []

    stats = {
        "pdfs": len(PDF_STORAGE) if "PDF_STORAGE" in globals() else 0,
        "quizzes": sum(1 for x in PDF_STORAGE.values() if x.get("quiz")) if "PDF_STORAGE" in globals() else 0,
        "active_today": len(users)
    }

    settings = get_settings()

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "total_users": len(users),
            "total_pdfs": stats["pdfs"],
            "total_quizzes": stats["quizzes"],
            "active_today": stats["active_today"],
            "users": users,

            # Maintenance info
            "maintenance": settings["maintenance_mode"],
            "reason": settings["reason"],
            "last_time": datetime.fromtimestamp(
                settings["last_time"]
            ).strftime("%Y-%m-%d %H:%M:%S")
        }
    )

#verify session

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

# ---------- BROADCAST PAGE ----------
@app.get("/admin/broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request):
    resp = verify_session(request)
    if resp:
        return resp

    return templates.TemplateResponse("broadcast.html", {"request": request})

#verify session

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

# ---------- SEND BROADCAST ----------
@app.post("/admin/broadcast")
async def broadcast(request: Request, message: str = Form(...)):
    resp = verify_session(request)
    if resp:
        return resp

    sent = 0
    for uid in list(user_mode.keys()):
        try:
            await bot.send_message(uid, f"📢 Admin Broadcast:\n\n{message}")
            sent += 1
        except:
            continue

    return {"status": "sent", "delivered": sent}

# ---------- VERIFY SESSION ----------

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

import psutil, time, random

START_TIME = time.time()

@app.get("/admin/status", response_class=HTMLResponse)
async def status_page(request: Request):
    resp = verify_session(request)
    if resp:
        return resp

    users = list(user_mode.keys()) if "user_mode" in globals() else []

    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "users": len(users)
        }
    )

# ---------- VERIFY SESSION ----------

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

@app.get("/admin/live_status")
async def live_status(request: Request):
    resp = verify_session(request)
    if resp:
        return resp

    users = list(user_mode.keys()) if "user_mode" in globals() else []

    conn = sqlite3.connect("analytics.db")
    c = conn.cursor()

    # Last 20 latency records
    c.execute("SELECT time_ms FROM latency ORDER BY id DESC LIMIT 20")
    latency = [row[0] for row in c.fetchall()][::-1]

    # Uptime percentage last 24 hours
    c.execute("""
    SELECT COUNT(*) FROM uptime
    WHERE ts > strftime('%s','now') - 86400
    """)
    up_count = c.fetchone()[0]

    conn.close()

    uptime_percent = min(100, int((up_count / 1440) * 100))

    return {
        "users": len(users),
        "latency_graph": latency,
        "uptime_percent": uptime_percent,
        "cpu": psutil.cpu_percent(),
        "memory": psutil.virtual_memory().percent
    }

#verify_session 

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None
  
# ---------- LOGOUT ----------
@app.get("/admin/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=302)

@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    resp = verify_session(request)
    if resp:
        return resp

    s = get_settings()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "maintenance": s["maintenance_mode"],
            "bot": s["bot_enabled"],
            "theme": s["theme"],
            "reason": s["reason"],
            "last_time": datetime.fromtimestamp(s["last_time"]).strftime("%Y-%m-%d %H:%M:%S")
        }
    )


@app.post("/admin/settings")
async def save_settings(
    request: Request,
    maintenance: int = Form(0),
    bot_enabled: int = Form(1),
    theme: str = Form("dark")
):
    
    resp = verify_session(request)
    if resp:
        return resp
    
    update_settings(maintenance, bot_enabled, theme)

    return RedirectResponse("/admin/settings", status_code=302)

#verify session

SESSION_TIMEOUT = 3600 * 6   # 6 hours

def verify_session(request: Request):
    session = request.session

    # ---------- NOT LOGGED IN ----------
    if not session.get("auth"):
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- IP LOCK ----------
    current_ip = request.client.host
    if session.get("ip") != current_ip:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION EXPIRY ----------
    login_time = session.get("login_time", 0)
    now = int(time.time())

    if now - login_time > SESSION_TIMEOUT:
        session.clear()
        return RedirectResponse("/admin/login", status_code=302)

    # ---------- SESSION VALID ----------
    return None

# ---------- UPLOAD PAGE ----------
@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

@app.post("/upload", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)):

    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF allowed")

    path = f"/tmp/{file.filename}"
    content = await file.read()

    with open(path, "wb") as f:
        f.write(content)

    doc = fitz.open(path)

    # ---------- Extract Text ----------
    text = ""
    for page in doc:
        text += page.get_text("text")

    # ---------- Extract Table of Contents ----------
    raw_toc = doc.get_toc(simple=True)
    chapters = []

    if raw_toc:
        for level, title, page in raw_toc:
            if level == 1:
                chapters.append({"title": title, "page": page})
    else:
        page_no = 1
        for page in doc:
            txt = page.get_text("text")
            if txt.strip().lower().startswith("chapter") or "CHAPTER" in txt[:50]:
                chapters.append({"title": txt.split("\n")[0], "page": page_no})
            page_no += 1

    total_pages = doc.page_count
    pdf_bytes = open(path, "rb").read()

    doc.close()
    os.remove(path)

    # ---------- SAVE SESSION ----------
    session_id = file.filename   # or uuid if you prefer unique
    request.session["pdf_id"] = session_id

    PDF_STORAGE[session_id] = {
        "filename": file.filename,
        "text": text,
        "summary": None,
        "quiz": None,
        "chapters": chapters,
        "bytes": pdf_bytes,
        "total_pages": total_pages
    }

    return templates.TemplateResponse(
        "uploaded.html",
        {"request": request, "filename": file.filename, "length": len(text)},
    )


# ===================== SUMMARY PAGE =====================
@app.get("/summarize", response_class=HTMLResponse)
async def summarize_pdf(request: Request):
    session_id = request.session.get("pdf_id")

    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(status_code=404, detail="PDF session expired or not found")

    data = PDF_STORAGE[session_id]

    # generate summary only once
    if "summary" not in data:
        text = data["text"]
        summary = await gemini_summary(text)

        if not summary:
            summary = "❌ Failed to generate summary. Gemini returned empty response."

        data["summary"] = summary

    return templates.TemplateResponse(
        "summary.html",
        {
            "request": request,
            "summary": data["summary"],
            "filename": data["filename"] if "filename" in data else "Uploaded PDF"
        }
    )

# ===================== QUIZ PAGE =====================
@app.get("/quiz", response_class=HTMLResponse)
async def quiz(request: Request):
    session_id = request.session.get("pdf_id")

    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(status_code=404, detail="PDF session expired or not found")

    data = PDF_STORAGE[session_id]
    text = data.get("text")

    if not text:
        raise HTTPException(status_code=404, detail="File not found")

    prompt = f"""
You are an expert EXAM QUIZ GENERATOR.

From the following study material, create EXACTLY 15 MCQ questions.

Return ONLY valid JSON.
NO markdown.
NO ```json.
NO explanation text outside JSON.

FORMAT:

[
  {{
    "question": "question text",
    "options": ["A","B","C","D"],
    "answer_index": 0,
    "explanation": "short explanation"
  }}
]

Rules:
- Works for ANY subject
- Concept-based, not copying text
- Simple exam language
- 4 options only
- answer_index must be 0,1,2,3
- Explanation must help student

TEXT:
{text[:20000]}
"""


    raw_response = await ask_gemini(prompt)

    if raw_response.startswith("⚠") or raw_response.startswith("❌"):
        return templates.TemplateResponse(
            "quiz.html",
            {"request": request, "error": raw_response, "questions": []}
        )

    import json
    try:
        questions = json.loads(raw_response)
    except Exception:
        return templates.TemplateResponse(
            "quiz.html",
            {
                "request": request,
                "error": "⚠ AI returned invalid JSON. Please try again.",
                "questions": []
            }
        )

    data["quiz"] = questions

    return templates.TemplateResponse(
        "quiz.html",
        {
            "request": request,
            "questions": questions,
            "error": None
        }
    )

# ---------- PROCESS ADMIN UPLOAD ----------
@app.post("/admin/upload", response_class=HTMLResponse)
async def admin_upload(request: Request, file: UploadFile = File(...)):
    resp = verify_session(request)
    if resp:
        return resp

    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF allowed")

    path = f"/tmp/{file.filename}"
    with open(path, "wb") as f:
        f.write(await file.read())

    doc = fitz.open(path)
    text = ""
    for page in doc:
        text += page.get_text("text")
    doc.close()
    os.remove(path)

    import uuid
    session_id = str(uuid.uuid4())
    request.session["pdf_id"] = session_id

    PDF_STORAGE[session_id] = {
    "filename": file.filename,
    "text": text,
    "summary": None,       # for summarize page
    "quiz": None,          # for quiz page
    "chapters": [],        # avoid crashes on chapters page
}

    return templates.TemplateResponse(
        "uploaded.html",
        {"request": request, "filename": file.filename, "length": len(text)},
    )

# ---------- SUMMARIZE ----------
async def gemini_summary(text):
    try:
        MODEL = "models/gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/{MODEL}:generateContent?key={GEMINI_KEY}"

        body = {
            "contents": [{
                "parts": [{
                    "text": f"""
Summarize the following chapter in a detailed structured way.

Rules:
- Chapter-wise breakdown
- Headings + subheadings
- Key concepts
- Definitions
- Student-friendly explanation
- Minimum 500+ words

TEXT:
{text[:45000]}
"""
                }]
            }]
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body) as res:
                result = await res.json()

                # ---------- SAFETY ----------
                if "candidates" not in result:
                    return f"⚠ Gemini Error:\n{result}"

                return result["candidates"][0]["content"]["parts"][0]["text"]

    except Exception as e:
        return f"❌ Exception: {str(e)}"

# ---------- QUIZ GENERATION ----------
@app.get("/quiz/{filename}", response_class=HTMLResponse)
async def quiz(request: Request, filename: str):

    s = get_settings()
    if s["maintenance_mode"]:
        return HTMLResponse("<h2>⚠ Bot under maintenance. Try later.</h2>")

    session_id = request.session.get("pdf_id")
    
    if not session_id or session_id not in PDF_STORAGE:
        raise HTTPException(status_code=404, detail="PDF session expired or not found")
    
    text = PDF_STORAGE[session_id]["text"]

    prompt = f"""
Generate 10 MCQ questions with 4 options each.
Q: question
A) option
B) option
C) option
D) option
Answer: X

TEXT:
{text[:18000]}
"""

    quiz_text = await safe_ai_call(ask_gemini, prompt)

    if not quiz_text:
        return HTMLResponse("<h2>🚨 API Quota exhausted. Try later.</h2>")

    return templates.TemplateResponse(
        "quiz.html", {"request": request, "filename": filename, "quiz": quiz_text}
    )


@app.get("/summarize_chapter", response_class=HTMLResponse)
async def summarize_chapter(request: Request, index: int):

    s = get_settings()
    if s["maintenance_mode"]:
        return HTMLResponse("<h2>⚠ Bot under maintenance. Try later.</h2>")

    session_id = request.session.get("pdf_id")
    data = PDF_STORAGE[session_id]

    chapters = data["chapters"]

    start_page = chapters[index]["page"]
    end_page = chapters[index+1]["page"] if index+1 < len(chapters) else data["total_pages"]

    text = get_chapter_text(data["bytes"], start_page, end_page)

    summary = await safe_ai_call(gemini_summary, text)

    if not summary:
        return "<h2>🚨 API quota exhausted. Try later.</h2>"

    return f"""
    <h2>{chapters[index]['title']}</h2>
    <pre>{summary}</pre>
    """

# ---------- HEALTH CHECK ----------
@app.get("/")
def home():
    return {"status": "Bot is running 🚀"}

# ---------- RUN BOT + WEB ----------
async def start_bot():
    print("🚀 Starting Telegram Bot...")
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

@router.message()
async def handle_message(message: Message):

    # 🔥 Ignore ALL non-text messages
    if message.content_type != "text":
        return

    settings = get_settings()

    # Maintenance Lock
    if settings["maintenance_mode"]:
        await message.answer(
            "⚠ Bot is currently under maintenance.\n"
            "Please try again later."
        )
        return

    user_message = message.text or ""

    # Prevent EMPTY Gemini requests
    if not user_message.strip():
        await message.answer("❌ Please send a valid message.")
        return

    response = await safe_ai_call(ask_gemini, user_message)

    if not response:
        # Bot already switched to maintenance inside safe_ai_call
        await message.answer(
            "🚨 AI service temporarily unavailable.\n"
            "🛠 Maintenance Mode Enabled.\n"
            "Please try again later."
        )
        return

    await message.answer(response)

async def auto_restore_task():
    import asyncio, time
    while True:
        s = get_settings()

        # If bot in maintenance → check cooldown
        if s["maintenance_mode"]:
            now = int(time.time())

            # 24 HOURS = 86400 seconds
            cooldown = 86400

            if now - s["last_time"] >= cooldown:
                try:
                    await bot.send_message(
                        5659835220,
                        "✅ *Bot Restored Automatically*\n"
                        "Cooldown finished. Service active again.",
                        parse_mode="Markdown"
                    )
                except:
                    pass

                update_settings(
                    maintenance=False,
                    reason="Auto restored after 24 hours"
                )
                print("✅ Maintenance automatically disabled after 24 hours")

        await asyncio.sleep(30)   # check every 30 sec

@app.on_event("startup")
async def on_startup():
    import asyncio
    asyncio.create_task(start_bot())
    asyncio.create_task(auto_restore_task())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
