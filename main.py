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

# ── Greeting keywords ─────────────────────────────────────────────────────────
GREETINGS = {
    "HI", "HELLO", "HEY", "HELP", "START", "MENU",
    "VANAKKAM", "NAMASTE", "HAI", "HELO", "VANAKAM",
    "GOOD MORNING", "GOOD AFTERNOON", "GOOD EVENING",
    "GM", "SUP", "YO", "HOWDY"
}

# ── Skill map (shared by signup + UPDATE SKILL) ───────────────────────────────
SKILL_MAP = {
    "1": "Harvesting", "HARVESTING": "Harvesting",
    "2": "Planting", "PLANTING": "Planting",
    "3": "Irrigation", "IRRIGATION": "Irrigation",
    "4": "Weeding", "WEEDING": "Weeding",
    "5": "General Labour", "GENERAL LABOUR": "General Labour",
    "GENERAL": "General Labour",
    "6": "Any Work (No Preference)", "ANY WORK": "Any Work (No Preference)",
    "ANY WORK (NO PREFERENCE)": "Any Work (No Preference)",
    "ANY": "Any Work (No Preference)", "FLEXIBLE": "Any Work (No Preference)",
    "NO PREFERENCE": "Any Work (No Preference)",
}

SKILL_PROMPT = (
    "What is your main skill?\n\n"
    "1️⃣  Harvesting\n"
    "2️⃣  Planting\n"
    "3️⃣  Irrigation\n"
    "4️⃣  Weeding\n"
    "5️⃣  General Labour\n"
    "6️⃣  Any Work (No Preference)\n\n"
    "Reply with the number or skill name."
)

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
    "POST JOB", "MY JOBS", "MY LABOURERS", "MY FARMERS", "VIEW JOBS",
    "CONFIRM", "CANCEL", "RATE", "JOB DONE", "UPDATE SKILL",
    "RENT EQUIPMENT", "VIEW EQUIPMENT", "MY EQUIPMENT", "BOOK EQUIPMENT", "CANCEL EQUIPMENT",
    "SUBSIDIES", "SUBSIDY", "MY PROFILE",
]

def fuzzy_suggestion(message, threshold=2):
    HINTS = {
        "RATE":             "Format: RATE [job_id] [stars 1–5]  •  Example: RATE 12 5",
        "CANCEL":           "Format: CANCEL [job_id]  •  Example: CANCEL 7",
        "CONFIRM":          "Format: CONFIRM [job_id]  •  Example: CONFIRM 3",
        "JOB DONE":         "Format: JOB DONE [job_id]  •  Example: JOB DONE 12",
        "UPDATE SKILL":     "Just send: UPDATE SKILL",
        "POST JOB":         "Just send: POST JOB",
        "MY JOBS":          "Just send: MY JOBS",
        "MY LABOURERS":     "Just send: MY LABOURERS",
        "MY FARMERS":       "Just send: MY FARMERS",
        "VIEW JOBS":        "Just send: VIEW JOBS",
        "RENT EQUIPMENT":   "Just send: RENT EQUIPMENT",
        "VIEW EQUIPMENT":   "Just send: VIEW EQUIPMENT",
        "MY EQUIPMENT":     "Just send: MY EQUIPMENT",
        "BOOK EQUIPMENT":   "Format: BOOK EQUIPMENT [id]  •  Example: BOOK EQUIPMENT 3",
        "CANCEL EQUIPMENT": "Format: CANCEL EQUIPMENT [id]  •  Example: CANCEL EQUIPMENT 3",
        "SUBSIDIES":        "Just send: SUBSIDIES",
        "SUBSIDY":          "Format: SUBSIDY [number]  •  Example: SUBSIDY 2",
        "MY PROFILE":       "Just send: MY PROFILE",
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
                return None, f"❓ That doesn't look like a valid date. Please use a format like '{example_future_date_str()}' or 'Tomorrow'."
            if not year_str and candidate < today:
                candidate = candidate.replace(year=candidate.year + 1)
            return candidate, None
    return None, f"❓ Couldn't understand that date.\n\nPlease reply with a date like '{example_future_date_str()}', '{example_future_date_str(fmt='%d/%m/%Y')}', or 'Tomorrow'."

def example_future_date_str(days_ahead: int = 5, fmt: str = "%d %B %Y") -> str:
    """Always returns a date a few days in the future from *today*, so prompts
    never show a stale hardcoded example date."""
    today = date.today()
    future = today.fromordinal(today.toordinal() + days_ahead)
    return future.strftime(fmt)

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

# ── Days-until helper for subsidy deadlines ───────────────────────────────────
def days_until(d: date) -> str:
    delta = (d - date.today()).days
    if delta < 0:
        return "expired"
    if delta == 0:
        return "⚠️ Last day today!"
    if delta <= 7:
        return f"⚠️ Only {delta} day{'s' if delta != 1 else ''} left!"
    if delta <= 30:
        return f"🔔 {delta} days left"
    return f"📅 Deadline: {d.strftime('%d %b %Y')}"

# ── Government subsidy schemes ────────────────────────────────────────────────
SUBSIDY_SCHEMES = [
    {
        "name": "PM-KISAN",
        "short": "₹6,000/year direct income support for farmers",
        "eligibility": "All landholding farmer families (subject to exclusion criteria like income tax payers, government employees in certain categories).",
        "benefit": "₹6,000 per year, paid in 3 installments of ₹2,000 directly to bank account.",
        "how_to_apply": "Apply online at pmkisan.gov.in or visit your nearest Common Service Centre (CSC) with Aadhaar, land records, and bank account details.",
        "link": "https://pmkisan.gov.in",
        "start_date": date(2019, 2, 24),
        "end_date": None,
        "renewal_note": "🔁 e-KYC must be renewed yearly to keep receiving installments.",
    },
    {
        "name": "PMFBY – Kharif 2025",
        "short": "Crop insurance for Kharif 2025 season",
        "eligibility": "All farmers growing notified crops in notified areas, including sharecroppers and tenant farmers.",
        "benefit": "Low premium (2% of sum insured) crop insurance covering losses from natural calamities, pests, and diseases.",
        "how_to_apply": "Apply through your bank, CSC, or pmfby.gov.in before 31 July 2025 (Kharif cutoff).",
        "link": "https://pmfby.gov.in",
        "start_date": date(2025, 4, 1),
        "end_date": date(2025, 7, 31),
        "renewal_note": None,
    },
    {
        "name": "PMFBY – Rabi 2025–26",
        "short": "Crop insurance for Rabi 2025–26 season",
        "eligibility": "All farmers growing notified Rabi crops.",
        "benefit": "Low premium (1.5% of sum insured) crop insurance covering losses from natural calamities.",
        "how_to_apply": "Apply through your bank, CSC, or pmfby.gov.in before 31 December 2025 (Rabi cutoff).",
        "link": "https://pmfby.gov.in",
        "start_date": date(2025, 10, 1),
        "end_date": date(2025, 12, 31),
        "renewal_note": None,
    },
    {
        "name": "KCC (Kisan Credit Card)",
        "short": "Easy credit access for farming needs at low interest",
        "eligibility": "Farmers, tenant farmers, sharecroppers, and self-help group members.",
        "benefit": "Short-term loans at subsidized interest rates (4–7%) for crop production, equipment, and allied activities.",
        "how_to_apply": "Apply at any nearby bank branch with land documents and identity proof.",
        "link": "https://www.myscheme.gov.in/schemes/kcc",
        "start_date": date(1998, 8, 1),
        "end_date": None,
        "renewal_note": "🔁 Credit limit is reviewed and renewed annually by your bank.",
    },
    {
        "name": "Soil Health Card Scheme",
        "short": "Free soil testing and crop-wise nutrient advice",
        "eligibility": "All farmers.",
        "benefit": "Free soil testing every 2 years with crop-wise fertilizer and nutrient recommendations to reduce input costs.",
        "how_to_apply": "Contact your local Krishi Vigyan Kendra (KVK) or agriculture department office to get your soil tested.",
        "link": "https://soilhealth.dac.gov.in",
        "start_date": date(2015, 2, 19),
        "end_date": None,
        "renewal_note": "🔁 Re-test and renew your card every 2 years.",
    },
    {
        "name": "MGNREGA",
        "short": "100 days guaranteed rural employment",
        "eligibility": "Any rural household willing to do unskilled manual work (relevant for labourers).",
        "benefit": "Guaranteed 100 days of wage employment per year at the notified minimum wage.",
        "how_to_apply": "Register at your local Gram Panchayat to get a Job Card, then apply for work as needed.",
        "link": "https://nrega.nic.in",
        "start_date": date(2006, 2, 2),
        "end_date": None,
        "renewal_note": "🔁 100-day work entitlement resets every financial year (April–March).",
    },
    {
        "name": "PM Krishi Sinchayee Yojana (PMKSY)",
        "short": "Subsidy on drip & sprinkler irrigation systems",
        "eligibility": "All farmers; SC/ST and small/marginal farmers get higher subsidy (55%).",
        "benefit": "55% subsidy for small/marginal farmers, 45% for others on drip and sprinkler irrigation systems.",
        "how_to_apply": "Apply through your State Agriculture Department or pmksy.gov.in with land and Aadhaar details.",
        "link": "https://pmksy.gov.in",
        "start_date": date(2015, 7, 1),
        "end_date": None,
        "renewal_note": None,
    },
    {
        "name": "National Food Security Mission (NFSM)",
        "short": "Free certified seeds & input support for key crops",
        "eligibility": "Farmers in notified districts growing rice, wheat, pulses, or coarse cereals.",
        "benefit": "Free/subsidised certified seeds, farm machinery, training, and demonstrations.",
        "how_to_apply": "Contact your Block Agriculture Officer or local Krishi Vigyan Kendra (KVK).",
        "link": "https://nfsm.gov.in",
        "start_date": date(2007, 10, 1),
        "end_date": None,
        "renewal_note": "🔁 Input support is allocated freshly each crop season — re-check with your KVK.",
    },
    {
        "name": "Tamil Nadu CM's Drought Relief – 2025",
        "short": "One-time ₹2,000/acre relief for drought-affected TN farmers",
        "eligibility": "Farmers in Tamil Nadu districts declared drought-affected for 2024–25 season.",
        "benefit": "₹2,000 per acre (up to 5 acres) direct bank transfer to eligible farmers.",
        "how_to_apply": "Apply at your Village Administrative Office (VAO) with patta/chitta and bank passbook before 30 September 2025.",
        "link": "https://www.tn.gov.in",
        "start_date": date(2025, 3, 1),
        "end_date": date(2025, 9, 30),
        "renewal_note": None,
    },
    {
        "name": "e-NAM (National Agriculture Market)",
        "short": "Sell crops online directly to buyers across India",
        "eligibility": "All farmers with produce registered at a linked APMC mandi.",
        "benefit": "Access to buyers across India, transparent online bidding, and direct bank payment — better prices, no middlemen.",
        "how_to_apply": "Register at enam.gov.in or through your local APMC/mandi office with Aadhaar and bank details.",
        "link": "https://enam.gov.in",
        "start_date": date(2016, 4, 14),
        "end_date": None,
        "renewal_note": None,
    },
    {
        "name": "Agri Infrastructure Fund (AIF)",
        "short": "Low-interest loans for farm storage & processing",
        "eligibility": "Farmers, FPOs, PACS, SHGs, agri-entrepreneurs for post-harvest infrastructure.",
        "benefit": "Loans up to ₹2 crore at 3% interest subsidy for warehouses, cold storage, processing units.",
        "how_to_apply": "Apply through any scheduled bank or at agriinfra.dac.gov.in with project report and land documents.",
        "link": "https://agriinfra.dac.gov.in",
        "start_date": date(2020, 8, 9),
        "end_date": None,
        "renewal_note": None,
    },
    {
        "name": "PM Fasal Bima (PMFBY) – Horticulture TN",
        "short": "Crop insurance for banana, tomato, onion (TN)",
        "eligibility": "Tamil Nadu farmers growing banana, tomato, onion, or other notified horticultural crops.",
        "benefit": "5% premium cap, covers crop loss from drought, flood, pests, and unseasonal rain.",
        "how_to_apply": "Apply at nearest Tamil Nadu Horticulture Department office or through your cooperative bank before season cutoff.",
        "link": "https://pmfby.gov.in",
        "start_date": date(2025, 6, 1),
        "end_date": date(2025, 8, 31),
        "renewal_note": None,
    },
]

def active_schemes(today: date = None) -> list:
    today = today or date.today()
    return [
        s for s in SUBSIDY_SCHEMES
        if s["start_date"] <= today
        and (s["end_date"] is None or s["end_date"] >= today)
    ]

def expired_schemes(today: date = None) -> list:
    today = today or date.today()
    return [
        s for s in SUBSIDY_SCHEMES
        if s["end_date"] is not None and s["end_date"] < today
    ]

def expiry_tag(scheme: dict) -> str:
    if scheme["end_date"] is None:
        return "🟢 Ongoing"
    return days_until(scheme["end_date"])

def next_cycle_estimate(scheme: dict) -> str:
    est_start = scheme["start_date"].replace(year=scheme["start_date"].year + 1)
    est_end   = scheme["end_date"].replace(year=scheme["end_date"].year + 1)
    return (
        f"📆 Likely reopens around {est_start.strftime('%b %Y')} "
        f"(estimate based on last year's cycle — confirm exact dates on the official portal)."
    )

def renewal_or_deadline_line(scheme: dict) -> str:
    if scheme["end_date"] is None:
        if scheme.get("renewal_note"):
            return scheme["renewal_note"]
        return "🟢 No fixed deadline — apply anytime."
    return f"📅 Deadline: {scheme['end_date'].strftime('%d %B %Y')}"

# ── Nearby-areas lookup (Option B: curated proximity map, ~20km clusters) ────
# Real radius/GPS matching needs lat/long + an external geocoding API (a
# planned Phase 2 upgrade). For now, each key maps to towns/villages within
# roughly 20km, so a job posted in one town also reaches labourers/farmers
# registered in its nearby cluster — not just an exact text match.
#
# Seeded with verified data for Tiruchengode taluk / Namakkal district
# (the primary test region). Add more clusters here as the user base grows
# into other districts — each entry should list places within ~20km of the key.
NEARBY_AREAS = {
    "TIRUCHENGODE": [
        "Tiruchengode", "Elacipalayam", "Sankari", "Mallasamudram",
        "Pallipalayam", "Komarapalayam", "Sankaridurg", "Erode",
        "Karumanur", "Mallasamudram West", "Vennandur",
    ],
    "SANKARI": [
        "Sankari", "Tiruchengode", "Mallasamudram", "Erode", "Komarapalayam",
    ],
    "ELACIPALAYAM": [
        "Elacipalayam", "Tiruchengode", "Sankari",
    ],
    "MALLASAMUDRAM": [
        "Mallasamudram", "Mallasamudram West", "Tiruchengode", "Sankari", "Karumanur",
    ],
    "KOMARAPALAYAM": [
        "Komarapalayam", "Pallipalayam", "Tiruchengode", "Sankari",
    ],
    "PALLIPALAYAM": [
        "Pallipalayam", "Komarapalayam", "Tiruchengode",
    ],
    "ERODE": [
        "Erode", "Tiruchengode", "Sankari", "Perundurai",
    ],
    "NAMAKKAL": [
        "Namakkal", "Tiruchengode", "Rasipuram", "Paramathi Velur",
    ],
    "RASIPURAM": [
        "Rasipuram", "Namakkal", "Tiruchengode",
    ],
}

def expand_nearby_locations(location: str) -> list:
    """Given a town/village name, return the list of place names considered
    'nearby' (~20km cluster). Always includes the original location itself.
    Falls back to just the original location if it's not in the curated map,
    so unmapped areas still work via the old exact-substring behaviour."""
    key = (location or "").strip().upper()
    if key in NEARBY_AREAS:
        return NEARBY_AREAS[key]
    # Reverse lookup: maybe they registered with a village that's *listed*
    # inside another cluster's nearby list, even if it's not a top-level key.
    for cluster_key, places in NEARBY_AREAS.items():
        if key in [p.upper() for p in places]:
            return places
    return [location] if location else []

def build_location_or_filter(location: str) -> str:
    """Builds a PostgREST 'or=()' filter string matching any place name in
    the nearby cluster for `location`, using ilike on each."""
    places = expand_nearby_locations(location)
    conditions = ",".join(f"location.ilike.{quote(f'%{p}%', safe='')}" for p in places)
    return f"or=({conditions})"

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
        or_filter = build_location_or_filter(location)
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?{or_filter}&status=eq.open&limit=5")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_open_jobs_by_location ERROR: {e}")
        return []

def get_labourers_by_location(location):
    try:
        or_filter = build_location_or_filter(location)
        url = f"{SUPABASE_URL}/rest/v1/labourers?{or_filter}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_labourers_by_location ERROR: {e}")
        return []

def get_confirmed_jobs_for_farmer(phone):
    """Jobs accepted by a labourer but not yet marked complete (farmer's pending-action list)."""
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

def get_confirmed_jobs_for_labourer(phone):
    """Jobs a labourer has accepted but not yet marked complete by the farmer."""
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?labourer_phone=eq.{encoded_phone}&status=eq.confirmed"
               f"&order=start_date.desc&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_confirmed_jobs_for_labourer ERROR: {e}")
        return []

def get_completed_jobs_for_farmer(phone):
    """Jobs marked completed — these are the ones eligible for rating."""
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?farmer_phone=eq.{encoded_phone}&status=eq.completed"
               f"&order=start_date.desc&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_completed_jobs_for_farmer ERROR: {e}")
        return []

def get_completed_jobs_for_labourer(phone):
    """Jobs marked completed — these are the ones eligible for rating."""
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?labourer_phone=eq.{encoded_phone}&status=eq.completed"
               f"&order=start_date.desc&limit=10")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_completed_jobs_for_labourer ERROR: {e}")
        return []

def count_jobs_posted_by_farmer(phone):
    """Total number of jobs ever posted by a farmer (any status)."""
    try:
        encoded_phone = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{encoded_phone}&select=id"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[DB] count_jobs_posted_by_farmer ERROR: {e}")
        return 0

def count_jobs_done_by_labourer(phone):
    """Total number of jobs actually completed (status=completed) by a labourer."""
    try:
        encoded_phone = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs"
               f"?labourer_phone=eq.{encoded_phone}&status=eq.completed&select=id")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[DB] count_jobs_done_by_labourer ERROR: {e}")
        return 0

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

def get_equipment_by_id(equipment_id):
    try:
        url = f"{SUPABASE_URL}/rest/v1/equipment?id=eq.{equipment_id}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        return data[0] if isinstance(data, list) and data else None
    except Exception as e:
        print(f"[DB] get_equipment_by_id ERROR: {e}")
        return None

def get_equipment_by_location(location):
    try:
        or_filter = build_location_or_filter(location)
        url = (f"{SUPABASE_URL}/rest/v1/equipment"
               f"?{or_filter}&available=eq.true&limit=10")
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

def notify_nearby_users_about_equipment(equipment):
    location = equipment["location"]
    farmers  = []
    try:
        or_filter = build_location_or_filter(location)
        url = f"{SUPABASE_URL}/rest/v1/farmers?{or_filter}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        farmers = res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[NOTIFY] get farmers ERROR: {e}")
    labourers = get_labourers_by_location(location)
    all_users = farmers + labourers
    print(f"[NOTIFY] Notifying {len(all_users)} user(s) about equipment in {location}")
    for user in all_users:
        if user.get("phone") != equipment.get("owner_phone"):
            send_whatsapp(
                user["phone"],
                f"🚜 Equipment Available for Rent Near You in {location}!\n\n"
                f"🔧 Equipment: {equipment['name']}\n"
                f"💰 Rent: ₹{equipment['rent_per_day']}/day\n"
                f"📅 Available until: {equipment.get('available_until') or 'Ongoing'}\n\n"
                f"Reply VIEW EQUIPMENT to see all listings."
            )

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

# ── Menu helpers ──────────────────────────────────────────────────────────────
def farmer_menu(name: str) -> str:
    return (
        f"Hello {name}! 🌾 How can I help you today?\n\n"
        f"╔══════════════════════╗\n"
        f"║  🌾 FARMER MENU      ║\n"
        f"╚══════════════════════╝\n\n"
        f"📋 POST JOB — Post a new job\n"
        f"📂 MY JOBS — View your posted jobs\n"
        f"👥 MY LABOURERS — Accepted/completed jobs\n"
        f"✅ JOB DONE [id] — Mark a job as completed\n"
        f"🚜 RENT EQUIPMENT — List equipment for rent\n"
        f"🔧 MY EQUIPMENT — View your listings\n"
        f"🏛️ SUBSIDIES — Government schemes\n"
        f"🪪 MY PROFILE — View your profile\n\n"
        f"💡 Tip: Send HELP anytime to see this menu."
    )

def labourer_menu(name: str) -> str:
    return (
        f"Hello {name}! 👋 How can I help you today?\n\n"
        f"╔══════════════════════╗\n"
        f"║  👷 LABOURER MENU    ║\n"
        f"╚══════════════════════╝\n\n"
        f"🔍 VIEW JOBS — See jobs near you\n"
        f"👨‍🌾 MY FARMERS — Accepted/completed jobs\n"
        f"🚜 VIEW EQUIPMENT — Browse equipment for rent\n"
        f"🏛️ SUBSIDIES — Government schemes\n"
        f"🛠️ UPDATE SKILL — Change your listed skill\n"
        f"🪪 MY PROFILE — View your profile\n\n"
        f"💡 Tip: Send HELP anytime to see this menu."
    )

def welcome_back(phone: str) -> str | None:
    farmer = get_from_db("farmers", phone)
    if farmer:
        return farmer_menu(farmer["name"])
    labourer = get_from_db("labourers", phone)
    if labourer:
        return labourer_menu(labourer["name"])
    return None

# ── Core message handler ──────────────────────────────────────────────────────
def handle_message(phone: str, raw_body: str) -> str:
    raw_body = raw_body.strip()
    message  = raw_body.upper().strip()

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
            return farmer_menu(farmer["name"]).replace(
                f"Hello {farmer['name']}! 🌾 How can I help you today?",
                f"Welcome back, {farmer['name']}! 🌾"
            )
        labourer = get_from_db("labourers", phone)
        if labourer:
            sessions[phone] = {"step": "done", "role": "labourer"}
            return labourer_menu(labourer["name"]).replace(
                f"Hello {labourer['name']}! 👋 How can I help you today?",
                f"Welcome back, {labourer['name']}! 👋"
            )
        sessions[phone]["step"] = "role"
        return (
            "🌾 *Welcome to Farm Connect!*\n\n"
            "Connecting farmers and labourers across Tamil Nadu.\n\n"
            "Are you a *FARMER* or *LABOURER*?\n"
            "Reply FARMER or LABOURER to get started."
        )

    # ── REGISTRATION ──────────────────────────────────────────────────────────
    elif step == "role":
        if message in ("FARMER", "LABOURER"):
            opposite_table = "labourers" if message == "FARMER" else "farmers"
            opposite_role  = "labourer" if message == "FARMER" else "farmer"
            existing_opposite = get_from_db(opposite_table, phone)
            if existing_opposite:
                sessions[phone] = {"step": "done", "role": opposite_role}
                menu = (farmer_menu(existing_opposite["name"]) if opposite_role == "farmer"
                        else labourer_menu(existing_opposite["name"]))
                return (
                    f"⚠️ This number is already registered as a *{opposite_role.upper()}* "
                    f"({existing_opposite['name']}).\n\n"
                    f"A phone number can only be registered under one role.\n\n"
                    f"{menu}"
                )
            sessions[phone]["role"] = message.lower()
            sessions[phone]["step"] = "name"
            return "Great! What is your name?"
        return "Please reply with FARMER or LABOURER only."

    elif step == "name":
        sessions[phone]["name"] = raw_body
        sessions[phone]["step"] = "location"
        return (
            f"Nice to meet you, {raw_body}! 🙏\n\n"
            f"What is your village or town name?\n"
            f"(We'll also notify you about jobs/equipment in nearby areas, "
            f"not just an exact match.)"
        )

    elif step == "location":
        sessions[phone]["location"] = raw_body.title()
        role = sessions[phone].get("role")
        if role == "labourer":
            sessions[phone]["step"] = "skill"
            return SKILL_PROMPT
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
                f"✅ *Registered as Farmer!*\n\n"
                f"👤 Name: {sessions[phone]['name']}\n"
                f"📍 Location: {sessions[phone]['location']}\n\n"
                f"Reply POST JOB to post your first job! 🌾"
            )

    elif step == "skill":
        skill = SKILL_MAP.get(message)
        if not skill:
            return f"Please reply with a number 1-6 or skill name.\n\n{SKILL_PROMPT}"
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
            f"✅ *Registered as Labourer!*\n\n"
            f"👤 Name: {sessions[phone]['name']}\n"
            f"📍 Location: {sessions[phone]['location']}\n"
            f"🛠️ Skill: {skill}\n\n"
            f"Reply VIEW JOBS to see available jobs near you! 💪"
        )

    # ── UPDATE SKILL FLOW (existing labourers) ────────────────────────────────
    elif step == "update_skill":
        skill = SKILL_MAP.get(message)
        if not skill:
            return f"Please reply with a number 1-6 or skill name.\n\n{SKILL_PROMPT}"
        updated = update_db("labourers", {"phone": phone}, {"skill": skill})
        sessions[phone]["step"] = "done"
        if not updated:
            return "⚠️ Error updating your skill. Please try again by sending UPDATE SKILL."
        return f"✅ Your skill has been updated to *{skill}*.\n\nReply VIEW JOBS to see work near you."

    # ── MAIN MENU ─────────────────────────────────────────────────────────────
    elif step == "done":
        print(f"[FLOW] DONE menu — message='{message}'")

        # ── Greeting / Help intercept ─────────────────────────────────────────
        normalised = message.strip("!?.👋🌾 ")
        if normalised in GREETINGS or message in GREETINGS:
            menu = welcome_back(phone)
            if menu:
                return menu
            sessions[phone] = {"step": "start"}
            return (
                "🌾 Welcome to Farm Connect!\n\n"
                "Are you a FARMER or LABOURER?\n"
                "Reply FARMER or LABOURER to get started."
            )

        # ── UPDATE SKILL ──────────────────────────────────────────────────────
        if message == "UPDATE SKILL":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only registered labourers can update their skill."
            sessions[phone]["step"] = "update_skill"
            current = labourer.get("skill") or "Not set"
            return (
                f"🛠️ Your current skill: *{current}*\n\n"
                f"{SKILL_PROMPT}"
            )

        # ── POST JOB ──────────────────────────────────────────────────────────
        elif message == "POST JOB":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only registered farmers can post jobs."
            sessions[phone]["step"] = "job_work_type"
            sessions[phone]["job"] = {}
            return (
                "📋 *Let's post your job!*\n\n"
                "What type of work is needed?\n"
                "(e.g. Harvesting, Planting, Irrigation, Weeding)"
            )

        # ── MY PROFILE ────────────────────────────────────────────────────────
        elif message == "MY PROFILE":
            farmer = get_from_db("farmers", phone)
            if farmer:
                total_posted = count_jobs_posted_by_farmer(phone)
                rating = farmer.get("rating")
                total_ratings = farmer.get("total_ratings", 0)
                if rating and total_ratings:
                    rating_str = f"{rating}⭐ ({total_ratings} rating{'s' if total_ratings != 1 else ''})"
                else:
                    rating_str = "No ratings yet"
                return (
                    f"🪪 *My Profile*\n\n"
                    f"👤 Name: {farmer['name']}\n"
                    f"🧾 Role: Farmer\n"
                    f"📍 Location: {farmer['location']}\n"
                    f"⭐ Rating: {rating_str}\n"
                    f"📋 Total jobs posted: {total_posted}\n\n"
                    f"Reply POST JOB to post a new job."
                )
            labourer = get_from_db("labourers", phone)
            if labourer:
                total_done = count_jobs_done_by_labourer(phone)
                rating = labourer.get("rating")
                total_ratings = labourer.get("total_ratings", 0)
                if rating and total_ratings:
                    rating_str = f"{rating}⭐ ({total_ratings} rating{'s' if total_ratings != 1 else ''})"
                else:
                    rating_str = "No ratings yet"
                return (
                    f"🪪 *My Profile*\n\n"
                    f"👤 Name: {labourer['name']}\n"
                    f"🧾 Role: Labourer\n"
                    f"📍 Location: {labourer['location']}\n"
                    f"🛠️ Skill: {labourer.get('skill') or 'Not set — reply UPDATE SKILL to set it'}\n"
                    f"⭐ Rating: {rating_str}\n"
                    f"✅ Total jobs completed: {total_done}\n\n"
                    f"Reply VIEW JOBS to find more work."
                )
            return "❌ Please register first. Reply HI to get started."

        # ── MY LABOURERS ──────────────────────────────────────────────────────
        elif message == "MY LABOURERS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can use this command."
            pending   = get_confirmed_jobs_for_farmer(phone)
            completed = get_completed_jobs_for_farmer(phone)
            if not pending and not completed:
                return "No accepted jobs found.\nReply POST JOB to post one."
            msg = ""
            if pending:
                msg += "👥 *Accepted — Not Yet Completed:*\n\n"
                for job in pending:
                    labourer_phone = job.get("labourer_phone")
                    labourer = get_from_db("labourers", labourer_phone) if labourer_phone else None
                    labourer_name = labourer["name"] if labourer else "Unknown"
                    msg += (
                        f"🔹 Job #{job['id']}\n"
                        f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                        f"   Labourer: {labourer_name}\n"
                        f"   🕓 In progress\n\n"
                    )
                msg += "Reply JOB DONE [job_id] once the work is finished.\nExample: JOB DONE 12\n\n"
            if completed:
                msg += "✅ *Completed Jobs:*\n\n"
                for job in completed:
                    rated = "✅ Rated" if job.get("rated") else "⭐ Not rated yet"
                    labourer_phone = job.get("labourer_phone")
                    labourer = get_from_db("labourers", labourer_phone) if labourer_phone else None
                    labourer_name = labourer["name"] if labourer else "Unknown"
                    rating_str = f" ({labourer['rating']}⭐)" if labourer and labourer.get("rating") else ""
                    msg += (
                        f"🔹 Job #{job['id']}\n"
                        f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                        f"   Labourer: {labourer_name}{rating_str}\n"
                        f"   {rated}\n\n"
                    )
                msg += "Reply RATE [job_id] [1-5] to rate a labourer.\nExample: RATE 12 5"
            return msg

        # ── MY FARMERS ────────────────────────────────────────────────────────
        elif message == "MY FARMERS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only labourers can use this command."
            pending   = get_confirmed_jobs_for_labourer(phone)
            completed = get_completed_jobs_for_labourer(phone)
            if not pending and not completed:
                return "No accepted jobs found.\nReply VIEW JOBS to find work."
            msg = ""
            if pending:
                msg += "👨‍🌾 *Accepted — Not Yet Completed:*\n\n"
                for job in pending:
                    farmer_phone = job.get("farmer_phone")
                    farmer = get_from_db("farmers", farmer_phone) if farmer_phone else None
                    farmer_name = farmer["name"] if farmer else "Unknown"
                    msg += (
                        f"🔹 Job #{job['id']}\n"
                        f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                        f"   Farmer: {farmer_name}\n"
                        f"   🕓 Waiting for farmer to mark JOB DONE\n\n"
                    )
            if completed:
                msg += "✅ *Completed Jobs:*\n\n"
                for job in completed:
                    rated = "✅ Rated" if job.get("labourer_rated") else "⭐ Not rated yet"
                    farmer_phone = job.get("farmer_phone")
                    farmer = get_from_db("farmers", farmer_phone) if farmer_phone else None
                    farmer_name = farmer["name"] if farmer else "Unknown"
                    rating_str = f" ({farmer['rating']}⭐)" if farmer and farmer.get("rating") else ""
                    msg += (
                        f"🔹 Job #{job['id']}\n"
                        f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                        f"   Farmer: {farmer_name}{rating_str}\n"
                        f"   {rated}\n\n"
                    )
                msg += "Reply RATE [job_id] [1-5] to rate a farmer.\nExample: RATE 12 5"
            return msg

        # ── JOB DONE ──────────────────────────────────────────────────────────
        elif message.startswith("JOB DONE"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: JOB DONE [job_id]\nExample: JOB DONE 12"
            job_id = parts[2]
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only the farmer who posted the job can mark it as done."
            updated = update_db(
                "jobs",
                {"id": job_id, "farmer_phone": phone, "status": "confirmed"},
                {"status": "completed"}
            )
            if not updated:
                return "❌ Job not found, not yours, or not in an accepted state."
            job = updated[0]
            labourer_phone = job.get("labourer_phone")
            labourer = get_from_db("labourers", labourer_phone) if labourer_phone else None
            labourer_name = labourer["name"] if labourer else "the labourer"
            if labourer_phone:
                send_whatsapp(
                    labourer_phone,
                    f"✅ *Job Marked as Completed!*\n\n"
                    f"🔨 Work: {job['work_type']}\n"
                    f"📍 Location: {job['location']}\n"
                    f"📅 Date: {job['start_date']}\n\n"
                    f"The farmer has confirmed this job is done. 🎉\n\n"
                    f"Please rate the farmer:\n"
                    f"Reply RATE {job['id']} [1-5]\nExample: RATE {job['id']} 5"
                )
            return (
                f"✅ *Job #{job['id']} marked as completed!*\n\n"
                f"🔨 Work: {job['work_type']}\n"
                f"👤 Labourer: {labourer_name}\n\n"
                f"Please rate the labourer:\n"
                f"Reply RATE {job['id']} [1-5]\nExample: RATE {job['id']} 5"
            )

        # ── RATE ──────────────────────────────────────────────────────────────
        elif message.startswith("RATE"):
            parts = raw_body.split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: RATE [job_id] [stars 1–5]\nExample: RATE 12 5"
            job_id, stars = parts[1], int(parts[2])
            if stars < 1 or stars > 5:
                return "Stars must be between 1 and 5."

            farmer   = get_from_db("farmers", phone)
            labourer = get_from_db("labourers", phone)
            if not farmer and not labourer:
                return "❌ Please register first. Reply HI to get started."

            try:
                url = f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}"
                res = req.get(url, headers=HEADERS, timeout=10)
                jobs = res.json()
            except Exception:
                return "❌ Could not fetch job. Try again."
            if not jobs:
                return "❌ Job not found."
            job = jobs[0]
            if job["status"] != "completed":
                return "❌ You can only rate jobs after the farmer marks them as JOB DONE."

            # ── Farmer rating their labourer ─────────────────────────────────
            if farmer and job.get("farmer_phone") == phone:
                if job.get("rated"):
                    return "You've already rated this job."
                labourer_phone = job.get("labourer_phone")
                if not labourer_phone:
                    return "❌ No labourer assigned to this job."
                target = get_from_db("labourers", labourer_phone)
                if not target:
                    return "❌ Labourer not found."
                old_total  = target.get("total_ratings", 0)
                old_rating = target.get("rating", 0)
                new_total  = old_total + 1
                new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
                update_db("labourers", {"phone": labourer_phone}, {"rating": new_rating, "total_ratings": new_total})
                update_db("jobs", {"id": job_id}, {"rated": True})
                star_display = "⭐" * stars
                return (
                    f"✅ Rated {target['name']} — {star_display}\n"
                    f"Their new rating: {new_rating}⭐ ({new_total} total ratings)"
                )

            # ── Labourer rating their farmer ─────────────────────────────────
            elif labourer and job.get("labourer_phone") == phone:
                if job.get("labourer_rated"):
                    return "You've already rated this job."
                farmer_phone = job.get("farmer_phone")
                if not farmer_phone:
                    return "❌ No farmer found for this job."
                target = get_from_db("farmers", farmer_phone)
                if not target:
                    return "❌ Farmer not found."
                old_total  = target.get("total_ratings", 0)
                old_rating = target.get("rating", 0)
                new_total  = old_total + 1
                new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
                update_db("farmers", {"phone": farmer_phone}, {"rating": new_rating, "total_ratings": new_total})
                update_db("jobs", {"id": job_id}, {"labourer_rated": True})
                star_display = "⭐" * stars
                return (
                    f"✅ Rated {target['name']} — {star_display}\n"
                    f"Their new rating: {new_rating}⭐ ({new_total} total ratings)"
                )

            else:
                return "❌ Job not found or doesn't belong to you."

        # ── MY JOBS ───────────────────────────────────────────────────────────
        elif message == "MY JOBS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can view their jobs."
            jobs = get_jobs_by_phone(phone)
            if not jobs:
                return "You haven't posted any jobs yet.\nReply POST JOB to post one."
            msg = "📋 *Your Recent Jobs:*\n\n"
            status_icon = {"open": "🟢", "confirmed": "🕓", "completed": "✅", "cancelled": "❌"}
            for i, job in enumerate(jobs):
                icon = status_icon.get(job["status"], "⚪")
                msg += (
                    f"{i+1}. {job['work_type']} — {job['location']}\n"
                    f"   👥 {job['num_labourers']} labourers | ₹{job['wage']}/day\n"
                    f"   📅 {job['start_date']} | {icon} {job['status'].upper()}\n"
                    f"   ID: {job['id']}\n\n"
                )
            msg += "Reply CANCEL [ID] to cancel a job, or JOB DONE [ID] once work is complete."
            return msg

        # ── VIEW JOBS ─────────────────────────────────────────────────────────
        elif message == "VIEW JOBS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only registered labourers can view jobs."
            jobs = get_open_jobs_by_location(labourer["location"])
            if not jobs:
                return (
                    f"No open jobs in {labourer['location']} right now. 😔\n\n"
                    f"We'll notify you the moment a new job is posted nearby! 🔔"
                )
            msg = f"🔍 *Open Jobs in {labourer['location']}:*\n\n"
            for i, job in enumerate(jobs):
                msg += (
                    f"{i+1}. 🔨 {job['work_type']}\n"
                    f"   👥 {job['num_labourers']} needed | ₹{job['wage']}/day\n"
                    f"   📅 {job['start_date']}\n"
                    f"   ➡️ Reply CONFIRM {job['id']} to accept\n\n"
                )
            return msg

        # ── CONFIRM ───────────────────────────────────────────────────────────
        elif message.startswith("CONFIRM"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CONFIRM [job_id]\nExample: CONFIRM 3"
            job_id   = parts[1]
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return "❌ Only registered labourers can confirm jobs."
            try:
                url = f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}"
                res = req.get(url, headers=HEADERS, timeout=10)
                lookup = res.json()
            except Exception:
                return "❌ Could not fetch job. Try again."
            if not lookup:
                return "❌ Job not found or already confirmed."
            if lookup[0].get("farmer_phone") == phone:
                return "❌ You can't confirm your own posted job."
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
                f"✅ *Job Confirmed!*\n\n"
                f"👤 Labourer: {labourer['name']}\n"
                f"🛠️ Skill: {labourer.get('skill', 'General')}\n"
                f"🔨 Work: {job['work_type']}\n"
                f"📍 Location: {job['location']}\n"
                f"📅 Date: {job['start_date']}\n\n"
                f"Your labourer will arrive on the job date. 🌾\n"
                f"Once the work is finished, reply JOB DONE {job['id']} to close it out and unlock ratings."
            )
            return (
                f"✅ *Job Confirmed!*\n\n"
                f"🔨 Work: {job['work_type']}\n"
                f"📍 Location: {job['location']}\n"
                f"📅 Date: {job['start_date']}\n"
                f"💰 Wage: ₹{job['wage']}/day\n\n"
                f"Please arrive on time. Good luck! 💪\n"
                f"The farmer will mark the job as done once work is finished — that's when ratings open up."
            )

        # ── CANCEL JOB ────────────────────────────────────────────────────────
        elif message.startswith("CANCEL") and not message.startswith("CANCEL EQUIPMENT"):
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
                    f"⚠️ *Job Cancelled*\n\n"
                    f"🔨 Work: {job['work_type']}\n"
                    f"📍 Location: {job['location']}\n"
                    f"📅 Date: {job['start_date']}\n\n"
                    f"This job has been cancelled by the farmer. Sorry for the inconvenience."
                )
            return f"✅ Job #{job_id} has been cancelled."

        # ── RENT EQUIPMENT ────────────────────────────────────────────────────
        elif message == "RENT EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only registered farmers can list equipment."
            sessions[phone]["step"] = "equip_name"
            sessions[phone]["equip"] = {}
            return (
                "🚜 *Let's list your equipment!*\n\n"
                "What equipment do you want to rent out?\n"
                "(e.g. Tractor, Rotavator, Sprayer, Thresher)"
            )

        # ── VIEW EQUIPMENT ────────────────────────────────────────────────────
        elif message == "VIEW EQUIPMENT":
            user = get_from_db("farmers", phone) or get_from_db("labourers", phone)
            if not user:
                return "❌ Please register first to view equipment."
            location = user.get("location", "")
            items = get_equipment_by_location(location)
            if not items:
                return (
                    f"No equipment available for rent in {location} right now. 😔\n"
                    f"Check back later!"
                )
            msg = f"🚜 *Equipment Available in {location}:*\n\n"
            for i, item in enumerate(items):
                msg += (
                    f"{i+1}. 🔧 {item['name']}\n"
                    f"   💰 ₹{item['rent_per_day']}/day\n"
                    f"   📅 Until: {item.get('available_until') or 'Ongoing'}\n"
                    f"   ➡️ Reply BOOK EQUIPMENT {item['id']} to book\n\n"
                )
            return msg

        # ── BOOK EQUIPMENT ────────────────────────────────────────────────────
        elif message.startswith("BOOK EQUIPMENT"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: BOOK EQUIPMENT [id]\nExample: BOOK EQUIPMENT 3"
            equipment_id = parts[2]
            user = get_from_db("farmers", phone) or get_from_db("labourers", phone)
            if not user:
                return "❌ Please register first to book equipment."
            item = get_equipment_by_id(equipment_id)
            if not item:
                return "❌ Equipment not found."
            if not item.get("available"):
                return f"❌ Sorry, {item['name']} is no longer available for rent."
            if item.get("owner_phone") == phone:
                return "❌ You can't book your own equipment."
            updated = update_db(
                "equipment",
                {"id": equipment_id},
                {"available": False, "booked_by": phone}
            )
            if not updated:
                return "❌ Could not complete booking. Please try again."
            send_whatsapp(
                item["owner_phone"],
                f"🔔 *Equipment Booking Confirmed!*\n\n"
                f"🚜 Equipment: {item['name']}\n"
                f"👤 Booked by: {user['name']}\n"
                f"📞 Contact: {phone}\n"
                f"💰 Rent: ₹{item['rent_per_day']}/day\n\n"
                f"Please coordinate with them for pickup/delivery."
            )
            return (
                f"✅ *Equipment Booked!*\n\n"
                f"🚜 Equipment: {item['name']}\n"
                f"💰 Rent: ₹{item['rent_per_day']}/day\n"
                f"📅 Available until: {item.get('available_until') or 'Ongoing'}\n\n"
                f"The owner has been notified. They will contact you shortly! 📞"
            )

        # ── MY EQUIPMENT ──────────────────────────────────────────────────────
        elif message == "MY EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return "❌ Only farmers can manage equipment listings."
            items = get_equipment_by_owner(phone)
            if not items:
                return "You haven't listed any equipment yet.\nReply RENT EQUIPMENT to add one."
            msg = "🚜 *Your Equipment Listings:*\n\n"
            for i, item in enumerate(items):
                status = "✅ Available" if item.get("available") else "🔒 Booked"
                msg += (
                    f"{i+1}. 🔧 {item['name']}\n"
                    f"   💰 ₹{item['rent_per_day']}/day | {status}\n"
                    f"   📅 Until: {item.get('available_until') or 'Ongoing'}\n"
                    f"   ID: {item['id']}\n\n"
                )
            msg += "Reply CANCEL EQUIPMENT [id] to remove a listing."
            return msg

        # ── CANCEL EQUIPMENT ──────────────────────────────────────────────────
        elif message.startswith("CANCEL EQUIPMENT"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CANCEL EQUIPMENT [id]\nExample: CANCEL EQUIPMENT 3"
            equipment_id = parts[2]
            item = get_equipment_by_id(equipment_id)
            if not item:
                return "❌ Equipment not found."
            if item.get("owner_phone") != phone:
                return "❌ You can only cancel your own equipment listings."
            updated = update_db(
                "equipment",
                {"id": equipment_id},
                {"available": False}
            )
            if not updated:
                return "❌ Could not cancel listing. Please try again."
            booked_by = item.get("booked_by")
            if booked_by:
                send_whatsapp(
                    booked_by,
                    f"⚠️ *Equipment Booking Cancelled*\n\n"
                    f"🚜 Equipment: {item['name']}\n"
                    f"📍 Location: {item['location']}\n\n"
                    f"The owner has cancelled this listing. Sorry for the inconvenience."
                )
            return f"✅ Equipment listing #{equipment_id} ({item['name']}) has been cancelled."

        # ── SUBSIDIES ─────────────────────────────────────────────────────────
        elif message == "SUBSIDIES":
            schemes = active_schemes()
            expired = expired_schemes()
            if not schemes and not expired:
                return "No government schemes are available right now. Check back later!"
            msg = "🏛️ *Active Government Schemes:*\n\n"
            for i, scheme in enumerate(schemes):
                tag = expiry_tag(scheme)
                msg += f"{i+1}. 📌 {scheme['name']}\n   {scheme['short']}\n   {tag}\n\n"
            if not schemes:
                msg = "No schemes are currently open for application.\n\n"
            if expired:
                msg += "─────────────────────\n❌ *Recently Expired:*\n\n"
                offset = len(schemes)
                for i, scheme in enumerate(expired):
                    msg += f"{offset + i + 1}. 📌 {scheme['name']} — ❌ Expired\n"
                msg += "\n"
            msg += "Reply SUBSIDY [number] for full details.\nExample: SUBSIDY 1"
            return msg

        # ── SUBSIDY [n] ───────────────────────────────────────────────────────
        elif message.startswith("SUBSIDY"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: SUBSIDY [number]\nExample: SUBSIDY 2"
            schemes = active_schemes()
            expired = expired_schemes()
            combined = schemes + expired
            index = int(parts[1]) - 1
            if index < 0 or index >= len(combined):
                return f"❌ Invalid number. Reply SUBSIDIES to see the list (1–{len(combined)})."
            scheme = combined[index]
            is_expired = index >= len(schemes)

            if is_expired:
                header = f"🏛️ *{scheme['name']}*\n❌ Expired (last cycle ended {scheme['end_date'].strftime('%d %B %Y')})\n\n{next_cycle_estimate(scheme)}\n"
            else:
                tag = expiry_tag(scheme)
                deadline_line = renewal_or_deadline_line(scheme)
                header = f"🏛️ *{scheme['name']}*\n{tag}\n{deadline_line}\n"

            return (
                f"{header}\n"
                f"📋 *Eligibility:*\n{scheme['eligibility']}\n\n"
                f"💰 *Benefit:*\n{scheme['benefit']}\n\n"
                f"📝 *How to Apply:*\n{scheme['how_to_apply']}\n\n"
                f"🔗 *Apply:* {scheme['link']}\n\n"
                f"Reply SUBSIDIES to see the full list."
            )

        # ── Unknown command ───────────────────────────────────────────────────
        else:
            suggestion, hint = fuzzy_suggestion(message)
            if suggestion:
                return (
                    f"❓ Unknown command. Did you mean *{suggestion}*?\n\n"
                    f"{hint}\n\nSend it exactly as shown to continue."
                )
            farmer = get_from_db("farmers", phone)
            if farmer:
                return farmer_menu(farmer["name"])
            labourer = get_from_db("labourers", phone)
            if labourer:
                return labourer_menu(labourer["name"])
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
        return f"When do you need them? (e.g. {example_future_date_str()}, Tomorrow)"

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
            f"✅ *Job Posted Successfully!*\n\n"
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
            f"(e.g. {example_future_date_str(days_ahead=10)}, Tomorrow, or reply *ongoing* if no end date)"
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
            return "⚠️ Error listing your equipment. Please try again by sending RENT EQUIPMENT."
        notify_nearby_users_about_equipment(saved)
        return (
            f"✅ *Equipment Listed!*\n\n"
            f"🚜 Equipment: {equip['name']}\n"
            f"💰 Rent: ₹{equip['rent_per_day']}/day\n"
            f"📍 Location: {farmer.get('location', 'Unknown')}\n"
            f"📅 Available until: {available_until or 'Ongoing'}\n\n"
            f"Farmers and labourers nearby can now find your equipment! 🔔"
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
    return {"message": "Farm Connect API is running 🌾"}

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

# ── Reset session ──────────────────────────────────────────────────────────────
@app.post("/reset")
async def reset_session(request: Request):
    data  = await request.json()
    phone = data.get("phone", "").strip()
    if phone in sessions:
        del sessions[phone]
        print(f"[RESET] Session cleared for {phone}")
    return JSONResponse({"status": "reset", "phone": phone})

# ── Web chat UI ────────────────────────────────────────────────────────────────
@app.get("/test", response_class=HTMLResponse)
def chat_ui():
    return """<!DOCTYPE html>
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

  /* Quick-reply chips */
  .chips { display: flex; gap: 6px; flex-wrap: wrap; padding: 6px 16px; background: #111b21; border-top: 1px solid #1f2c33; flex-shrink: 0; }
  .chip { background: #2a3942; border: 1px solid #3b4a54; color: #aebac1; font-size: 11px; padding: 4px 10px; border-radius: 14px; cursor: pointer; transition: background 0.15s; user-select: none; }
  .chip:hover { background: #3b4a54; color: #e9edef; }
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
Type any message below or tap a quick reply.
<div class="time">now</div></div>
  </div>
</div>

<div class="chips">
  <span class="chip" onclick="quickSend('Hi')">Hi</span>
  <span class="chip" onclick="quickSend('POST JOB')">POST JOB</span>
  <span class="chip" onclick="quickSend('MY JOBS')">MY JOBS</span>
  <span class="chip" onclick="quickSend('VIEW JOBS')">VIEW JOBS</span>
  <span class="chip" onclick="quickSend('SUBSIDIES')">SUBSIDIES</span>
  <span class="chip" onclick="quickSend('RENT EQUIPMENT')">RENT EQUIPMENT</span>
  <span class="chip" onclick="quickSend('VIEW EQUIPMENT')">VIEW EQUIPMENT</span>
  <span class="chip" onclick="quickSend('MY LABOURERS')">MY LABOURERS</span>
  <span class="chip" onclick="quickSend('MY FARMERS')">MY FARMERS</span>
  <span class="chip" onclick="quickSend('UPDATE SKILL')">UPDATE SKILL</span>
  <span class="chip" onclick="quickSend('MY PROFILE')">MY PROFILE</span>
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

  // Escape HTML first, then turn any https:// URLs into clickable links.
  // WhatsApp auto-linkifies URLs natively; this does the same for the web UI.
  function escHtml(str) {
    let s = str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    s = s.replace(/(https?:\/\/[^\s<]+)/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer" style="color:#53bdeb;text-decoration:underline;">$1</a>');
    return s;
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

  async function sendMessage(overrideText) {
    const input  = document.getElementById('msg');
    const btn    = document.getElementById('send');
    const text   = overrideText || input.value.trim();
    if (!text) return;

    const phone = getPhone();
    if (!overrideText) { input.value = ''; input.style.height = 'auto'; }
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

  function quickSend(text) {
    sendMessage(text);
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
    addBubble(`Session reset for ${phone}.\\nSay Hi to start fresh! 👋`, 'recv');
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
