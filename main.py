import os
import re
import traceback
import threading
import time
from datetime import datetime, date
import requests as req
from fastapi import FastAPI, Form, Response
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
            req.get("https://farm-connect-yjg8.onrender.com/", timeout=10)
            print("[KEEP-ALIVE] Pinged successfully")
        except Exception as e:
            print(f"[KEEP-ALIVE] Ping failed: {e}")

threading.Thread(target=keep_alive, daemon=True).start()

# ── Env vars ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
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
    """Standard Levenshtein distance."""
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        ndp = [i + 1]
        for j, cb in enumerate(b):
            ndp.append(min(dp[j] + (ca != cb), dp[j + 1] + 1, ndp[-1] + 1))
        dp = ndp
    return dp[-1]

# Known commands, ordered longest-first so multi-word ones match before prefixes
KNOWN_COMMANDS = [
    "POST JOB", "MY JOBS", "MY LABOURERS", "VIEW JOBS",
    "CONFIRM", "CANCEL", "RATE",
]

def fuzzy_suggestion(message, threshold=2):
    """
    Return a (suggestion, hint) tuple when the message looks like a
    mis-typed command, else (None, None).

    Strategy:
      1. Prefix guard  — message starts with a known command root but has
                         trailing garbage (e.g. "CANCELL 5", "RETE 3 4").
      2. Edit-distance — whole message is within `threshold` edits of a
                         known command (catches "CANCLE", "VEIW JOBS").
    """
    HINTS = {
        "RATE":         "Format: RATE [job_id] [stars 1–5]  •  Example: RATE 12 5",
        "CANCEL":       "Format: CANCEL [job_id]  •  Example: CANCEL 7",
        "CONFIRM":      "Format: CONFIRM [job_id]  •  Example: CONFIRM 3",
        "POST JOB":     "Just send: POST JOB",
        "MY JOBS":      "Just send: MY JOBS",
        "MY LABOURERS": "Just send: MY LABOURERS",
        "VIEW JOBS":    "Just send: VIEW JOBS",
    }

    # 1. Prefix guard: "CANCELL 5" starts with "CANCEL" but has extras
    for cmd in KNOWN_COMMANDS:
        if message.startswith(cmd) and message != cmd:
            # Only flag if the character right after the command isn't a space
            # followed by a valid digit argument (those are handled upstream).
            remainder = message[len(cmd):]
            if remainder and not remainder.startswith(" "):
                return cmd, HINTS[cmd]

    # 2. Edit-distance on the first token or the whole short message
    first_token = message.split()[0] if message.split() else message
    for cmd in KNOWN_COMMANDS:
        # Compare against just the first word of multi-word commands too
        cmd_first = cmd.split()[0]
        if _edit_distance(first_token, cmd_first) <= threshold:
            # Avoid false positives on very short tokens like "MY"
            if len(first_token) >= 3:
                return cmd, HINTS[cmd]

    # 3. Whole-message edit distance for short commands like "MY JOBS"
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

# Explicit datetime formats we try first (day-month-year style, since this
# is an Indian-farmer-facing app — avoids US-style month/day ambiguity).
DATE_FORMATS = [
    "%d %B %Y", "%d %b %Y", "%d %B", "%d %b",
    "%d-%m-%Y", "%d/%m/%Y", "%d-%m", "%d/%m",
    "%B %d %Y", "%b %d %Y", "%B %d", "%b %d",
    "%Y-%m-%d",
]


def parse_relative_date(text):
    """Handle TODAY / TOMORROW / NEXT WEEK style phrases. Returns a date or None."""
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
    """
    Try to parse a farmer-entered date string into a date object.

    Returns (parsed_date, error_message). If parsing fails entirely,
    parsed_date is None and error_message explains why. If parsing
    succeeds but the date is in the past, parsed_date is None and
    error_message explains the past-date problem.
    """
    text = raw_text.strip()
    today = date.today()

    # 1. Relative phrases
    rel = parse_relative_date(text)
    if rel:
        return rel, None

    # 2. Try explicit formats, filling in missing year with the current
    #    (or next, if the resulting date would be in the past) year.
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text.title() if "%B" in fmt or "%b" in fmt else text, fmt)
        except ValueError:
            continue

        if "%Y" not in fmt:
            # No year given — assume current year, roll to next year if that's already past
            candidate = parsed.replace(year=today.year).date()
            if candidate < today:
                candidate = candidate.replace(year=today.year + 1)
            return candidate, None
        else:
            return parsed.date(), None

    # 3. Loose regex fallback: "20 june 2026", "20th june", "june 20"
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.IGNORECASE)
    m = re.match(
        r"^(\d{1,2})\s+([a-zA-Z]+)(?:\s+(\d{4}))?$", cleaned.strip()
    )
    if not m:
        m = re.match(
            r"^([a-zA-Z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?$", cleaned.strip()
        )
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
                return None, (
                    "❓ That doesn't look like a valid date. "
                    "Please use a format like '20 June 2026' or 'Tomorrow'."
                )
            if not year_str and candidate < today:
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate, None

    return None, (
        "❓ Couldn't understand that date.\n\n"
        "Please reply with a date like '20 June 2026', '20/06/2026', or 'Tomorrow'."
    )


def validate_future_date(raw_text):
    """
    Validate that raw_text represents a real calendar date that is today
    or later. Returns (is_valid, normalized_str_or_None, error_message_or_None).
    """
    parsed, err = parse_job_date(raw_text)
    if err:
        return False, None, err
    today = date.today()
    if parsed < today:
        return False, None, (
            f"❌ That date ({parsed.strftime('%d %B %Y')}) is in the past.\n\n"
            f"Please enter a future date (today or later), e.g. "
            f"'{(today.strftime('%d %B %Y'))}' or 'Tomorrow'."
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
        url = (
            f"{SUPABASE_URL}/rest/v1/jobs"
            f"?farmer_phone=eq.{encoded_phone}"
            f"&order=created_at.desc&limit=5"
        )
        print(f"[DB] GET jobs: {url}")
        res = req.get(url, headers=HEADERS, timeout=10)
        print(f"[DB] GET status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_jobs_by_phone ERROR: {e}\n{traceback.format_exc()}")
        return []


def get_open_jobs_by_location(location):
    try:
        encoded_location = quote(f"%{location}%", safe="")
        url = (
            f"{SUPABASE_URL}/rest/v1/jobs"
            f"?location=ilike.{encoded_location}"
            f"&status=eq.open&limit=5"
        )
        print(f"[DB] GET open jobs: {url}")
        res = req.get(url, headers=HEADERS, timeout=10)
        print(f"[DB] GET status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_open_jobs_by_location ERROR: {e}\n{traceback.format_exc()}")
        return []


def get_labourers_by_location(location):
    try:
        encoded_location = quote(f"%{location}%", safe="")
        url = f"{SUPABASE_URL}/rest/v1/labourers?location=ilike.{encoded_location}"
        print(f"[DB] GET labourers: {url}")
        res = req.get(url, headers=HEADERS, timeout=10)
        print(f"[DB] GET status={res.status_code} | body={res.text[:300]}")
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_labourers_by_location ERROR: {e}\n{traceback.format_exc()}")
        return []


def get_confirmed_jobs_for_farmer(phone):
    try:
        encoded_phone = quote(phone, safe="")
        url = (
            f"{SUPABASE_URL}/rest/v1/jobs"
            f"?farmer_phone=eq.{encoded_phone}"
            f"&status=eq.confirmed"
            f"&order=start_date.desc&limit=10"
        )
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_confirmed_jobs_for_farmer ERROR: {e}")
        return []


# ── Twilio helpers ────────────────────────────────────────────────────────────

def send_whatsapp(to, message):
    try:
        print(f"[TWILIO] Sending to {to}: {message[:80]}")
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_FROM, to=to)
        print(f"[TWILIO] Sent OK — SID={msg.sid}")
    except Exception as e:
        print(f"[TWILIO] ERROR sending to {to}: {e}\n{traceback.format_exc()}")


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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
def root():
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

        print(f"\n{'='*60}")
        print(f"[WEBHOOK] FROM={phone} | RAW='{raw_body}' | UPPER='{message}'")

        if phone not in sessions:
            sessions[phone] = {"step": "start"}

        step = sessions[phone].get("step", "start")
        print(f"[SESSION] step='{step}' | session={sessions[phone]}")

        # ── START ─────────────────────────────────────────────────────────────

        if step == "start":
            farmer = get_from_db("farmers", phone)
            if farmer:
                sessions[phone] = {"step": "done", "role": "farmer"}
                return twiml_response(
                    f"Welcome back {farmer['name']}! 🌾\n\n"
                    f"Reply:\n"
                    f"POST JOB — Post a new job\n"
                    f"MY JOBS — View your posted jobs\n"
                    f"MY LABOURERS — See confirmed jobs & rate labourers"
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

        # ── REGISTRATION ──────────────────────────────────────────────────────

        elif step == "role":
            if message in ("FARMER", "LABOURER"):
                sessions[phone]["role"] = message.lower()
                sessions[phone]["step"] = "name"
                return twiml_response("Great! What is your name?")
            return twiml_response("Please reply with FARMER or LABOURER only.")

        elif step == "name":
            sessions[phone]["name"] = raw_body
            sessions[phone]["step"] = "location"
            return twiml_response(
                f"Nice to meet you {raw_body}! What is your village or town name?"
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
                    return twiml_response("⚠️ Error saving your details. Please try again.")
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
                return twiml_response("⚠️ Error saving your details. Please try again.")
            sessions[phone]["step"] = "done"
            sessions[phone]["role"] = "labourer"
            return twiml_response(
                f"✅ Registered as Labourer!\n\n"
                f"Name: {sessions[phone]['name']}\n"
                f"Location: {sessions[phone]['location']}\n"
                f"Skill: {skill}\n\n"
                f"Reply VIEW JOBS to see available jobs near you."
            )

        # ── MAIN MENU ─────────────────────────────────────────────────────────

        elif step == "done":
            print(f"[FLOW] DONE menu — message='{message}'")

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

            elif message == "MY LABOURERS":
                farmer = get_from_db("farmers", phone)
                if not farmer:
                    return twiml_response("❌ Only farmers can use this command.")
                jobs = get_confirmed_jobs_for_farmer(phone)
                if not jobs:
                    return twiml_response("No confirmed jobs found.\nReply POST JOB to post one.")
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
                return twiml_response(msg)

            elif message.startswith("RATE"):
                parts = raw_body.split()
                if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                    return twiml_response(
                        "❓ Couldn't read that.\n\n"
                        "Format: RATE [job_id] [stars 1–5]\n"
                        "Example: RATE 12 5"
                    )
                job_id, stars = parts[1], int(parts[2])
                if stars < 1 or stars > 5:
                    return twiml_response("Stars must be between 1 and 5.")
                farmer = get_from_db("farmers", phone)
                if not farmer:
                    return twiml_response("❌ Only farmers can rate labourers.")
                try:
                    url = f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}&farmer_phone=eq.{quote(phone, safe='')}"
                    res = req.get(url, headers=HEADERS, timeout=10)
                    jobs = res.json()
                except Exception:
                    return twiml_response("❌ Could not fetch job. Try again.")
                if not jobs:
                    return twiml_response("❌ Job not found or doesn't belong to you.")
                job = jobs[0]
                if job.get("rated"):
                    return twiml_response("You've already rated this job.")
                if job["status"] != "confirmed":
                    return twiml_response("❌ Can only rate confirmed jobs.")
                labourer_phone = job.get("labourer_phone")
                if not labourer_phone:
                    return twiml_response("❌ No labourer assigned to this job.")
                labourer = get_from_db("labourers", labourer_phone)
                if not labourer:
                    return twiml_response("❌ Labourer not found.")
                old_total = labourer.get("total_ratings", 0)
                old_rating = labourer.get("rating", 0)
                new_total = old_total + 1
                new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
                update_db("labourers", {"phone": labourer_phone}, {
                    "rating": new_rating,
                    "total_ratings": new_total
                })
                update_db("jobs", {"id": job_id}, {"rated": True})
                return twiml_response(
                    f"✅ Rated {labourer['name']} — {stars} stars!\n"
                    f"Their new rating: {new_rating}⭐ ({new_total} ratings)"
                )

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

            elif message == "VIEW JOBS":
                labourer = get_from_db("labourers", phone)
                if not labourer:
                    return twiml_response("❌ Only registered labourers can view jobs.")
                jobs = get_open_jobs_by_location(labourer["location"])
                if not jobs:
                    return twiml_response(
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
                return twiml_response(msg)

            elif message.startswith("CONFIRM"):
                parts = raw_body.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    return twiml_response(
                        "❓ Couldn't read that.\n\n"
                        "Format: CONFIRM [job_id]\n"
                        "Example: CONFIRM 3"
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

            elif message.startswith("CANCEL"):
                parts = raw_body.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    return twiml_response(
                        "❓ Couldn't read that.\n\n"
                        "Format: CANCEL [job_id]\n"
                        "Example: CANCEL 7"
                    )
                job_id = parts[1]
                updated = update_db(
                    "jobs",
                    {"id": job_id, "farmer_phone": phone},
                    {"status": "cancelled"}
                )
                if not updated:
                    return twiml_response("❌ Job not found or you don't own this job.")
                job = updated[0]
                labourer_phone = job.get("labourer_phone")
                if labourer_phone:
                    send_whatsapp(
                        labourer_phone,
                        f"⚠️ Job Cancelled\n\n"
                        f"Work: {job['work_type']}\n"
                        f"Location: {job['location']}\n"
                        f"Date: {job['start_date']}\n\n"
                        f"This job has been cancelled by the farmer. Sorry for the inconvenience."
                    )
                    print(f"[CANCEL] Notified labourer {labourer_phone} of cancellation for job {job_id}")
                else:
                    print(f"[CANCEL] No labourer assigned to job {job_id} — no notification sent")
                return twiml_response(f"✅ Job #{job_id} has been cancelled.")

            # ── Unknown / near-miss command ───────────────────────────────────
            else:
                suggestion, hint = fuzzy_suggestion(message)
                if suggestion:
                    return twiml_response(
                        f"❓ Unknown command. Did you mean *{suggestion}*?\n\n"
                        f"{hint}\n\n"
                        f"Send it exactly as shown to continue."
                    )

                # Truly unrecognised — show role-appropriate menu
                farmer = get_from_db("farmers", phone)
                if farmer:
                    return twiml_response(
                        f"❌ Unknown command.\n\n"
                        f"Hello {farmer['name']}! 🌾 Available commands:\n"
                        f"POST JOB — Post a new job\n"
                        f"MY JOBS — View your posted jobs\n"
                        f"MY LABOURERS — See confirmed jobs & rate labourers"
                    )
                labourer = get_from_db("labourers", phone)
                if labourer:
                    return twiml_response(
                        f"❌ Unknown command.\n\n"
                        f"Hello {labourer['name']}! 👋 Available commands:\n"
                        f"VIEW JOBS — See available jobs near you"
                    )
                sessions[phone] = {"step": "start"}
                return twiml_response(
                    "🌾 Welcome to Farm Connect!\n\n"
                    "Are you a FARMER or LABOURER?\n"
                    "Reply FARMER or LABOURER to get started."
                )

        # ── JOB POSTING FLOW ──────────────────────────────────────────────────

        elif step == "job_work_type":
            sessions[phone]["job"]["work_type"] = raw_body
            sessions[phone]["step"] = "job_num_labourers"
            return twiml_response("How many labourers do you need?")

        elif step == "job_num_labourers":
            if not raw_body.isdigit():
                return twiml_response("Please enter a number. How many labourers do you need?")
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
            return twiml_response("When do you need them? (e.g. 20 June 2026, Tomorrow)")

        elif step == "job_date":
            is_valid, normalized_date, err = validate_future_date(raw_body)
            if not is_valid:
                return twiml_response(err)

            job = sessions[phone]["job"]
            job["start_date"] = normalized_date
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
                    "⚠️ Error posting your job. Please try again by sending POST JOB."
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

        # ── FALLBACK ──────────────────────────────────────────────────────────

        else:
            print(f"[FLOW] Unknown step '{step}' — resetting")
            sessions[phone] = {"step": "start"}
            return twiml_response(
                "Something went wrong. Let's start over.\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )

    except Exception:
        print(f"[WEBHOOK] UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
        err = MessagingResponse()
        err.message("⚠️ Something went wrong. Please try again in a moment.")
        return Response(content=str(err), media_type="application/xml")
