import os
import requests as req
from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

sessions = {}

def save_to_db(table, data):
    req.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        json=data,
        headers=HEADERS
    )

@app.get("/")
def root():
    return {"message": "Farm Connect API is running"}

@app.post("/webhook")
async def whatsapp_webhook(
    Body: str = Form(...),
    From: str = Form(...)
):
    message = Body.strip().upper()
    phone = From
    response = MessagingResponse()

    if phone not in sessions:
        sessions[phone] = {"step": "start"}

    session = sessions[phone]

    if session["step"] == "start":
        response.message(
            "🌾 Welcome to Farm Connect!\n\n"
            "Are you a FARMER or LABOURER?\n"
            "Reply with FARMER or LABOURER to get started."
        )
        sessions[phone]["step"] = "role"

    elif session["step"] == "role":
        if message == "FARMER":
            sessions[phone]["role"] = "farmer"
            sessions[phone]["step"] = "name"
            response.message("Great! What is your name?")
        elif message == "LABOURER":
            sessions[phone]["role"] = "labourer"
            sessions[phone]["step"] = "name"
            response.message("Great! What is your name?")
        else:
            response.message("Please reply with FARMER or LABOURER only.")

    elif session["step"] == "name":
        sessions[phone]["name"] = Body.strip()
        sessions[phone]["step"] = "location"
        response.message(f"Nice to meet you {Body.strip()}! What is your village or town name?")

    elif session["step"] == "location":
        sessions[phone]["location"] = Body.strip()
        name = sessions[phone]["name"]
        location = sessions[phone]["location"]
        role = sessions[phone]["role"]

        if role == "farmer":
            save_to_db("farmers", {
                "phone": phone,
                "name": name,
                "location": location
            })
            response.message(
                f"✅ Registered as Farmer!\n\n"
                f"Name: {name}\n"
                f"Location: {location}\n\n"
                f"Reply POST JOB to post a new job."
            )
        else:
            save_to_db("labourers", {
                "phone": phone,
                "name": name,
                "location": location
            })
            response.message(
                f"✅ Registered as Labourer!\n\n"
                f"Name: {name}\n"
                f"Location: {location}\n\n"
                f"We will notify you when jobs are nearby."
            )

        sessions[phone]["step"] = "done"

    elif session["step"] == "done":
        response.message(
            "You are already registered! 🌾\n"
            "Reply POST JOB to post a job."
        )

    return Response(content=str(response), media_type="application/xml")
