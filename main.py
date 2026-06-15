import os
from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Temporary memory to track conversation state
sessions = {}

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

    # New user
    if phone not in sessions:
        sessions[phone] = {"step": "start"}

    session = sessions[phone]

    # Step 1 - Ask role
    if session["step"] == "start":
        response.message(
            "🌾 Welcome to Farm Connect!\n\n"
            "Are you a FARMER or LABOURER?\n"
            "Reply with FARMER or LABOURER to get started."
        )
        sessions[phone]["step"] = "role"

    # Step 2 - Save role, ask name
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

    # Step 3 - Save name, ask location
    elif session["step"] == "name":
        sessions[phone]["name"] = Body.strip()
        sessions[phone]["step"] = "location"
        response.message(f"Nice to meet you {Body.strip()}! What is your village or town name?")

    # Step 4 - Save location, store in DB
    elif session["step"] == "location":
        sessions[phone]["location"] = Body.strip()
        name = sessions[phone]["name"]
        location = sessions[phone]["location"]
        role = sessions[phone]["role"]

        if role == "farmer":
            supabase.table("farmers").upsert({
                "phone": phone,
                "name": name,
                "location": location
            }).execute()
            response.message(
                f"✅ You are registered as a Farmer!\n\n"
                f"Name: {name}\n"
                f"Location: {location}\n\n"
                f"Reply POST JOB to post a new job."
            )
        else:
            supabase.table("labourers").upsert({
                "phone": phone,
                "name": name,
                "location": location
            }).execute()
            response.message(
                f"✅ You are registered as a Labourer!\n\n"
                f"Name: {name}\n"
                f"Location: {location}\n\n"
                f"We will notify you when jobs are available nearby."
            )

        sessions[phone]["step"] = "done"

    # Done
    elif session["step"] == "done":
        response.message(
            "You are already registered! 🌾\n"
            "Reply POST JOB to post a job (farmers only)."
        )

    return Response(content=str(response), media_type="application/xml")
