
# Built By Rezim Titoria

from fastapi import FastAPI
from pymongo import MongoClient
from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

client = MongoClient(os.getenv("MONGO_URI"))
db = client.studybot

class PDFData(BaseModel):
    user_id: int
    text: str
    title: str

@app.post("/save_pdf")
def save_pdf(data: PDFData):
    db.documents.insert_one(data.dict())
    return {"msg": "PDF saved"}

@app.get("/get_notes/{user_id}")
def get_notes(user_id: int):
    docs = list(db.documents.find({"user_id": user_id}, {"_id":0}))
    return docs

