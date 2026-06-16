import os
import requests as req
from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv()

app = FastAPI()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = "whatsapp:+14155238886"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

sessions = {}

# ─── DATABASE HELPERS ───

def save_to_db(table, data, returning=False):
    headers = {**HEADERS, "Prefer": "return=representation"} if returning else HEADERS
    res = req.post(f"{SUPABASE_URL}/rest/v1/{table}", json=data, headers=headers)
    return res.json() if returning else res

def get_from_db(table, phone):
    encoded_phone = quote(phone, safe='')
    res = req.get(f"{SUPABASE_URL}/rest/v1/{table}?phone=eq.{encoded_phone}", headers=HEADERS)
    data = res.json()
    return data[0] if data else None

def update_db(table, filters, data):
    query = "&".join([f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items()])
    res = req.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{query}",
        json=data,
        headers={**HEADERS, "Prefer": "return=representation"}
    )
    return res.json()

def send_whatsapp(to, message):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=to)
    except Exception as e:
        print(f"WhatsApp send error: {e}")

def notify_nearby_labourers(job):
    encoded_location = quote(job["location"], safe='')
    labourers = req.get(
        f"{SUPABASE_URL}/rest/v1/labourers?location=eq.{encoded_location}",
        headers=HEADERS
    ).json()
    for labourer in labourers:
        if labourer["phone"] != job["farmer_phone"]:
            send_whatsapp(
                labourer["phone"],
                f"🔔 New Job Near You in {job['location']}!\n\n"
                f"🔨 Work: {job['work_type']}\n"
                f"👥 Labourers needed: {job['num_labourers']}\n"
                f"💰 Wage: ₹{job['wage']}/day\n"
                f"📅 Date: {job['start_date']}\n\n"
                f"Reply CONFIRM {job['id']} to accept this job."
            )

# ─── MAIN WEBHOOK ───

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

    # ─── START — CHECK IF ALREADY REGISTERED ───

    if session["step"] == "start":
        farmer = get_from_db("farmers", phone)
        labourer = get_from_db("labourers", phone)

        if farmer:
            sessions[phone] = {"step": "done", "role": "farmer"}
            response.message(
                f"Welcome back {farmer['name']}! 🌾\n\n"
                f"Reply:\n"
                f"POST JOB — Post a new job\n"
                f"MY JOBS — View your posted jobs"
            )
        elif labourer:
            sessions[phone] = {"step": "done", "role": "labourer"}
            response.message(
                f"Welcome back {labourer['name']}! 👋\n\n"
                f"Reply:\n"
                f"VIEW JOBS — See available jobs near you"
            )
        else:
            response.message(
                "🌾 Welcome to Farm Connect!\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )
            sessions[phone]["step"] = "role"

    # ─── REGISTRATION ───

    elif session["step"] == "role":
        if message in ["FARMER", "LABOURER"]:
            sessions[phone]["role"] = message.lower()
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
        role = sessions[phone]["role"]

        if role == "labourer":
            sessions[phone]["step"] = "skill"
            response.message(
                "What is your main skill?\n\n"
                "1. Harvesting\n"
                "2. Planting\n"
                "3. Irrigation\n"
                "4. Weeding\n"
                "5. General Labour\n\n"
                "Reply with the number or skill name."
            )
        else:
            save_to_db("farmers", {
                "phone": phone,
                "name": sessions[phone]["name"],
                "location": sessions[phone]["location"]
            })
            response.message(
                f"✅ Registered as Farmer!\n\n"
                f"Name: {sessions[phone]['name']}\n"
                f"Location: {sessions[phone]['location']}\n\n"
                f"Reply POST JOB to post a new job."
            )
            sessions[phone]["step"] = "done"

    elif session["step"] == "skill":
        skill_map = {
            "1": "Harvesting", "HARVESTING": "Harvesting",
            "2": "Planting", "PLANTING": "Planting",
            "3": "Irrigation", "IRRIGATION": "Irrigation",
            "4": "Weeding", "WEEDING": "Weeding",
            "5": "General Labour", "GENERAL LABOUR": "General Labour",
            "GENERAL": "General Labour"
        }
        skill = skill_map.get(message)
        if not skill:
            response.message("Please reply with a number 1-5 or skill name.")
        else:
            save_to_db("labourers", {
                "phone": phone,
                "name": sessions[phone]["name"],
                "location": sessions[phone]["location"],
                "skill": skill
            })
            response.message(
                f"✅ Registered as Labourer!\n\n"
                f"Name: {sessions[phone]['name']}\n"
                f"Location: {sessions[phone]['location']}\n"
                f"Skill: {skill}\n\n"
                f"Reply VIEW JOBS to see available jobs near you."
            )
            sessions[phone]["step"] = "done"

    # ─── MAIN MENU ───

    elif session["step"] == "done":

        # POST JOB
        if message == "POST JOB":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                response.message("❌ Only registered farmers can post jobs.")
            else:
                sessions[phone]["step"] = "job_work_type"
                sessions[phone]["job"] = {}
                response.message(
                    "📋 Let's post your job!\n\n"
                    "What type of work is needed?\n"
                    "(e.g. Harvesting, Planting, Irrigation, Weeding)"
                )

        # MY JOBS
        elif message == "MY JOBS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                response.message("❌ Only farmers can view their jobs.")
            else:
                encoded_phone = quote(phone, safe='')
                jobs = req.get(
                    f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{encoded_phone}&order=created_at.desc&limit=5",
                    headers=HEADERS
                ).json()
                if not jobs:
                    response.message("You haven't posted any jobs yet.\nReply POST JOB to post one.")
                else:
                    msg = "📋 Your Recent Jobs:\n\n"
                    for i, job in enumerate(jobs):
                        msg += (
                            f"{i+1}. {job['work_type']} — {job['location']}\n"
                            f"   👥 {job['num_labourers']} labourers | ₹{job['wage']}/day\n"
                            f"   📅 {job['start_date']} | {job['status'].upper()}\n"
                            f"   ID: {job['id']}\n\n"
                        )
                    msg += "Reply CANCEL [ID] to cancel a job."
                    response.message(msg)

        # VIEW JOBS
        elif message == "VIEW JOBS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                response.message("❌ Only registered labourers can view jobs.")
            else:
                encoded_location = quote(labourer["location"], safe='')
                jobs = req.get(
                    f"{SUPABASE_URL}/rest/v1/jobs?location=eq.{encoded_location}&status=eq.open&limit=5",
                    headers=HEADERS
                ).json()
                if not jobs:
                    response.message(
                        f"No open jobs in {labourer['location']} right now.\n"
                        f"We will notify you when new jobs are posted!"
                    )
                else:
                    msg = f"🔍 Open Jobs in {labourer['location']}:\n\n"
                    for i, job in enumerate(jobs):
                        msg += (
                            f"{i+1}. {job['work_type']}\n"
                            f"   👥 {job['num_labourers']} needed | ₹{job['wage']}/day\n"
                            f"   📅 {job['start_date']}\n"
                            f"   Reply CONFIRM {job['id']} to accept\n\n"
                        )
                    response.message(msg)

        # CONFIRM [job_id]
        elif message.startswith("CONFIRM"):
            parts = Body.strip().split()
            if len(parts) < 2:
                response.message("Please reply CONFIRM followed by job ID.\nExample: CONFIRM 1")
            else:
                job_id = parts[1]
                labourer = get_from_db("labourers", phone)
                if not labourer:
                    response.message("❌ Only registered labourers can confirm jobs.")
                else:
                    updated = update_db(
                        "jobs",
                        {"id": job_id, "status": "open"},
                        {"status": "confirmed", "labourer_phone": phone}
                    )
                    if not updated:
                        response.message("❌ Job not found or already confirmed.")
                    else:
                        job = updated[0]
                        send_whatsapp(
                            job["farmer_phone"],
                            f"✅ Job Confirmed!\n\n"
                            f"Labourer: {labourer['name']}\n"
                            f"Skill: {labourer.get('skill', 'General')}\n"
                            f"Work: {job['work_type']}\n"
                            f"Date: {job['start_date']}\n\n"
                            f"Your labourer will arrive on the job date. 🌾"
                        )
                        response.message(
                            f"✅ Job Confirmed!\n\n"
                            f"Work: {job['work_type']}\n"
                            f"Location: {job['location']}\n"
                            f"Date: {job['start_date']}\n"
                            f"Wage: ₹{job['wage']}/day\n\n"
                            f"Please arrive on time. Good luck! 💪"
                        )

        # CANCEL [job_id]
        elif message.startswith("CANCEL"):
            parts = Body.strip().split()
            if len(parts) < 2:
                response.message("Please reply CANCEL followed by job ID.\nExample: CANCEL 1")
            else:
                job_id = parts[1]
                encoded_phone = quote(phone, safe='')
                updated = update_db(
                    "jobs",
                    {"id": job_id, "farmer_phone": phone},
                    {"status": "cancelled"}
                )
                if not updated:
                    response.message("❌ Job not found or you don't own this job.")
                else:
                    response.message(f"✅ Job #{job_id} has been cancelled.")

        # UNKNOWN
        else:
            farmer = get_from_db("farmers", phone)
            if farmer:
                response.message(
                    f"Hello {farmer['name']}! 🌾\n\n"
                    f"Reply:\n"
                    f"POST JOB — Post a new job\n"
                    f"MY JOBS — View your posted jobs"
                )
            else:
                labourer = get_from_db("labourers", phone)
                if labourer:
                    response.message(
                        f"Hello {labourer['name']}! 👋\n\n"
                        f"Reply:\n"
                        f"VIEW JOBS — See available jobs near you"
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
        farmer = get_from_db("farmers", phone)
        location = farmer["location"] if farmer else "Unknown"

        saved = save_to_db("jobs", {
            "farmer_phone": phone,
            "work_type": job["work_type"],
            "num_labourers": job["num_labourers"],
            "wage": job["wage"],
            "start_date": job["start_date"],
            "location": location,
            "status": "open"
        }, returning=True)

        response.message(
            f"✅ Job Posted Successfully!\n\n"
            f"📍 Location: {location}\n"
            f"🔨 Work: {job['work_type']}\n"
            f"👥 Labourers needed: {job['num_labourers']}\n"
            f"💰 Wage: ₹{job['wage']}/day\n"
            f"📅 Date: {job['start_date']}\n\n"
            f"Notifying nearby labourers now! 🔔"
        )

        if saved and isinstance(saved, list) and len(saved) > 0:
            notify_nearby_labourers(saved[0])

        sessions[phone]["step"] = "done"

    return Response(content=str(response), media_type="application/xml")
