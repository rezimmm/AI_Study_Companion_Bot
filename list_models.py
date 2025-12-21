import requests, os
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_KEY")
print("KEY:", key[:8], "...")

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
res = requests.get(url)

print("\n==== AVAILABLE MODELS FOR YOUR ACCOUNT ====\n")
print(res.text)
