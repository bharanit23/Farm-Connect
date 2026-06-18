import os
import re
import traceback
import threading
import time
from datetime import datetime, date
import requests as req
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv()
app = FastAPI()

# ── Keep-alive: prevents Render free tier from spinning down ──────────────────
def keep_alive():
    while True:
        time.sleep(840)
        try:
            req.get("https://farm-connect-yjg8.onrender.com/ping", timeout=10)
            print("[KEEP-ALIVE] Pinged successfully")
        except Exception as e:
            print(f"[KEEP-ALIVE] Ping failed: {e}")
threading.Thread(target=keep_alive, daemon=True).start()

# ── Env vars ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = "whatsapp:+14155238886"

print("=== STARTUP ENV CHECK ===")
print(f"SUPABASE_URL  : {'SET' if SUPABASE_URL else '*** MISSING ***'}")
print(f"SUPABASE_KEY  : {'SET' if SUPABASE_KEY else '*** MISSING ***'}")
print(f"TWILIO_SID    : {'SET' if TWILIO_ACCOUNT_SID else '*** MISSING ***'}")
print(f"TWILIO_TOKEN  : {'SET' if TWILIO_AUTH_TOKEN else '*** MISSING ***'}")
print("=========================")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

sessions = {}

# ── Fuzzy near-miss helper ────────────────────────────────────────────────────
def _edit_distance(a, b):
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        ndp = [i + 1]
        for j, cb in enumerate(b):
            ndp.append(min(dp[j] + (ca != cb), dp[j + 1] + 1, ndp[-1] + 1))
        dp = ndp
    return dp[-1]

KNOWN_COMMANDS = [
    "POST JOB", "MY JOBS", "MY LABOURERS", "VIEW JOBS",
    "CONFIRM", "CANCEL", "RATE",
    "LIST EQUIPMENT", "VIEW EQUIPMENT", "MY EQUIPMENT",
]

def fuzzy_suggestion(message, threshold=2):
    HINTS = {
        "RATE":           "Format: RATE [job_id] [stars 1–5]  •  Example: RATE 12 5",
        "CANCEL":         "Format: CANCEL [job_id]  •  Example: CANCEL 7",
        "CONFIRM":        "Format: CONFIRM [job_id]  •  Example: CONFIRM 3",
        "POST JOB":       "Just send: POST JOB",
        "MY JOBS":        "Just send: MY JOBS",
        "MY LABOURERS":   "Just send: MY LABOURERS",
        "VIEW JOBS":      "Just send: VIEW JOBS",
        "LIST EQUIPMENT": "Just send: LIST EQUIPMENT",
        "VIEW EQUIPMENT": "Just send: VIEW EQUIPMENT",
        "MY EQUIPMENT":   "Just send: MY EQUIPMENT",
    }
    for cmd in KNOWN_COMMANDS:
        if message.startswith(cmd) and message != cmd:
            remainder = message[len(cmd):]
            if remainder and not remainder.startswith(" "):
                return cmd, HINTS[cmd]
    first_token = message.split()[0] if message.split() else message
    for cmd in KNOWN_COMMANDS:
        cmd_first = cmd.split()[0]
        if _edit_distance(first_token, cmd_first) <= threshold:
            if len(first_token) >= 3:
                return cmd, HINTS[cmd]
    for cmd in KNOWN_COMMANDS:
        if abs(len(message) - len(cmd)) <= threshold:
            if _edit_distance(message, cmd) <= threshold:
                return cmd, HINTS[cmd]
    return None, None

# ── Date parsing / validation helper ─────────────────────────────────────────
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
DATE_FORMATS = [
    "%d %B %Y", "%d %b %Y", "%d %B", "%d %b",
    "%d-%m-%Y", "%d/%m/%Y", "%d-%m", "%d/%m",
    "%B %d %Y", "%b %d %Y", "%B %d", "%b %d",
    "%Y-%m-%d",
]

def parse_relative_date(text):
    t = text.strip().lower()
    today = date.today()
    if t == "today":
        return today
    if t == "tomorrow":
        return today.fromordinal(today.toordinal() + 1)
    if t == "next week":
        return today.fromordinal(today.toordinal() + 7)
    return None

def parse_job_date(raw_text):
    text = raw_text.strip()
    today = date.today()
    rel = parse_relative_date(text)
    if rel:
        return rel, None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text.title() if "%B" in fmt or "%b" in fmt else text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            candidate = parsed.replace(year=today.year).date()
            if candidate < today:
                candidate = candidate.replace(year=today.year + 1)
            return candidate, None
        else:
            return parsed.date(), None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
    m = re.match(r"^(\d{1,2})\s+([a-zA-Z]+)(?:\s+(\d{4}))?$", cleaned.strip())
    if not m:
        m = re.match(r"^([a-zA-Z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?$", cleaned.strip())
        if m:
            month_name, day_str, year_str = m.group(1), m.group(2), m.group(3)
        else:
            month_name = day_str = year_str = None
    else:
        day_str, month_name, year_str = m.group(1), m.group(2), m.group(3)
    if month_name:
        month_key = month_name.lower()
        month_num = MONTHS.get(month_key)
        if month_num and day_str.isdigit():
            day_num = int(day_str)
            year_num = int(year_str) if year_str else today.year
            try:
                candidate = date(year_num, month_num, day_num)
            except ValueError:
                return None, "❓ That doesn't look like a valid date. Please use a format like '20 June 2026' or 'Tomorrow'."
            if not year_str and candidate < today:
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate, None
    return None, "❓ Couldn't understand that date.\n\nPlease reply with a date like '20 June 2026', '20/06/2026', or 'Tomorrow'."

def validate_future_date(raw_text):
    parsed, err = parse_job_date(raw_text)
    if err:
        return False, None, err
    today = date.today()
    if parsed < today:
        return False, None, (
            f"❌ That date ({parsed.strftime('%d %B %Y')}) is in the past.\n\n"
            f"Please enter a future date (today or later), e.g. "
            f"'{today.strftime('%d %B %Y')}' or 'Tomorrow'."
        )
    return True, parsed.strftime("%d %B %Y"), None

# ── Database helpers ──────────────────────────────────────────────────────────
def save_to_db(table, data):
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        print(f"[DB] POST {url} | data={data}")
        res = req.post(url, json=data, headers=HEADERS, timeout=10)
        print(f"[DB] POST status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        result = res.json()
        return result[0] if isinstance(result, list) and result else None
    except Exception as e:
        print(f"[DB] save_to_db ERROR: {e}\n{traceback.format_exc()}")
        return None

def get_from_db(table, phone):
    try:
        encoded_phone = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/{table}?phone=eq.{encoded_phone}"
        print(f"[DB] GET {url}")
        res = req.get(url, headers=HEADERS, timeout=10)
        print(f"[DB] GET status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        data = res.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception as e:
        print(f"[DB] get_from_db ERROR: {e}\n{traceback.format_exc()}")
        return None

def update_db(table, filters, data):
    try:
        query = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
        url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
        print(f"[DB] PATCH {url} | data={data}")
        res = req.patch(url, json=data, headers=HEADERS, timeout=10)
        print(f"[DB] PATCH status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        result = res.json()
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"[DB] update_db ERROR: {e}\n{traceback.format_exc()}")
        return []

def get_jobs_by_phone(phone):
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?farmer_phone=eq.{encoded_phone}&order=created_at.desc&limit=5")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_jobs_by_phone ERROR: {e}")
        return []

def get_open_jobs_by_location(location):
    try:
        encoded_location = quote(f"%{location}%", safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?location=ilike.{encoded_location}&status=eq.open&limit=5")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_open_jobs_by_location ERROR: {e}")
        return []

def get_labourers_by_location(location):
    try:
        encoded_location = quote(f"%{location}%", safe="")
        url = f"{SUPABASE_URL}/rest/v1/labourers?location=ilike.{encoded_location}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_labourers_by_location ERROR: {e}")
        return []

def get_confirmed_jobs_for_farmer(phone):
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?farmer_phone=eq.{encoded_phone}&status=eq.confirmed"
               f"&order=start_date.desc&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_confirmed_jobs_for_farmer ERROR: {e}")
        return []

# ── Equipment DB helpers ──────────────────────────────────────────────────────
def save_equipment(data):
    return save_to_db("equipment", data)

def get_equipment_by_owner(phone):
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/equipment"
               f"?owner_phone=eq.{encoded_phone}&order=created_at.desc&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_equipment_by_owner ERROR: {e}")
        return []

def get_equipment_by_location(location):
    try:
        encoded_location = quote(f"%{location}%", safe="")
        url = (f"{SUPABASE_URL}/rest/v1/equipment"
               f"?location=ilike.{encoded_location}&available=eq.true&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_equipment_by_location ERROR: {e}")
        return []

# ── Twilio helpers ────────────────────────────────────────────────────────────
def send_whatsapp(to, message):
    try:
        print(f"[TWILIO] Sending to {to}: {message[:80]}")
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=to)
        print(f"[TWILIO] Sent OK — SID={msg.sid}")
    except Exception as e:
        print(f"[TWILIO] ERROR sending to {to}: {e}")

def notify_nearby_labourers(job):
    labourers = get_labourers_by_location(job["location"])
    print(f"[NOTIFY] Found {len(labourers)} labourer(s) near {job['location']}")
    for labourer in labourers:
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
    print(f"[REPLY] {text[:120]}")
    r = MessagingResponse()
    r.message(text)
    return Response(content=str(r), media_type="application/xml")

# ── Core message handler (returns plain text string) ─────────────────────────
def handle_message(phone: str, raw_body: str) -> str:
    raw_body = raw_body.strip()
    message  = raw_body.upper()

    print(f"\n{'='*60}")
    print(f"[WEBHOOK] FROM={phone} | RAW='{raw_body}' | UPPER='{message}'")

    if phone not in sessions:
        sessions[phone] = {"step": "start"}

    step = sessions[phone].get("step", "start")
    print(f"[SESSION] step='{step}' | session={sessions[phone]}")

    # ── START ─────────────────────────────────────────────────────────────────
    if step == "start":
        farmer = get_from_db("farmers", phone)
        if farmer:
            sessions[phone] = {"step": "done", "role": "farmer"}
            return (
                f"Welcome back {farmer['name']}! 🌾\n\n"
                f"Reply:\n"
                f"POST JOB — Post a new job\n"
                f"MY JOBS — View your posted jobs\n"
                f"MY LABOURERS — See confirmed jobs & rate labourers\n"
                f"LIST EQUIPMENT — Rent out your equipment\n"
                f"MY EQUIPMENT — View your equipment listings"
            )
        labourer = get_from_db("labourers", phone)
        if labourer:
            sessions[phone] = {"step": "done", "role": "labourer"}
            return (
                f"Welcome back {labourer['name']}! 👋\n\n"
                f"Reply:\n"
                f"VIEW JOBS — See available jobs near you\n"
                f"VIEW EQUIPMENT — Browse equipment for rent near you"
            )
        sessions[phone]["step"] = "role"
        return (
            "🌾 Welcome to Farm Connect!\n\n"
            "Are you a FARMER or LABOURER?\n"
            "Reply FARMER or LABOURER to get started."
        )

    # ── REGISTRATION ──────────────────────────────────────────────────────────
    elif step == "role":
        if message in ("FARMER", "LABOURER"):
            sessions[phone]["role"] = message.lower()
            sessions[phone]["step"] = "name"
            return "Great! What is your name?"
        return "Please reply with FARMER or LABOURER only."

    elif step == "name":
        sessions[phone]["name"] = raw_body
        sessions[phone]["step"] = "location"
        return f"Nice to meet you {raw_body}! What is your village or town name?"

    elif step == "location":
        sessions[phone]["location"] = raw_body.title()
        role = sessions[phone].get("role")
        if role == "labourer":
            sessions[phone]["step"] = "skill"
            return (
                "What is your main skill?\n\n"
                "1. Harvesting\n2. Planting\n3. Irrigation\n4. Weeding\n5. General Labour\n\n"
                "Reply with the number or skill name."
            )
        else:
            saved = save_to_db("farmers", {
                "phone": phone,
                "name": sessions[phone]["name"],
                "location": sessions[phone]["location"]
            })
            if not saved:
                return "⚠️ Error saving your details. Please try again."
            sessions[phone]["step"] = "done"
            return (
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
            return "Please reply with a number 1-5 or skill name."
        saved = save_to_db("labourers", {
            "phone": phone,
            "name": sessions[phone]["name"],
            "location": sessions[phone]["location"],
            "skill": skill
        })
        if not saved:
            return "⚠️ Error saving your details. Please try again."
        sessions[phone]["step"] = "done"
        sessions[phone]["role"] = "labourer"
        return (
            f"✅ Registered as Labourer!\n\n"
            f"Name: {sessions[phone]['name']}\n"
            f"Location: {sessions[phone]['location']}\n"
            f"Skill: {skill}\n\n"
            f"Reply VIEW JOBS to see available jobs near you."
        )

    # ── MAIN MENU ─────────────────────────────────────────────────────────────
    elif step == "done":
        print(f"[FLOW] DONE menu — message='{message}'")

        if message == "POST JOB":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only registered farmers can post jobs."
            sessions[phone]["step"] = "job_work_type"
            sessions[phone]["job"] = {}
            return (
                "📋 Let's post your job!\n\n"
                "What type of work is needed?\n"
                "(e.g. Harvesting, Planting, Irrigation, Weeding)"
            )

        elif message == "MY LABOURERS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can use this command."
            jobs = get_confirmed_jobs_for_farmer(phone)
            if not jobs:
                return "No confirmed jobs found.\nReply POST JOB to post one."
            msg = "👥 Your Confirmed Jobs:\n\n"
            for job in jobs:
                rated = "✅ Rated" if job.get("rated") else "⭐ Not rated"
                labourer_phone = job.get("labourer_phone")
                labourer = get_from_db("labourers", labourer_phone) if labourer_phone else None
                labourer_name = labourer["name"] if labourer else "Unknown"
                msg += (
                    f"ID: {job['id']}\n"
                    f"Work: {job['work_type']} | Date: {job['start_date']}\n"
                    f"Labourer: {labourer_name}\n"
                    f"Status: {rated}\n\n"
                )
            msg += "Reply RATE [job_id] [1-5] to rate a labourer."
            return msg

        elif message.startswith("RATE"):
            parts = raw_body.split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: RATE [job_id] [stars 1–5]\nExample: RATE 12 5"
            job_id, stars = parts[1], int(parts[2])
            if stars < 1 or stars > 5:
                return "Stars must be between 1 and 5."
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can rate labourers."
            try:
                url = f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}&farmer_phone=eq.{quote(phone, safe='')}"
                res = req.get(url, headers=HEADERS, timeout=10)
                jobs = res.json()
            except Exception:
                return "❌ Could not fetch job. Try again."
            if not jobs:
                return "❌ Job not found or doesn't belong to you."
            job = jobs[0]
            if job.get("rated"):
                return "You've already rated this job."
            if job["status"] != "confirmed":
                return "❌ Can only rate confirmed jobs."
            labourer_phone = job.get("labourer_phone")
            if not labourer_phone:
                return "❌ No labourer assigned to this job."
            labourer = get_from_db("labourers", labourer_phone)
            if not labourer:
                return "❌ Labourer not found."
            old_total  = labourer.get("total_ratings", 0)
            old_rating = labourer.get("rating", 0)
            new_total  = old_total + 1
            new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
            update_db("labourers", {"phone": labourer_phone}, {"rating": new_rating, "total_ratings": new_total})
            update_db("jobs", {"id": job_id}, {"rated": True})
            return (
                f"✅ Rated {labourer['name']} — {stars} stars!\n"
                f"Their new rating: {new_rating}⭐ ({new_total} ratings)"
            )

        elif message == "MY JOBS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can view their jobs."
            jobs = get_jobs_by_phone(phone)
            if not jobs:
                return "You haven't posted any jobs yet.\nReply POST JOB to post one."
            msg = "📋 Your Recent Jobs:\n\n"
            for i, job in enumerate(jobs):
                msg += (
                    f"{i+1}. {job['work_type']} — {job['location']}\n"
                    f"   👥 {job['num_labourers']} labourers | ₹{job['wage']}/day\n"
                    f"   📅 {job['start_date']} | {job['status'].upper()}\n"
                    f"   ID: {job['id']}\n\n"
                )
            msg += "Reply CANCEL [ID] to cancel a job."
            return msg

        elif message == "VIEW JOBS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only registered labourers can view jobs."
            jobs = get_open_jobs_by_location(labourer["location"])
            if not jobs:
                return (
                    f"No open jobs in {labourer['location']} right now.\n"
                    f"We'll notify you when new jobs are posted!"
                )
            msg = f"🔍 Open Jobs in {labourer['location']}:\n\n"
            for i, job in enumerate(jobs):
                msg += (
                    f"{i+1}. {job['work_type']}\n"
                    f"   👥 {job['num_labourers']} needed | ₹{job['wage']}/day\n"
                    f"   📅 {job['start_date']}\n"
                    f"   Reply CONFIRM {job['id']} to accept\n\n"
                )
            return msg

        elif message.startswith("CONFIRM"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CONFIRM [job_id]\nExample: CONFIRM 3"
            job_id   = parts[1]
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only registered labourers can confirm jobs."
            updated = update_db(
                "jobs",
                {"id": job_id, "status": "open"},
                {"status": "confirmed", "labourer_phone": phone}
            )
            if not updated:
                return "❌ Job not found or already confirmed."
            job = updated[0]
            send_whatsapp(
                job["farmer_phone"],
                f"✅ Job Confirmed!\n\nLabourer: {labourer['name']}\n"
                f"Skill: {labourer.get('skill', 'General')}\n"
                f"Work: {job['work_type']}\nDate: {job['start_date']}\n\n"
                f"Your labourer will arrive on the job date. 🌾"
            )
            return (
                f"✅ Job Confirmed!\n\n"
                f"Work: {job['work_type']}\nLocation: {job['location']}\n"
                f"Date: {job['start_date']}\nWage: ₹{job['wage']}/day\n\n"
                f"Please arrive on time. Good luck! 💪"
            )

        elif message.startswith("CANCEL"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CANCEL [job_id]\nExample: CANCEL 7"
            job_id  = parts[1]
            updated = update_db(
                "jobs",
                {"id": job_id, "farmer_phone": phone},
                {"status": "cancelled"}
            )
            if not updated:
                return "❌ Job not found or you don't own this job."
            job = updated[0]
            labourer_phone = job.get("labourer_phone")
            if labourer_phone:
                send_whatsapp(
                    labourer_phone,
                    f"⚠️ Job Cancelled\n\nWork: {job['work_type']}\n"
                    f"Location: {job['location']}\nDate: {job['start_date']}\n\n"
                    f"This job has been cancelled by the farmer. Sorry for the inconvenience."
                )
                print(f"[CANCEL] Notified labourer {labourer_phone} for job {job_id}")
            else:
                print(f"[CANCEL] No labourer on job {job_id} — no notification")
            return f"✅ Job #{job_id} has been cancelled."

        # ── EQUIPMENT COMMANDS ────────────────────────────────────────────────

        elif message == "LIST EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only registered farmers can list equipment."
            sessions[phone]["step"] = "equip_name"
            sessions[phone]["equip"] = {}
            return (
                "🚜 Let's list your equipment!\n\n"
                "What equipment do you want to rent out?\n"
                "(e.g. Tractor, Rotavator, Sprayer, Thresher)"
            )

        elif message == "VIEW EQUIPMENT":
            user = get_from_db("farmers", phone) or get_from_db("labourers", phone)
            if not user:
                return "❌ Please register first to view equipment."
            location = user.get("location", "")
            items = get_equipment_by_location(location)
            if not items:
                return (
                    f"No equipment available for rent in {location} right now.\n"
                    f"Check back later!"
                )
            msg = f"🚜 Equipment Available in {location}:\n\n"
            for i, item in enumerate(items):
                msg += (
                    f"{i+1}. {item['name']}\n"
                    f"   💰 ₹{item['rent_per_day']}/day\n"
                    f"   📅 Available until: {item.get('available_until') or 'Ongoing'}\n\n"
                )
            msg += "Contact the owner through your local Farm Connect agent to book."
            return msg

        elif message == "MY EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can manage equipment listings."
            items = get_equipment_by_owner(phone)
            if not items:
                return "You haven't listed any equipment yet.\nReply LIST EQUIPMENT to add one."
            msg = "🚜 Your Equipment Listings:\n\n"
            for i, item in enumerate(items):
                status = "✅ Available" if item.get("available") else "❌ Unavailable"
                msg += (
                    f"{i+1}. {item['name']}\n"
                    f"   💰 ₹{item['rent_per_day']}/day | {status}\n"
                    f"   📅 Until: {item.get('available_until') or 'Ongoing'}\n"
                    f"   ID: {item['id']}\n\n"
                )
            return msg

        else:
            suggestion, hint = fuzzy_suggestion(message)
            if suggestion:
                return (
                    f"❓ Unknown command. Did you mean *{suggestion}*?\n\n"
                    f"{hint}\n\nSend it exactly as shown to continue."
                )
            farmer = get_from_db("farmers", phone)
            if farmer:
                return (
                    f"❌ Unknown command.\n\nHello {farmer['name']}! 🌾 Available commands:\n"
                    f"POST JOB — Post a new job\n"
                    f"MY JOBS — View your posted jobs\n"
                    f"MY LABOURERS — See confirmed jobs & rate labourers\n"
                    f"LIST EQUIPMENT — Rent out your equipment\n"
                    f"MY EQUIPMENT — View your equipment listings"
                )
            labourer = get_from_db("labourers", phone)
            if labourer:
                return (
                    f"❌ Unknown command.\n\nHello {labourer['name']}! 👋 Available commands:\n"
                    f"VIEW JOBS — See available jobs near you\n"
                    f"VIEW EQUIPMENT — Browse equipment for rent near you"
                )
            sessions[phone] = {"step": "start"}
            return (
                "🌾 Welcome to Farm Connect!\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )

    # ── JOB POSTING FLOW ──────────────────────────────────────────────────────
    elif step == "job_work_type":
        sessions[phone]["job"]["work_type"] = raw_body
        sessions[phone]["step"] = "job_num_labourers"
        return "How many labourers do you need?"

    elif step == "job_num_labourers":
        if not raw_body.isdigit():
            return "Please enter a number. How many labourers do you need?"
        sessions[phone]["job"]["num_labourers"] = int(raw_body)
        sessions[phone]["step"] = "job_wage"
        return "What is the wage per day? (in ₹)"

    elif step == "job_wage":
        if not raw_body.replace(".", "", 1).isdigit():
            return "Please enter a valid amount (e.g. 600). What is the wage per day?"
        sessions[phone]["job"]["wage"] = raw_body
        sessions[phone]["step"] = "job_date"
        return "When do you need them? (e.g. 20 June 2026, Tomorrow)"

    elif step == "job_date":
        is_valid, normalized_date, err = validate_future_date(raw_body)
        if not is_valid:
            return err
        job = sessions[phone]["job"]
        job["start_date"] = normalized_date
        farmer = get_from_db("farmers", phone)
        if not farmer:
            sessions[phone]["step"] = "done"
            return "❌ Could not find your farmer profile. Please try again."
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
            return "⚠️ Error posting your job. Please try again by sending POST JOB."
        notify_nearby_labourers(saved)
        return (
            f"✅ Job Posted Successfully!\n\n"
            f"📍 Location: {location}\n"
            f"🔨 Work: {job['work_type']}\n"
            f"👥 Labourers needed: {job['num_labourers']}\n"
            f"💰 Wage: ₹{job['wage']}/day\n"
            f"📅 Date: {job['start_date']}\n\n"
            f"Notifying nearby labourers now! 🔔"
        )

    # ── EQUIPMENT LISTING FLOW ────────────────────────────────────────────────
    elif step == "equip_name":
        sessions[phone]["equip"]["name"] = raw_body
        sessions[phone]["step"] = "equip_rent"
        return f"What is the rent per day for your {raw_body}? (in ₹)"

    elif step == "equip_rent":
        if not raw_body.replace(".", "", 1).isdigit():
            return "Please enter a valid amount (e.g. 500). What is the rent per day?"
        sessions[phone]["equip"]["rent_per_day"] = raw_body
        sessions[phone]["step"] = "equip_available_until"
        return (
            "Available until which date?\n"
            "(e.g. 30 June 2026, Tomorrow, or reply 'ongoing' if no end date)"
        )

    elif step == "equip_available_until":
        farmer = get_from_db("farmers", phone)
        equip  = sessions[phone]["equip"]
        available_until = None
        if raw_body.strip().lower() not in ("ongoing", "anytime", "-"):
            is_valid, normalized_date, err = validate_future_date(raw_body)
            if not is_valid:
                return err
            available_until = normalized_date
        saved = save_equipment({
            "owner_phone":     phone,
            "name":            equip["name"],
            "rent_per_day":    equip["rent_per_day"],
            "location":        farmer.get("location", "Unknown"),
            "available_until": available_until,
            "available":       True,
        })
        sessions[phone]["step"] = "done"
        if not saved:
            return "⚠️ Error listing your equipment. Please try again by sending LIST EQUIPMENT."
        return (
            f"✅ Equipment Listed!\n\n"
            f"🚜 Equipment: {equip['name']}\n"
            f"💰 Rent: ₹{equip['rent_per_day']}/day\n"
            f"📍 Location: {farmer.get('location', 'Unknown')}\n"
            f"📅 Available until: {available_until or 'Ongoing'}\n\n"
            f"Farmers and labourers nearby can now find your equipment!"
        )

    # ── FALLBACK ──────────────────────────────────────────────────────────────
    else:
        print(f"[FLOW] Unknown step '{step}' — resetting")
        sessions[phone] = {"step": "start"}
        return (
            "Something went wrong. Let's start over.\n\n"
            "Are you a FARMER or LABOURER?\n"
            "Reply FARMER or LABOURER to get started."
        )

# ── Routes ────────────────────────────────────────────────────────────────────

@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "ok"}

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"message": "Farm Connect API is running"}

# ── Twilio webhook ─────────────────────────────────────────────────────────────
@app.post("/webhook")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    try:
        reply = handle_message(From, Body.strip())
        return twiml_response(reply)
    except Exception:
        print(f"[WEBHOOK] UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
        err = MessagingResponse()
        err.message("⚠️ Something went wrong. Please try again in a moment.")
        return Response(content=str(err), media_type="application/xml")

# ── Web chat API ───────────────────────────────────────────────────────────────
@app.post("/chat")
async def chat_api(request: Request):
    try:
        data    = await request.json()
        phone   = data.get("phone", "web_test").strip()
        message = data.get("message", "").strip()
        if not message:
            return JSONResponse({"reply": "Please enter a message."})
        reply = handle_message(phone, message)
        return JSONResponse({"reply": reply})
    except Exception:
        print(f"[CHAT API] EXCEPTION:\n{traceback.format_exc()}")
        return JSONResponse({"reply": "⚠️ Something went wrong. Please try again."})


<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Farm Connect — Web Test</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0b141a; color: #e9edef; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  .header { background: #202c33; padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #313d45; flex-shrink: 0; }
  .header-left { display: flex; align-items: center; gap: 10px; }
  .avatar { width: 40px; height: 40px; border-radius: 50%; background: #00a884; display: flex; align-items: center; justify-content: center; font-size: 20px; }
  .header h1 { font-size: 16px; color: #e9edef; }
  .header span { color: #8696a0; font-size: 12px; }
  .badge { background: #00a884; color: white; font-size: 11px; padding: 2px 8px; border-radius: 10px; }

  .phone-bar { background: #111b21; padding: 8px 16px; border-bottom: 1px solid #313d45; display: flex; align-items: center; gap: 8px; flex-shrink: 0; flex-wrap: wrap; }
  .phone-bar label { color: #8696a0; font-size: 12px; white-space: nowrap; }
  .phone-bar select, .phone-bar input[type=text] { background: #2a3942; border: 1px solid #3b4a54; color: #e9edef; padding: 5px 10px; border-radius: 6px; font-size: 12px; outline: none; }
  .phone-bar select { flex: 1; min-width: 160px; }
  .phone-bar input[type=text] { flex: 2; min-width: 160px; }
  .btn { border: none; color: white; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; white-space: nowrap; }
  .btn-reset { background: #ea4335; }
  .btn-reset:hover { background: #c5221f; }

  #chat { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; background: #0b141a; }
  #chat::-webkit-scrollbar { width: 5px; }
  #chat::-webkit-scrollbar-thumb { background: #374248; border-radius: 4px; }

  .bubble-wrap { display: flex; flex-direction: column; }
  .bubble-wrap.sent  { align-items: flex-end; }
  .bubble-wrap.recv  { align-items: flex-start; }

  .bubble { max-width: 72%; padding: 7px 12px 4px; border-radius: 8px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; position: relative; }
  .sent .bubble  { background: #005c4b; border-bottom-right-radius: 2px; }
  .recv .bubble  { background: #202c33; border-bottom-left-radius: 2px; }
  .bubble .time  { font-size: 10px; color: #8696a0; margin-top: 2px; text-align: right; }

  .typing { color: #8696a0; font-size: 13px; font-style: italic; padding: 4px 0 4px 4px; }

  .input-bar { background: #202c33; padding: 10px 14px; display: flex; gap: 10px; align-items: flex-end; border-top: 1px solid #313d45; flex-shrink: 0; }
  #msg { flex: 1; background: #2a3942; border: none; color: #e9edef; padding: 10px 14px; border-radius: 24px; font-size: 14px; outline: none; resize: none; max-height: 120px; overflow-y: auto; line-height: 1.4; }
  #msg::placeholder { color: #8696a0; }
  #send { background: #00a884; border: none; color: white; width: 44px; height: 44px; border-radius: 50%; cursor: pointer; font-size: 18px; flex-shrink: 0; transition: background 0.15s; }
  #send:hover { background: #06cf9c; }
  #send:disabled { background: #3b4a54; cursor: default; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="avatar">🌾</div>
    <div>
      <h1>Farm Connect Bot</h1>
      <span>Web Test Interface</span>
    </div>
  </div>
  <span class="badge">TEST MODE</span>
</div>

<div class="phone-bar">
  <label>Testing as:</label>
  <select id="phonePreset" onchange="updatePhone()">
    <option value="whatsapp:+918754176823">Bharani T (+91 8754176823)</option>
    <option value="whatsapp:+919942149060">User 2 (+91 9942149060)</option>
    <option value="web_farmer_test">New Farmer (fresh)</option>
    <option value="web_labourer_test">New Labourer (fresh)</option>
    <option value="custom">Custom phone...</option>
  </select>
  <input type="text" id="customPhone" placeholder="e.g. whatsapp:+91XXXXXXXXXX" style="display:none">
  <button class="btn btn-reset" onclick="resetSession()">🔄 Reset Session</button>
</div>

<div id="chat">
  <div class="bubble-wrap recv">
    <div class="bubble">👋 Welcome to Farm Connect Web Tester!
Type any message below and press Enter.
<div class="time">now</div></div>
  </div>
</div>

<div class="input-bar">
  <textarea id="msg" rows="1" placeholder="Type a message..."></textarea>
  <button id="send" onclick="sendMessage()">➤</button>
</div>

<script>
  function getPhone() {
    const preset = document.getElementById('phonePreset').value;
    if (preset === 'custom') return document.getElementById('customPhone').value.trim() || 'custom_test';
    return preset;
  }

  function updatePhone() {
    const custom = document.getElementById('customPhone');
    custom.style.display = document.getElementById('phonePreset').value === 'custom' ? 'inline' : 'none';
  }

  function nowTime() {
    return new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
  }

  function addBubble(text, type) {
    const chat = document.getElementById('chat');
    const wrap = document.createElement('div');
    wrap.className = `bubble-wrap ${type}`;
    wrap.innerHTML = `<div class="bubble">${escHtml(text)}<div class="time">${nowTime()}</div></div>`;
    chat.appendChild(wrap);
    chat.scrollTop = chat.scrollHeight;
  }

  function escHtml(str) {
    return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function showTyping() {
    const chat = document.getElementById('chat');
    const el = document.createElement('div');
    el.id = 'typing';
    el.className = 'typing';
    el.textContent = 'Bot is typing...';
    chat.appendChild(el);
    chat.scrollTop = chat.scrollHeight;
  }

  function hideTyping() {
    const el = document.getElementById('typing');
    if (el) el.remove();
  }

  async function sendMessage() {
    const input  = document.getElementById('msg');
    const btn    = document.getElementById('send');
    const text   = input.value.trim();
    if (!text) return;

    const phone = getPhone();
    input.value = '';
    input.style.height = 'auto';
    addBubble(text, 'sent');
    btn.disabled = true;
    showTyping();

    try {
      const res  = await fetch('/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({phone, message: text})
      });
      const data = await res.json();
      hideTyping();
      addBubble(data.reply, 'recv');
    } catch(e) {
      hideTyping();
      addBubble('⚠️ Could not reach bot. Is Render running?', 'recv');
    } finally {
      btn.disabled = false;
      input.focus();
    }
  }

  async function resetSession() {
    const phone = getPhone();
    try {
      await fetch('/reset', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({phone})
      });
    } catch(e) {}
    document.getElementById('chat').innerHTML = '';
    addBubble(`Session reset for ${phone}.\nSay Hi to start fresh! 👋`, 'recv');
  }

  const msgEl = document.getElementById('msg');
  msgEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  msgEl.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
</script>
</body>
</html>"""
