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
    res = req.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        json=data,
        headers=HEADERS
    )
    return res

def get_from_db(table, phone):
    from urllib.parse import quote
    encoded_phone = quote(phone, safe='')
    res = req.get(
        f"{SUPABASE_URL}/rest/v1/{table}?phone=eq.{encoded_phone}",
        headers=HEADERS
    )
    data = res.json()
    return data[0] if data else None
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

    # ─── REGISTRATION FLOW ───

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
        response.message(
            f"Nice to meet you {Body.strip()}! "
            f"What is your village or town name?"
        )

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

    # ─── MAIN MENU ───

    elif session["step"] == "done":
        if message == "POST JOB":
            # Check if this person is actually a farmer
            farmer = get_from_db("farmers", phone)
            if not farmer:
                response.message(
                    "❌ Only registered farmers can post jobs.\n"
                    "Reply FARMER to register as a farmer first."
                )
            else:
                sessions[phone]["step"] = "job_work_type"
                sessions[phone]["job"] = {}
                response.message(
                    "📋 Let's post your job!\n\n"
                    "What type of work is needed?\n"
                    "(e.g. Harvesting, Planting, Irrigation, Weeding)"
                )
        else:
            response.message(
                "Reply POST JOB to post a new job. 🌾"
            )

    # ─── JOB POSTING FLOW ───

    elif session["step"] == "job_work_type":
        sessions[phone]["job"]["work_type"] = Body.strip()
        sessions[phone]["step"] = "job_num_labourers"
        response.message("How many labourers do you need?")

    elif session["step"] == "job_num_labourers":
        if not Body.strip().isdigit():
            response.message("Please enter a number. How many labourers do you need?")
        else:
            sessions[phone]["job"]["num_labourers"] = int(Body.strip())
            sessions[phone]["step"] = "job_wage"
            response.message("What is the wage per day? (in ₹)")

    elif session["step"] == "job_wage":
        sessions[phone]["job"]["wage"] = Body.strip()
        sessions[phone]["step"] = "job_date"
        response.message("When do you need them? (e.g. Tomorrow, 20 June)")

    elif session["step"] == "job_date":
        job = sessions[phone]["job"]
        job["start_date"] = Body.strip()

        # Get farmer's location
        farmer = get_from_db("farmers", phone)
        location = farmer["location"] if farmer else "Unknown"

        # Save job to database
        save_to_db("jobs", {
            "farmer_phone": phone,
            "work_type": job["work_type"],
            "num_labourers": job["num_labourers"],
            "wage": job["wage"],
            "start_date": job["start_date"],
            "location": location,
            "status": "open"
        })

        response.message(
            f"✅ Job Posted Successfully!\n\n"
            f"📍 Location: {location}\n"
            f"🔨 Work: {job['work_type']}\n"
            f"👥 Labourers needed: {job['num_labourers']}\n"
            f"💰 Wage: ₹{job['wage']}/day\n"
            f"📅 Date: {job['start_date']}\n\n"
            f"We are notifying nearby labourers now!"
        )

        sessions[phone]["step"] = "done"

    return Response(content=str(response), media_type="application/xml")
