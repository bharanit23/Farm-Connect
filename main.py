import os
import traceback
import requests as req
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import PlainTextResponse
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
    "Prefer": "return=representation"
}

# In-memory sessions — survives within a single server process.
# On redeploy, sessions reset, but registered users are re-fetched from DB gracefully.
sessions = {}


# ─── DATABASE HELPERS ───────────────────────────────────────────────────────────

def save_to_db(table, data):
    """Insert a row and return the saved record (or None on error)."""
    try:
        res = req.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            json=data,
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        result = res.json()
        return result[0] if isinstance(result, list) and result else None
    except Exception as e:
        print(f"[save_to_db] Error saving to {table}: {e}\n{traceback.format_exc()}")
        return None


def get_from_db(table, phone):
    """Fetch a single row by phone number (returns dict or None)."""
    try:
        encoded_phone = quote(phone, safe="")
        res = req.get(
            f"{SUPABASE_URL}/rest/v1/{table}?phone=eq.{encoded_phone}",
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        data = res.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception as e:
        print(f"[get_from_db] Error fetching from {table}: {e}\n{traceback.format_exc()}")
        return None


def update_db(table, filters, data):
    """PATCH rows matching filters. Returns list of updated rows (or empty list)."""
    try:
        query = "&".join(
            f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items()
        )
        res = req.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{query}",
            json=data,
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        result = res.json()
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"[update_db] Error updating {table}: {e}\n{traceback.format_exc()}")
        return []


def get_jobs_by_phone(phone):
    """Fetch the 5 most recent jobs for a farmer."""
    try:
        encoded_phone = quote(phone, safe="")
        res = req.get(
            f"{SUPABASE_URL}/rest/v1/jobs"
            f"?farmer_phone=eq.{encoded_phone}"
            f"&order=created_at.desc&limit=5",
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[get_jobs_by_phone] Error: {e}\n{traceback.format_exc()}")
        return []


def get_open_jobs_by_location(location):
    """Fetch up to 5 open jobs matching the given location (case-insensitive)."""
    try:
        # FIX: ilike requires % wildcards for partial matching
        encoded_location = quote(f"%{location}%", safe="")
        res = req.get(
            f"{SUPABASE_URL}/rest/v1/jobs"
            f"?location=ilike.{encoded_location}"
            f"&status=eq.open&limit=5",
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[get_open_jobs_by_location] Error: {e}\n{traceback.format_exc()}")
        return []


def get_labourers_by_location(location):
    """Fetch all labourers in a given location for job notifications."""
    try:
        encoded_location = quote(f"%{location}%", safe="")
        res = req.get(
            f"{SUPABASE_URL}/rest/v1/labourers"
            f"?location=ilike.{encoded_location}",
            headers=HEADERS,
            timeout=10
        )
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[get_labourers_by_location] Error: {e}\n{traceback.format_exc()}")
        return []


# ─── TWILIO HELPERS ─────────────────────────────────────────────────────────────

def send_whatsapp(to, message):
    """Send a WhatsApp message via Twilio (out-of-band, not TwiML)."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=to)
    except Exception as e:
        print(f"[send_whatsapp] Error sending to {to}: {e}\n{traceback.format_exc()}")


def notify_nearby_labourers(job):
    """Notify all labourers in the same location about a new job."""
    labourers = get_labourers_by_location(job["location"])
    for labourer in labourers:
        # Don't notify the farmer themselves if they're also registered as labourer
        if labourer.get("phone") != job.get("farmer_phone"):
            send_whatsapp(
                labourer["phone"],
                f"🔔 New Job Near You in {job['location']}!\n\n"
                f"🔨 Work: {job['work_type']}\n"
                f"👥 Labourers needed: {job['num_labourers']}\n"
                f"💰 Wage: ₹{job['wage']}/day\n"
                f"📅 Date: {job['start_date']}\n\n"
                f"Reply CONFIRM {job['id']} to accept this job."
            )


def twiml_response(text):
    """Helper to build a TwiML XML response string."""
    r = MessagingResponse()
    r.message(text)
    return Response(content=str(r), media_type="application/xml")


# ─── ROUTES ─────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    """Health check endpoint — supports both GET and HEAD (Render uses HEAD)."""
    return {"message": "Farm Connect API is running"}


@app.post("/webhook")
async def whatsapp_webhook(
    Body: str = Form(...),
    From: str = Form(...)
):
    try:
        raw_body = Body.strip()
        message = raw_body.upper()
        phone = From

        # Initialise session for new callers
        if phone not in sessions:
            sessions[phone] = {"step": "start"}

        session = sessions[phone]
        step = session.get("step", "start")

        # ── START: identify returning vs new user ────────────────────────────

        if step == "start":
            farmer = get_from_db("farmers", phone)
            if farmer:
                sessions[phone] = {"step": "done", "role": "farmer"}
                return twiml_response(
                    f"Welcome back {farmer['name']}! 🌾\n\n"
                    f"Reply:\n"
                    f"POST JOB — Post a new job\n"
                    f"MY JOBS — View your posted jobs"
                )

            labourer = get_from_db("labourers", phone)
            if labourer:
                sessions[phone] = {"step": "done", "role": "labourer"}
                return twiml_response(
                    f"Welcome back {labourer['name']}! 👋\n\n"
                    f"Reply:\n"
                    f"VIEW JOBS — See available jobs near you"
                )

            sessions[phone]["step"] = "role"
            return twiml_response(
                "🌾 Welcome to Farm Connect!\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )

        # ── REGISTRATION FLOW ────────────────────────────────────────────────

        elif step == "role":
            if message in ("FARMER", "LABOURER"):
                sessions[phone]["role"] = message.lower()
                sessions[phone]["step"] = "name"
                return twiml_response("Great! What is your name?")
            else:
                return twiml_response("Please reply with FARMER or LABOURER only.")

        elif step == "name":
            sessions[phone]["name"] = raw_body
            sessions[phone]["step"] = "location"
            return twiml_response(
                f"Nice to meet you {raw_body}! "
                f"What is your village or town name?"
            )

        elif step == "location":
            sessions[phone]["location"] = raw_body.title()
            role = sessions[phone].get("role")

            if role == "labourer":
                sessions[phone]["step"] = "skill"
                return twiml_response(
                    "What is your main skill?\n\n"
                    "1. Harvesting\n"
                    "2. Planting\n"
                    "3. Irrigation\n"
                    "4. Weeding\n"
                    "5. General Labour\n\n"
                    "Reply with the number or skill name."
                )
            else:
                saved = save_to_db("farmers", {
                    "phone": phone,
                    "name": sessions[phone]["name"],
                    "location": sessions[phone]["location"]
                })
                if not saved:
                    return twiml_response(
                        "⚠️ There was an error saving your details. Please try again."
                    )
                sessions[phone]["step"] = "done"
                return twiml_response(
                    f"✅ Registered as Farmer!\n\n"
                    f"Name: {sessions[phone]['name']}\n"
                    f"Location: {sessions[phone]['location']}\n\n"
                    f"Reply POST JOB to post a new job."
                )

        elif step == "skill":
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
                return twiml_response("Please reply with a number 1-5 or skill name.")

            saved = save_to_db("labourers", {
                "phone": phone,
                "name": sessions[phone]["name"],
                "location": sessions[phone]["location"],
                "skill": skill
            })
            if not saved:
                return twiml_response(
                    "⚠️ There was an error saving your details. Please try again."
                )
            sessions[phone]["step"] = "done"
            sessions[phone]["role"] = "labourer"
            return twiml_response(
                f"✅ Registered as Labourer!\n\n"
                f"Name: {sessions[phone]['name']}\n"
                f"Location: {sessions[phone]['location']}\n"
                f"Skill: {skill}\n\n"
                f"Reply VIEW JOBS to see available jobs near you."
            )

        # ── MAIN MENU ────────────────────────────────────────────────────────

        elif step == "done":

            # POST JOB
            if message == "POST JOB":
                farmer = get_from_db("farmers", phone)
                if not farmer:
                    return twiml_response("❌ Only registered farmers can post jobs.")
                sessions[phone]["step"] = "job_work_type"
                sessions[phone]["job"] = {}
                return twiml_response(
                    "📋 Let's post your job!\n\n"
                    "What type of work is needed?\n"
                    "(e.g. Harvesting, Planting, Irrigation, Weeding)"
                )

            # MY JOBS
            elif message == "MY JOBS":
                farmer = get_from_db("farmers", phone)
                if not farmer:
                    return twiml_response("❌ Only farmers can view their jobs.")
                jobs = get_jobs_by_phone(phone)
                if not jobs:
                    return twiml_response(
                        "You haven't posted any jobs yet.\n"
                        "Reply POST JOB to post one."
                    )
                msg = "📋 Your Recent Jobs:\n\n"
                for i, job in enumerate(jobs):
                    msg += (
                        f"{i+1}. {job['work_type']} — {job['location']}\n"
                        f"   👥 {job['num_labourers']} labourers | ₹{job['wage']}/day\n"
                        f"   📅 {job['start_date']} | {job['status'].upper()}\n"
                        f"   ID: {job['id']}\n\n"
                    )
                msg += "Reply CANCEL [ID] to cancel a job."
                return twiml_response(msg)

            # VIEW JOBS
            elif message == "VIEW JOBS":
                labourer = get_from_db("labourers", phone)
                if not labourer:
                    return twiml_response("❌ Only registered labourers can view jobs.")
                jobs = get_open_jobs_by_location(labourer["location"])
                if not jobs:
                    return twiml_response(
                        f"No open jobs in {labourer['location']} right now.\n"
                        f"We will notify you when new jobs are posted!"
                    )
                msg = f"🔍 Open Jobs in {labourer['location']}:\n\n"
                for i, job in enumerate(jobs):
                    msg += (
                        f"{i+1}. {job['work_type']}\n"
                        f"   👥 {job['num_labourers']} needed | ₹{job['wage']}/day\n"
                        f"   📅 {job['start_date']}\n"
                        f"   Reply CONFIRM {job['id']} to accept\n\n"
                    )
                return twiml_response(msg)

            # CONFIRM [job_id]
            elif message.startswith("CONFIRM"):
                parts = raw_body.split()
                if len(parts) < 2:
                    return twiml_response(
                        "Please reply CONFIRM followed by job ID.\n"
                        "Example: CONFIRM 1"
                    )
                job_id = parts[1]
                labourer = get_from_db("labourers", phone)
                if not labourer:
                    return twiml_response("❌ Only registered labourers can confirm jobs.")
                updated = update_db(
                    "jobs",
                    {"id": job_id, "status": "open"},
                    {"status": "confirmed", "labourer_phone": phone}
                )
                if not updated:
                    return twiml_response("❌ Job not found or already confirmed.")
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
                return twiml_response(
                    f"✅ Job Confirmed!\n\n"
                    f"Work: {job['work_type']}\n"
                    f"Location: {job['location']}\n"
                    f"Date: {job['start_date']}\n"
                    f"Wage: ₹{job['wage']}/day\n\n"
                    f"Please arrive on time. Good luck! 💪"
                )

            # CANCEL [job_id]
            elif message.startswith("CANCEL"):
                parts = raw_body.split()
                if len(parts) < 2:
                    return twiml_response(
                        "Please reply CANCEL followed by job ID.\n"
                        "Example: CANCEL 1"
                    )
                job_id = parts[1]
                updated = update_db(
                    "jobs",
                    {"id": job_id, "farmer_phone": phone},
                    {"status": "cancelled"}
                )
                if not updated:
                    return twiml_response(
                        "❌ Job not found or you don't own this job."
                    )
                return twiml_response(f"✅ Job #{job_id} has been cancelled.")

            # UNKNOWN COMMAND
            else:
                farmer = get_from_db("farmers", phone)
                if farmer:
                    return twiml_response(
                        f"Hello {farmer['name']}! 🌾\n\n"
                        f"Reply:\n"
                        f"POST JOB — Post a new job\n"
                        f"MY JOBS — View your posted jobs"
                    )
                labourer = get_from_db("labourers", phone)
                if labourer:
                    return twiml_response(
                        f"Hello {labourer['name']}! 👋\n\n"
                        f"Reply:\n"
                        f"VIEW JOBS — See available jobs near you"
                    )
                # Fallback: session says done but user not in DB — restart
                sessions[phone] = {"step": "start"}
                return twiml_response(
                    "🌾 Welcome to Farm Connect!\n\n"
                    "Are you a FARMER or LABOURER?\n"
                    "Reply FARMER or LABOURER to get started."
                )

        # ── JOB POSTING FLOW ─────────────────────────────────────────────────

        elif step == "job_work_type":
            sessions[phone]["job"]["work_type"] = raw_body
            sessions[phone]["step"] = "job_num_labourers"
            return twiml_response("How many labourers do you need?")

        elif step == "job_num_labourers":
            if not raw_body.isdigit():
                return twiml_response(
                    "Please enter a number. How many labourers do you need?"
                )
            sessions[phone]["job"]["num_labourers"] = int(raw_body)
            sessions[phone]["step"] = "job_wage"
            return twiml_response("What is the wage per day? (in ₹)")

        elif step == "job_wage":
            if not raw_body.replace(".", "", 1).isdigit():
                return twiml_response(
                    "Please enter a valid amount (e.g. 600). What is the wage per day?"
                )
            sessions[phone]["job"]["wage"] = raw_body
            sessions[phone]["step"] = "job_date"
            return twiml_response("When do you need them? (e.g. 20 June, Tomorrow)")

        elif step == "job_date":
            job = sessions[phone]["job"]
            job["start_date"] = raw_body

            farmer = get_from_db("farmers", phone)
            if not farmer:
                sessions[phone]["step"] = "done"
                return twiml_response("❌ Could not find your farmer profile. Please try again.")

            location = farmer.get("location", "Unknown")

            saved = save_to_db("jobs", {
                "farmer_phone": phone,
                "work_type": job["work_type"],
                "num_labourers": job["num_labourers"],
                "wage": job["wage"],
                "start_date": job["start_date"],
                "location": location,
                "status": "open"
            })

            sessions[phone]["step"] = "done"

            if not saved:
                return twiml_response(
                    "⚠️ There was an error posting your job. Please try again by sending POST JOB."
                )

            notify_nearby_labourers(saved)

            return twiml_response(
                f"✅ Job Posted Successfully!\n\n"
                f"📍 Location: {location}\n"
                f"🔨 Work: {job['work_type']}\n"
                f"👥 Labourers needed: {job['num_labourers']}\n"
                f"💰 Wage: ₹{job['wage']}/day\n"
                f"📅 Date: {job['start_date']}\n\n"
                f"Notifying nearby labourers now! 🔔"
            )

        # ── FALLBACK: unknown session step ───────────────────────────────────
        else:
            print(f"[webhook] Unknown session step '{step}' for {phone} — resetting.")
            sessions[phone] = {"step": "start"}
            return twiml_response(
                "Something went wrong. Let's start over.\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )

    except Exception:
        print(f"[webhook] Unhandled exception:\n{traceback.format_exc()}")
        err = MessagingResponse()
        err.message("⚠️ Something went wrong on our end. Please try again in a moment.")
        return Response(content=str(err), media_type="application/xml")
