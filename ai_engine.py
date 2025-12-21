
# Built By Rezim Titoria

import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

GEMINI_KEY = (os.getenv("GEMINI_KEY") or "").strip()
print("LOADED GEMINI KEY:", GEMINI_KEY[:8], "...")

# ⭐ This model exists in YOUR account
MODEL = "gemini-flash-latest"


def ask_ai(prompt):
    try:
        if not GEMINI_KEY or not GEMINI_KEY.startswith("AIza"):
            return "❌ Gemini API key missing or invalid. Please set GEMINI_KEY correctly."

        # IMPORTANT: your account uses v1beta
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_KEY}"

        headers = {"Content-Type": "application/json"}

        data = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ]
        }

        res = requests.post(url, headers=headers, data=json.dumps(data))

        print("GEMINI RAW:", res.text)

        res_json = res.json()

        # Handle API error nicely
        if "error" in res_json:
            return "❌ Gemini Error: " + res_json["error"]["message"]

        # Extract response text
        return res_json["candidates"][0]["content"]["parts"][0]["text"]

    except Exception as e:
        print("GEMINI ERROR:", e)
        return "❌ AI processing failed."

