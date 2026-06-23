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

# ── Language preference store (phone → "EN" | "TA" | "HI") ──────────────────
LANG_PREFS: dict[str, str] = {}   # phone → language code

SUPPORTED_LANGS = {
    "1": "TA", "TAMIL": "TA", "தமிழ்": "TA",
    "2": "EN", "ENGLISH": "EN",
    "3": "HI", "HINDI": "HI", "हिंदी": "HI",
}

def get_lang(phone: str) -> str:
    """Return the stored language preference for this user, defaulting to EN."""
    return LANG_PREFS.get(phone, "EN")

def set_lang(phone: str, lang: str):
    LANG_PREFS[phone] = lang

# ── Central translation dictionary ────────────────────────────────────────────
T = {
    "welcome_new": {
        "EN": (
            "🌾 *Welcome to Farm Connect!*\n\n"
            "Connecting farmers and labourers across Tamil Nadu.\n\n"
            "Are you a *FARMER* or *LABOURER*?\n"
            "Reply FARMER or LABOURER to get started.\n\n"
            "🌐 Reply LANGUAGE to choose Tamil / Hindi / English."
        ),
        "TA": (
            "🌾 *Farm Connect-க்கு வரவேற்கிறோம்!*\n\n"
            "தமிழ்நாடு முழுவதும் விவசாயிகள் மற்றும் தொழிலாளர்களை இணைக்கிறோம்.\n\n"
            "நீங்கள் *விவசாயி (FARMER)* அல்லது *தொழிலாளர் (LABOURER)*?\n"
            "FARMER அல்லது LABOURER என்று பதில் அனுப்பவும்.\n\n"
            "🌐 மொழி மாற்ற LANGUAGE என்று அனுப்பவும்."
        ),
        "HI": (
            "🌾 *Farm Connect में आपका स्वागत है!*\n\n"
            "तमिलनाडु भर में किसानों और मजदूरों को जोड़ रहे हैं।\n\n"
            "क्या आप *किसान (FARMER)* हैं या *मजदूर (LABOURER)*?\n"
            "FARMER या LABOURER टाइप करें।\n\n"
            "🌐 भाषा बदलने के लिए LANGUAGE भेजें।"
        ),
    },
    "ask_name": {
        "EN": "Great! What is your name?",
        "TA": "நல்லது! உங்கள் பெயர் என்ன?",
        "HI": "बढ़िया! आपका नाम क्या है?",
    },
    "ask_location": {
        "EN": (
            "Nice to meet you, {name}! 🙏\n\n"
            "What is your village or town name?\n"
            "(We'll also notify you about jobs/equipment in nearby areas, not just an exact match.)"
        ),
        "TA": (
            "{name} அவர்களே, சந்தித்ததில் மகிழ்ச்சி! 🙏\n\n"
            "உங்கள் கிராமம் அல்லது நகரத்தின் பெயர் என்ன?\n"
            "(அருகிலுள்ள பகுதிகளில் உள்ள வேலைகள் பற்றியும் தெரிவிப்போம்.)"
        ),
        "HI": (
            "{name} जी, आपसे मिलकर अच्छा लगा! 🙏\n\n"
            "आपके गाँव या शहर का नाम क्या है?\n"
            "(हम आसपास के इलाकों में नौकरी/उपकरण की जानकारी भी देंगे।)"
        ),
    },
    "registered_farmer": {
        "EN": "✅ *Registered as Farmer!*\n\n👤 Name: {name}\n📍 Location: {location}\n\nReply POST JOB to post your first job! 🌾",
        "TA": "✅ *விவசாயியாக பதிவு செய்யப்பட்டது!*\n\n👤 பெயர்: {name}\n📍 இடம்: {location}\n\nஉங்கள் முதல் வேலையை போட POST JOB என்று அனுப்பவும்! 🌾",
        "HI": "✅ *किसान के रूप में पंजीकृत!*\n\n👤 नाम: {name}\n📍 स्थान: {location}\n\nपहली नौकरी पोस्ट करने के लिए POST JOB भेजें! 🌾",
    },
    "registered_labourer": {
        "EN": "✅ *Registered as Labourer!*\n\n👤 Name: {name}\n📍 Location: {location}\n🛠️ Skill: {skill}\n\nReply VIEW JOBS to see available jobs near you! 💪",
        "TA": "✅ *தொழிலாளராக பதிவு செய்யப்பட்டது!*\n\n👤 பெயர்: {name}\n📍 இடம்: {location}\n🛠️ திறன்: {skill}\n\nஉங்களுக்கு அருகிலுள்ள வேலைகளை காண VIEW JOBS அனுப்பவும்! 💪",
        "HI": "✅ *मजदूर के रूप में पंजीकृत!*\n\n👤 नाम: {name}\n📍 स्थान: {location}\n🛠️ कौशल: {skill}\n\nनजदीकी नौकरियाँ देखने के लिए VIEW JOBS भेजें! 💪",
    },
    "language_prompt": {
        "EN": (
            "🌐 *Choose Your Language / மொழி தேர்வு / भाषा चुनें*\n\n"
            "1️⃣  தமிழ் (Tamil)\n2️⃣  English\n3️⃣  हिंदी (Hindi)\n\nReply 1, 2, or 3."
        ),
        "TA": (
            "🌐 *உங்கள் மொழியை தேர்வு செய்யவும்*\n\n"
            "1️⃣  தமிழ் (Tamil)\n2️⃣  English\n3️⃣  हिंदी (Hindi)\n\n1, 2, அல்லது 3 என்று பதில் அனுப்பவும்."
        ),
        "HI": (
            "🌐 *अपनी भाषा चुनें*\n\n"
            "1️⃣  தமிழ் (Tamil)\n2️⃣  English\n3️⃣  हिंदी (Hindi)\n\n1, 2 या 3 टाइप करें।"
        ),
    },
    "language_set": {
        "EN": "✅ Language set to *English*. All messages will now be in English.",
        "TA": "✅ மொழி *தமிழ்* ஆக அமைக்கப்பட்டது. இனி எல்லா செய்திகளும் தமிழில் இருக்கும்.",
        "HI": "✅ भाषा *हिंदी* पर सेट की गई। अब सभी संदेश हिंदी में होंगे।",
    },
    "language_invalid": {
        "EN": "❓ Please reply 1 (Tamil), 2 (English), or 3 (Hindi).",
        "TA": "❓ 1 (தமிழ்), 2 (English), அல்லது 3 (हिंदी) என்று பதில் அனுப்பவும்.",
        "HI": "❓ कृपया 1 (Tamil), 2 (English), या 3 (Hindi) टाइप करें।",
    },
    "register_first": {
        "EN": "❌ Please register first. Reply HI to get started.",
        "TA": "❌ முதலில் பதிவு செய்யவும். தொடங்க HI என்று அனுப்பவும்.",
        "HI": "❌ पहले पंजीकरण करें। शुरू करने के लिए HI भेजें।",
    },
    "error_saving": {
        "EN": "⚠️ Error saving your details. Please try again.",
        "TA": "⚠️ விவரங்களை சேமிக்க பிழை. மீண்டும் முயற்சிக்கவும்.",
        "HI": "⚠️ विवरण सहेजने में त्रुटि। कृपया पुनः प्रयास करें।",
    },
    "farmers_only": {
        "EN": "❌ Only registered farmers can use this command.",
        "TA": "❌ பதிவு செய்த விவசாயிகள் மட்டுமே இந்த கட்டளையை பயன்படுத்தலாம்.",
        "HI": "❌ केवल पंजीकृत किसान ही इस आदेश का उपयोग कर सकते हैं।",
    },
    "labourers_only": {
        "EN": "❌ Only registered labourers can use this command.",
        "TA": "❌ பதிவு செய்த தொழிலாளர்கள் மட்டுமே இந்த கட்டளையை பயன்படுத்தலாம்.",
        "HI": "❌ केवल पंजीकृत मजदूर ही इस आदेश का उपयोग कर सकते हैं।",
    },
    "bad_format_id": {
        "EN": "❓ Couldn't read that.\n\nFormat: {fmt}\nExample: {ex}",
        "TA": "❓ புரியவில்லை.\n\nவடிவம்: {fmt}\nஉதாரணம்: {ex}",
        "HI": "❓ समझ नहीं आया।\n\nप्रारूप: {fmt}\nउदाहरण: {ex}",
    },
    "job_not_found": {
        "EN": "❌ Job not found or you don't own this job.",
        "TA": "❌ வேலை கிடைக்கவில்லை அல்லது இது உங்கள் வேலை இல்லை.",
        "HI": "❌ नौकरी नहीं मिली या यह आपकी नौकरी नहीं है।",
    },
    "db_fetch_error": {
        "EN": "❌ Could not fetch data. Please try again.",
        "TA": "❌ தரவை பெற முடியவில்லை. மீண்டும் முயற்சிக்கவும்.",
        "HI": "❌ डेटा नहीं मिला। कृपया पुनः प्रयास करें।",
    },
    "reply_farmer_or_labourer": {
        "EN": "Please reply with FARMER or LABOURER only.",
        "TA": "FARMER அல்லது LABOURER என்று மட்டும் பதில் அனுப்பவும்.",
        "HI": "कृपया केवल FARMER या LABOURER टाइप करें।",
    },
    "already_registered_as": {
        "EN": "⚠️ This number is already registered as a *{role}* ({name}).\n\nA phone number can only be registered under one role.\n\n{menu}",
        "TA": "⚠️ இந்த எண் ஏற்கனவே *{role}* ஆக பதிவு செய்யப்பட்டுள்ளது ({name}).\n\nஒரு தொலைபேசி எண் ஒரே ஒரு பாத்திரத்தில் மட்டுமே பதிவு செய்யலாம்.\n\n{menu}",
        "HI": "⚠️ यह नंबर पहले से *{role}* के रूप में पंजीकृत है ({name})।\n\nएक फ़ोन नंबर केवल एक भूमिका में पंजीकृत हो सकता है।\n\n{menu}",
    },
    "skill_invalid": {
        "EN": "Please reply with a number 1-6 or skill name.\n\n{prompt}",
        "TA": "1 முதல் 6 வரை ஒரு எண் அல்லது திறன் பெயர் அனுப்பவும்.\n\n{prompt}",
        "HI": "कृपया 1-6 के बीच कोई नंबर या कौशल का नाम टाइप करें।\n\n{prompt}",
    },
    "skill_updated": {
        "EN": "✅ Your skill has been updated to *{skill}*.\n\nReply VIEW JOBS to see work near you.",
        "TA": "✅ உங்கள் திறன் *{skill}* ஆக புதுப்பிக்கப்பட்டது.\n\nVIEW JOBS அனுப்பி அருகிலுள்ள வேலைகளை பாருங்கள்.",
        "HI": "✅ आपका कौशल *{skill}* में अपडेट किया गया।\n\nVIEW JOBS भेजकर पास की नौकरियाँ देखें।",
    },
    "skill_update_error": {
        "EN": "⚠️ Error updating your skill. Please try again by sending UPDATE SKILL.",
        "TA": "⚠️ திறனை புதுப்பிக்க பிழை. UPDATE SKILL அனுப்பி மீண்டும் முயற்சிக்கவும்.",
        "HI": "⚠️ कौशल अपडेट करने में त्रुटि। UPDATE SKILL भेजकर पुनः प्रयास करें।",
    },
    "update_skill_prompt": {
        "EN": "🛠️ Your current skill: *{skill}*\n\n{prompt}",
        "TA": "🛠️ உங்கள் தற்போதைய திறன்: *{skill}*\n\n{prompt}",
        "HI": "🛠️ आपका वर्तमान कौशल: *{skill}*\n\n{prompt}",
    },
    "post_job_start": {
        "EN": "📋 *Let's post your job!*\n\nWhat type of work is needed?\n(e.g. Harvesting, Planting, Irrigation, Weeding)",
        "TA": "📋 *வேலையை போடுவோம்!*\n\nஎன்ன வகை வேலை தேவை?\n(உதா: Harvesting, Planting, Irrigation, Weeding)",
        "HI": "📋 *नौकरी पोस्ट करते हैं!*\n\nकिस प्रकार का काम चाहिए?\n(जैसे: Harvesting, Planting, Irrigation, Weeding)",
    },
    "ask_num_labourers": {
        "EN": "How many labourers do you need?",
        "TA": "எத்தனை தொழிலாளர்கள் தேவை?",
        "HI": "कितने मजदूर चाहिए?",
    },
    "ask_num_labourers_invalid": {
        "EN": "Please enter a number. How many labourers do you need?",
        "TA": "ஒரு எண் உள்ளிடவும். எத்தனை தொழிலாளர்கள் தேவை?",
        "HI": "कृपया एक संख्या दर्ज करें। कितने मजदूर चाहिए?",
    },
    "ask_wage": {
        "EN": "What is the wage per day? (in ₹)",
        "TA": "நாளொன்றுக்கு கூலி என்ன? (₹ இல்)",
        "HI": "प्रति दिन मजदूरी क्या है? (₹ में)",
    },
    "ask_wage_with_avg": {
        "EN": "What is the wage per day? (in ₹)\n\n💡 Average for {work_type} near {location} is ₹{avg}/day (based on completed jobs).",
        "TA": "நாளொன்றுக்கு கூலி என்ன? (₹ இல்)\n\n💡 {location} அருகில் {work_type} வேலைக்கு சராசரி கூலி ₹{avg}/நாள் (முடிந்த வேலைகள் அடிப்படையில்).",
        "HI": "प्रति दिन मजदूरी क्या है? (₹ में)\n\n💡 {location} के पास {work_type} के लिए औसत मजदूरी ₹{avg}/दिन है (पूर्ण नौकरियों के आधार पर)।",
    },
    "ask_wage_invalid": {
        "EN": "Please enter a valid amount (e.g. 600). What is the wage per day?",
        "TA": "சரியான தொகை உள்ளிடவும் (உதா: 600). நாளொன்றுக்கு கூலி என்ன?",
        "HI": "कृपया एक सही राशि दर्ज करें (जैसे 600)। प्रति दिन मजदूरी क्या है?",
    },
    "wage_below_avg": {
        "EN": "⚠️ That's noticeably below the area average of ₹{avg}/day for this work — you may get fewer responses.\n\n{date_prompt}",
        "TA": "⚠️ இது இப்பகுதியில் இந்த வேலைக்கான சராசரி ₹{avg}/நாளை விட குறைவாக உள்ளது — குறைவான பதில்கள் வரலாம்.\n\n{date_prompt}",
        "HI": "⚠️ यह इस काम के क्षेत्र औसत ₹{avg}/दिन से काफी कम है — कम प्रतिक्रियाएँ मिल सकती हैं।\n\n{date_prompt}",
    },
    "ask_date": {
        "EN": "When do you need them? (e.g. {example}, Tomorrow)",
        "TA": "எந்த தேதியில் தேவை? (உதா: {example}, Tomorrow)",
        "HI": "कब चाहिए? (जैसे: {example}, Tomorrow)",
    },
    "job_posted": {
        "EN": "✅ *Job Posted Successfully!*\n\n📍 Location: {location}\n🔨 Work: {work_type}\n👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n{weather_line}\nNotifying nearby labourers now! 🔔",
        "TA": "✅ *வேலை வெற்றிகரமாக போடப்பட்டது!*\n\n📍 இடம்: {location}\n🔨 வேலை: {work_type}\n👥 தேவையான தொழிலாளர்கள்: {num_labourers}\n💰 கூலி: ₹{wage}/நாள்\n📅 தேதி: {start_date}\n{weather_line}\nஅருகிலுள்ள தொழிலாளர்களுக்கு தெரிவிக்கிறோம்! 🔔",
        "HI": "✅ *नौकरी सफलतापूर्वक पोस्ट हुई!*\n\n📍 स्थान: {location}\n🔨 काम: {work_type}\n👥 मजदूर चाहिए: {num_labourers}\n💰 मजदूरी: ₹{wage}/दिन\n📅 तारीख: {start_date}\n{weather_line}\nपास के मजदूरों को सूचित कर रहे हैं! 🔔",
    },
    "job_post_error": {
        "EN": "⚠️ Error posting your job. Please try again by sending POST JOB.",
        "TA": "⚠️ வேலையை போட பிழை. POST JOB அனுப்பி மீண்டும் முயற்சிக்கவும்.",
        "HI": "⚠️ नौकरी पोस्ट करने में त्रुटि। POST JOB भेजकर पुनः प्रयास करें।",
    },
    "farmer_profile_not_found": {
        "EN": "❌ Could not find your farmer profile. Please try again.",
        "TA": "❌ உங்கள் விவசாயி சுயவிவரம் கிடைக்கவில்லை. மீண்டும் முயற்சிக்கவும்.",
        "HI": "❌ आपकी किसान प्रोफ़ाइल नहीं मिली। कृपया पुनः प्रयास करें।",
    },
    "my_jobs_empty": {
        "EN": "You haven't posted any jobs yet.\nReply POST JOB to post one.",
        "TA": "நீங்கள் இன்னும் எந்த வேலையும் போடவில்லை.\nPOST JOB அனுப்பி ஒரு வேலை போடுங்கள்.",
        "HI": "आपने अभी तक कोई नौकरी पोस्ट नहीं की।\nPOST JOB भेजकर एक पोस्ट करें।",
    },
    "my_jobs_header": {
        "EN": "📋 *Your Recent Jobs:*\n\n",
        "TA": "📋 *உங்கள் சமீபத்திய வேலைகள்:*\n\n",
        "HI": "📋 *आपकी हालिया नौकरियाँ:*\n\n",
    },
    "my_jobs_footer": {
        "EN": "Reply CANCEL [ID] to cancel a job, or JOB DONE [ID] once work is complete.",
        "TA": "CANCEL [ID] அனுப்பி வேலையை ரத்து செய்யலாம், அல்லது வேலை முடிந்தால் JOB DONE [ID] அனுப்பவும்.",
        "HI": "CANCEL [ID] भेजकर नौकरी रद्द करें, या काम पूरा होने पर JOB DONE [ID] भेजें।",
    },
    "status_open":      {"EN": "OPEN",      "TA": "திறந்துள்ளது",              "HI": "खुली"},
    "status_confirmed": {"EN": "CONFIRMED", "TA": "உறுதிப்படுத்தப்பட்டது",   "HI": "पक्की"},
    "status_completed": {"EN": "COMPLETED", "TA": "முடிந்தது",                "HI": "पूर्ण"},
    "status_cancelled": {"EN": "CANCELLED", "TA": "ரத்து செய்யப்பட்டது",     "HI": "रद्द"},
    "no_jobs_nearby": {
        "EN": "No open jobs near {location} right now. 😔\n\nWe'll notify you the moment a new job is posted nearby! 🔔",
        "TA": "{location} அருகில் இப்போது எந்த வேலையும் இல்லை. 😔\n\nபுதிய வேலை வந்தவுடன் உடனடியாக தெரிவிப்போம்! 🔔",
        "HI": "{location} के पास अभी कोई नौकरी नहीं है। 😔\n\nनजदीकी नौकरी मिलते ही सूचित करेंगे! 🔔",
    },
    "view_jobs_header": {
        "EN": "🔍 *Open Jobs Near {location}:*\n\n{rating_line}",
        "TA": "🔍 *{location} அருகில் திறந்த வேலைகள்:*\n\n{rating_line}",
        "HI": "🔍 *{location} के पास खुली नौकरियाँ:*\n\n{rating_line}",
    },
    "rating_line": {
        "EN": "⭐ Your rating: {rating}⭐ ({count} rating{s})\n\n",
        "TA": "⭐ உங்கள் மதிப்பீடு: {rating}⭐ ({count} மதிப்பீடு{s})\n\n",
        "HI": "⭐ आपकी रेटिंग: {rating}⭐ ({count} रेटिंग{s})\n\n",
    },
    "no_rating_yet": {
        "EN": "⭐ Your rating: No ratings yet\n\n",
        "TA": "⭐ உங்கள் மதிப்பீடு: இன்னும் மதிப்பீடு இல்லை\n\n",
        "HI": "⭐ आपकी रेटिंग: अभी कोई रेटिंग नहीं\n\n",
    },
    "view_jobs_item": {
        "EN": "{i}. 🔨 {work_type}\n   📍 Location: {location}\n   👥 {num_labourers} needed | ₹{wage}/day\n   📅 {start_date}\n   ➡️ Reply CONFIRM {job_id} to accept\n\n",
        "TA": "{i}. 🔨 {work_type}\n   📍 இடம்: {location}\n   👥 {num_labourers} தேவை | ₹{wage}/நாள்\n   📅 {start_date}\n   ➡️ CONFIRM {job_id} அனுப்பி ஏற்றுக்கொள்ளவும்\n\n",
        "HI": "{i}. 🔨 {work_type}\n   📍 स्थान: {location}\n   👥 {num_labourers} चाहिए | ₹{wage}/दिन\n   📅 {start_date}\n   ➡️ CONFIRM {job_id} भेजकर स्वीकार करें\n\n",
    },
    "job_confirmed_labourer": {
        "EN": "✅ *Job Confirmed!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n💰 Wage: ₹{wage}/day\n\nPlease arrive on time. Good luck! 💪",
        "TA": "✅ *வேலை உறுதிப்படுத்தப்பட்டது!*\n\n🔨 வேலை: {work_type}\n📍 இடம்: {location}\n📅 தேதி: {start_date}\n💰 கூலி: ₹{wage}/நாள்\n\nசரியான நேரத்தில் வாருங்கள். வாழ்த்துக்கள்! 💪",
        "HI": "✅ *नौकरी पक्की हुई!*\n\n🔨 काम: {work_type}\n📍 स्थान: {location}\n📅 तारीख: {start_date}\n💰 मजदूरी: ₹{wage}/दिन\n\nसमय पर पहुँचें। शुभकामनाएँ! 💪",
    },
    "confirm_not_labourer": {
        "EN": "❌ Only registered labourers can confirm jobs.",
        "TA": "❌ பதிவு செய்த தொழிலாளர்கள் மட்டுமே வேலைகளை ஏற்றுக்கொள்ளலாம்.",
        "HI": "❌ केवल पंजीकृत मजदूर ही नौकरी स्वीकार कर सकते हैं।",
    },
    "confirm_own_job": {
        "EN": "❌ You can't confirm your own posted job.",
        "TA": "❌ நீங்கள் போட்ட வேலையை நீங்களே ஏற்றுக்கொள்ள முடியாது.",
        "HI": "❌ आप अपनी ही पोस्ट की नौकरी स्वीकार नहीं कर सकते।",
    },
    "confirm_already_taken": {
        "EN": "❌ Job not found or already confirmed.",
        "TA": "❌ வேலை கிடைக்கவில்லை அல்லது ஏற்கனவே உறுதிப்படுத்தப்பட்டது.",
        "HI": "❌ नौकरी नहीं मिली या पहले से पक्की हो चुकी है।",
    },
    "job_done_not_farmer": {
        "EN": "❌ Only the farmer who posted the job can mark it as done.",
        "TA": "❌ வேலையை போட்ட விவசாயி மட்டுமே முடிந்ததாக குறிக்கலாம்.",
        "HI": "❌ केवल नौकरी पोस्ट करने वाला किसान ही इसे पूर्ण मार्क कर सकता है।",
    },
    "job_done_not_found": {
        "EN": "❌ Job not found, not yours, or not in an accepted state.",
        "TA": "❌ வேலை கிடைக்கவில்லை, உங்களுடையது இல்லை, அல்லது ஏற்றுக்கொள்ளப்பட்ட நிலையில் இல்லை.",
        "HI": "❌ नौकरी नहीं मिली, आपकी नहीं है, या स्वीकृत स्थिति में नहीं है।",
    },
    "job_done_success": {
        "EN": "✅ *Job #{job_id} marked as completed!*\n\n🔨 Work: {work_type}\n👤 Labourer: {labourer_name}\n\nPlease rate the labourer:\nReply RATE {job_id} [1-5]\nExample: RATE {job_id} 5",
        "TA": "✅ *வேலை #{job_id} முடிந்ததாக குறிக்கப்பட்டது!*\n\n🔨 வேலை: {work_type}\n👤 தொழிலாளர்: {labourer_name}\n\nதொழிலாளரை மதிப்பிடவும்:\nRATE {job_id} [1-5] அனுப்பவும்\nஉதாரணம்: RATE {job_id} 5",
        "HI": "✅ *नौकरी #{job_id} पूर्ण मार्क की गई!*\n\n🔨 काम: {work_type}\n👤 मजदूर: {labourer_name}\n\nमजदूर को रेट करें:\nRATE {job_id} [1-5] भेजें\nउदाहरण: RATE {job_id} 5",
    },
    "rate_invalid_format": {
        "EN": "❓ Couldn't read that.\n\nFormat: RATE [job_id] [stars 1–5]\nExample: RATE 12 5",
        "TA": "❓ புரியவில்லை.\n\nவடிவம்: RATE [job_id] [மதிப்பு 1–5]\nஉதாரணம்: RATE 12 5",
        "HI": "❓ समझ नहीं आया।\n\nप्रारूप: RATE [job_id] [स्टार 1–5]\nउदाहरण: RATE 12 5",
    },
    "rate_stars_range": {
        "EN": "Stars must be between 1 and 5.",
        "TA": "மதிப்பு 1 முதல் 5 வரை இருக்க வேண்டும்.",
        "HI": "स्टार 1 से 5 के बीच होना चाहिए।",
    },
    "rate_job_not_completed": {
        "EN": "❌ You can only rate jobs after the farmer marks them as JOB DONE.",
        "TA": "❌ விவசாயி JOB DONE என்று குறித்த பிறகு மட்டுமே மதிப்பிட முடியும்.",
        "HI": "❌ किसान द्वारा JOB DONE मार्क करने के बाद ही रेट कर सकते हैं।",
    },
    "rate_already_rated": {
        "EN": "You've already rated this job.",
        "TA": "இந்த வேலையை ஏற்கனவே மதிப்பிட்டுவிட்டீர்கள்.",
        "HI": "आप इस नौकरी को पहले ही रेट कर चुके हैं।",
    },
    "rate_no_labourer": {
        "EN": "❌ No labourer assigned to this job.",
        "TA": "❌ இந்த வேலைக்கு எந்த தொழிலாளரும் ஒதுக்கப்படவில்லை.",
        "HI": "❌ इस नौकरी में कोई मजदूर नियुक्त नहीं है।",
    },
    "rate_no_farmer": {
        "EN": "❌ No farmer found for this job.",
        "TA": "❌ இந்த வேலைக்கு விவசாயி கிடைக்கவில்லை.",
        "HI": "❌ इस नौकरी के लिए किसान नहीं मिला।",
    },
    "rate_person_not_found": {
        "EN": "❌ {role} not found.",
        "TA": "❌ {role} கிடைக்கவில்லை.",
        "HI": "❌ {role} नहीं मिला।",
    },
    "rate_success": {
        "EN": "✅ Rated {name} — {stars}\nTheir new rating: {new_rating}⭐ ({total} total ratings)",
        "TA": "✅ {name} க்கு மதிப்பு — {stars}\nபுதிய மதிப்பீடு: {new_rating}⭐ (மொத்தம் {total} மதிப்பீடுகள்)",
        "HI": "✅ {name} को रेट किया — {stars}\nनई रेटिंग: {new_rating}⭐ (कुल {total} रेटिंग)",
    },
    "rate_not_your_job": {
        "EN": "❌ Job not found or doesn't belong to you.",
        "TA": "❌ வேலை கிடைக்கவில்லை அல்லது உங்களுடையது இல்லை.",
        "HI": "❌ नौकरी नहीं मिली या आपकी नहीं है।",
    },
    "cancel_success": {
        "EN": "✅ Job #{job_id} has been cancelled.",
        "TA": "✅ வேலை #{job_id} ரத்து செய்யப்பட்டது.",
        "HI": "✅ नौकरी #{job_id} रद्द कर दी गई।",
    },
    "cancel_penalty_line": {
        "EN": "\n📉 Since a labourer had already accepted this job, a ₹{amount} penalty and a {rating_drop}⭐ rating drop have been applied to your account.",
        "TA": "\n📉 ஒரு தொழிலாளர் ஏற்கனவே ஏற்றுக்கொண்டதால், ₹{amount} அபராதம் மற்றும் {rating_drop}⭐ மதிப்பீடு குறைவு உங்கள் கணக்கில் பதிவு செய்யப்பட்டது.",
        "HI": "\n📉 एक मजदूर पहले ही स्वीकार कर चुका था, इसलिए ₹{amount} जुर्माना और {rating_drop}⭐ रेटिंग गिरावट आपके खाते में लागू की गई।",
    },
    "no_show_not_farmer": {
        "EN": "❌ Only the farmer who posted the job can report a no-show.",
        "TA": "❌ வேலையை போட்ட விவசாயி மட்டுமே வராமல் போனதை புகார் செய்யலாம்.",
        "HI": "❌ केवल नौकरी पोस्ट करने वाला किसान ही नो-शो रिपोर्ट कर सकता है।",
    },
    "no_show_not_found": {
        "EN": "❌ Job not found, not yours, or not in an accepted (confirmed) state.",
        "TA": "❌ வேலை கிடைக்கவில்லை, உங்களுடையது இல்லை, அல்லது ஏற்றுக்கொள்ளப்பட்ட நிலையில் இல்லை.",
        "HI": "❌ नौकरी नहीं मिली, आपकी नहीं है, या स्वीकृत स्थिति में नहीं है।",
    },
    "no_show_no_labourer": {
        "EN": "✅ Job #{job_id} marked cancelled, but no labourer was on record to flag.",
        "TA": "✅ வேலை #{job_id} ரத்தாக குறிக்கப்பட்டது, ஆனால் புகார் செய்ய தொழிலாளர் பதிவு இல்லை.",
        "HI": "✅ नौकरी #{job_id} रद्द मार्क की गई, लेकिन फ्लैग करने के लिए कोई मजदूर दर्ज नहीं था।",
    },
    "no_show_success": {
        "EN": "✅ Reported. Job #{job_id} has been cancelled and {labourer_name} has been flagged for not showing up.\n\n📉 A ₹{amount} penalty and a {rating_drop}⭐ rating drop have been applied to their account.\n\nReply POST JOB to re-post this work.",
        "TA": "✅ புகார் செய்யப்பட்டது. வேலை #{job_id} ரத்தாகி {labourer_name} வரவில்லை என்று கொடியிடப்பட்டது.\n\n📉 ₹{amount} அபராதம் மற்றும் {rating_drop}⭐ மதிப்பீடு குறைவு அவர்கள் கணக்கில் பதிவு செய்யப்பட்டது.\n\nPOST JOB அனுப்பி இந்த வேலையை மீண்டும் போடுங்கள்.",
        "HI": "✅ रिपोर्ट किया गया। नौकरी #{job_id} रद्द हुई और {labourer_name} को नो-शो के लिए फ्लैग किया गया।\n\n📉 ₹{amount} जुर्माना और {rating_drop}⭐ रेटिंग गिरावट उनके खाते में लागू की गई।\n\nPOST JOB भेजकर यह काम दोबारा पोस्ट करें।",
    },
    "my_labourers_empty": {
        "EN": "No accepted jobs found.\nReply POST JOB to post one.",
        "TA": "ஏற்றுக்கொள்ளப்பட்ட வேலைகள் இல்லை.\nPOST JOB அனுப்பி ஒரு வேலை போடுங்கள்.",
        "HI": "कोई स्वीकृत नौकरी नहीं मिली।\nPOST JOB भेजकर एक पोस्ट करें।",
    },
    "my_labourers_pending_header": {
        "EN": "👥 *Accepted — Not Yet Completed:*\n\n",
        "TA": "👥 *ஏற்றுக்கொள்ளப்பட்டது — இன்னும் முடிக்கவில்லை:*\n\n",
        "HI": "👥 *स्वीकृत — अभी पूर्ण नहीं:*\n\n",
    },
    "my_labourers_pending_footer": {
        "EN": "Reply JOB DONE [job_id] once the work is finished.\nExample: JOB DONE 12\nIf they didn't show up, reply NO SHOW [job_id] instead.\n\n",
        "TA": "வேலை முடிந்தால் JOB DONE [job_id] அனுப்பவும்.\nஉதாரணம்: JOB DONE 12\nவரவில்லை என்றால் NO SHOW [job_id] அனுப்பவும்.\n\n",
        "HI": "काम पूरा होने पर JOB DONE [job_id] भेजें।\nउदाहरण: JOB DONE 12\nअगर नहीं आए तो NO SHOW [job_id] भेजें।\n\n",
    },
    "my_labourers_completed_header": {
        "EN": "✅ *Completed Jobs:*\n\n",
        "TA": "✅ *முடிந்த வேலைகள்:*\n\n",
        "HI": "✅ *पूर्ण नौकरियाँ:*\n\n",
    },
    "my_labourers_completed_footer": {
        "EN": "Reply RATE [job_id] [1-5] to rate a labourer.\nExample: RATE 12 5\nWorked well with someone? Reply REHIRE [job_id] to invite them again.",
        "TA": "RATE [job_id] [1-5] அனுப்பி தொழிலாளரை மதிப்பிடவும்.\nஉதாரணம்: RATE 12 5\nநன்றாக வேலை செய்தவரை மீண்டும் REHIRE [job_id] அனுப்பி அழையுங்கள்.",
        "HI": "RATE [job_id] [1-5] भेजकर मजदूर को रेट करें।\nउदाहरण: RATE 12 5\nअच्छा काम किया? REHIRE [job_id] भेजकर दोबारा बुलाएँ।",
    },
    "in_progress":      {"EN": "🕓 In progress",     "TA": "🕓 நடந்துகொண்டிருக்கிறது",  "HI": "🕓 जारी है"},
    "rated_label":      {"EN": "✅ Rated",            "TA": "✅ மதிப்பிடப்பட்டது",        "HI": "✅ रेट किया गया"},
    "not_rated_label":  {"EN": "⭐ Not rated yet",    "TA": "⭐ இன்னும் மதிப்பிடவில்லை", "HI": "⭐ अभी रेट नहीं किया"},
    "no_show_flag":     {"EN": " ⚠️ {count} past no-show(s)", "TA": " ⚠️ {count} முறை வரவில்லை", "HI": " ⚠️ {count} बार नो-शो"},
    "unknown_name":     {"EN": "Unknown",             "TA": "தெரியவில்லை",                "HI": "अज्ञात"},
    "my_farmers_empty": {
        "EN": "No accepted jobs found.\nReply VIEW JOBS to find work.",
        "TA": "ஏற்றுக்கொள்ளப்பட்ட வேலைகள் இல்லை.\nVIEW JOBS அனுப்பி வேலை தேடுங்கள்.",
        "HI": "कोई स्वीकृत नौकरी नहीं मिली।\nVIEW JOBS भेजकर काम खोजें।",
    },
    "my_farmers_pending_header": {
        "EN": "👨‍🌾 *Accepted — Not Yet Completed:*\n\n",
        "TA": "👨‍🌾 *ஏற்றுக்கொள்ளப்பட்டது — இன்னும் முடிக்கவில்லை:*\n\n",
        "HI": "👨‍🌾 *स्वीकृत — अभी पूर्ण नहीं:*\n\n",
    },
    "my_farmers_pending_waiting": {
        "EN": "🕓 Waiting for farmer to mark JOB DONE",
        "TA": "🕓 விவசாயி JOB DONE குறிக்க காத்திருக்கிறோம்",
        "HI": "🕓 किसान द्वारा JOB DONE मार्क करने की प्रतीक्षा है",
    },
    "my_farmers_completed_header": {
        "EN": "✅ *Completed Jobs:*\n\n",
        "TA": "✅ *முடிந்த வேலைகள்:*\n\n",
        "HI": "✅ *पूर्ण नौकरियाँ:*\n\n",
    },
    "my_farmers_completed_footer": {
        "EN": "Reply RATE [job_id] [1-5] to rate a farmer.\nExample: RATE 12 5",
        "TA": "RATE [job_id] [1-5] அனுப்பி விவசாயியை மதிப்பிடவும்.\nஉதாரணம்: RATE 12 5",
        "HI": "RATE [job_id] [1-5] भेजकर किसान को रेट करें।\nउदाहरण: RATE 12 5",
    },
    "job_history_empty": {
        "EN": "No past jobs yet.\nReply VIEW JOBS to find work.",
        "TA": "இதுவரை வேலை வரலாறு இல்லை.\nVIEW JOBS அனுப்பி வேலை தேடுங்கள்.",
        "HI": "अभी तक कोई पुरानी नौकरी नहीं।\nVIEW JOBS भेजकर काम खोजें।",
    },
    "job_history_header":    {"EN": "📜 *Your Job History:*\n\n",    "TA": "📜 *உங்கள் வேலை வரலாறு:*\n\n",        "HI": "📜 *आपका नौकरी इतिहास:*\n\n"},
    "job_history_ongoing":   {"EN": "🕓 *Ongoing:*\n\n",             "TA": "🕓 *நடந்துகொண்டிருக்கிறது:*\n\n",      "HI": "🕓 *जारी:*\n\n"},
    "job_history_completed": {"EN": "✅ *Completed:*\n\n",           "TA": "✅ *முடிந்தது:*\n\n",                   "HI": "✅ *पूर्ण:*\n\n"},
    "job_history_cancelled": {"EN": "❌ *Cancelled:*\n\n",           "TA": "❌ *ரத்து செய்யப்பட்டது:*\n\n",       "HI": "❌ *रद्द:*\n\n"},
    "job_history_footer": {
        "EN": "Reply VIEW JOBS to find more work.",
        "TA": "VIEW JOBS அனுப்பி மேலும் வேலைகளை தேடுங்கள்.",
        "HI": "VIEW JOBS भेजकर और नौकरियाँ खोजें।",
    },
    "today_header_farmer": {
        "EN": "📅 *Today for {name}* — {date}\n\n",
        "TA": "📅 *{name} அவர்களுக்கு இன்று* — {date}\n\n",
        "HI": "📅 *{name} के लिए आज* — {date}\n\n",
    },
    "today_header_labourer": {
        "EN": "📅 *Today for {name}* — {date}\n\n",
        "TA": "📅 *{name} அவர்களுக்கு இன்று* — {date}\n\n",
        "HI": "📅 *{name} के लिए आज* — {date}\n\n",
    },
    "today_in_progress": {
        "EN": "🕓 *{count} job(s) in progress:*\n",
        "TA": "🕓 *{count} வேலை நடந்துகொண்டிருக்கிறது:*\n",
        "HI": "🕓 *{count} नौकरी जारी:*\n",
    },
    "today_pending_rate": {
        "EN": "⭐ *{count} job(s) waiting for your rating:*\n",
        "TA": "⭐ *{count} வேலை உங்கள் மதிப்பீட்டிற்காக காத்திருக்கிறது:*\n",
        "HI": "⭐ *{count} नौकरी आपकी रेटिंग का इंतजार:*\n",
    },
    "today_subsidy_deadlines": {
        "EN": "🏛️ *Subsidy deadlines this week:*\n",
        "TA": "🏛️ *இந்த வாரம் திட்ட கடைசி தேதிகள்:*\n",
        "HI": "🏛️ *इस हफ्ते योजना की अंतिम तारीखें:*\n",
    },
    "today_nothing_farmer": {
        "EN": "✅ Nothing urgent today. Reply POST JOB to find labourers.\n",
        "TA": "✅ இன்று அவசரம் ஒன்றும் இல்லை. POST JOB அனுப்பி தொழிலாளர்களை தேடுங்கள்.\n",
        "HI": "✅ आज कुछ जरूरी नहीं। POST JOB भेजकर मजदूर खोजें।\n",
    },
    "today_open_jobs": {
        "EN": "🔍 *{count} open job(s) near {location}:*\n",
        "TA": "🔍 *{location} அருகில் {count} திறந்த வேலை:*\n",
        "HI": "🔍 *{location} के पास {count} खुली नौकरी:*\n",
    },
    "today_accepted_jobs": {
        "EN": "🕓 *{count} job(s) you've accepted, awaiting JOB DONE from farmer:*\n",
        "TA": "🕓 *நீங்கள் ஏற்றுக்கொண்ட {count} வேலை, விவசாயி JOB DONE குறிக்க காத்திருக்கிறது:*\n",
        "HI": "🕓 *आपने स्वीकार की {count} नौकरी, किसान के JOB DONE का इंतजार:*\n",
    },
    "today_nothing_labourer": {
        "EN": "😔 Nothing nearby right now. We'll notify you when a job is posted.\n",
        "TA": "😔 இப்போது அருகில் எதுவும் இல்லை. வேலை வந்தவுடன் தெரிவிப்போம்.\n",
        "HI": "😔 अभी पास में कुछ नहीं। नौकरी पोस्ट होते ही सूचित करेंगे।\n",
    },
    "my_days_result": {
        "EN": (
            "📊 *MGNREGA-style Day Tracker*\n({fy_start} – {fy_end})\n\n{bar}\n"
            "✅ Days completed via Farm Connect: {days_done}\n"
            "🎯 Remaining toward 100-day entitlement: {days_left}\n\n"
            "ℹ️ This counts your *completed Farm Connect jobs* this financial year as a rough guide — "
            "it does not include MGNREGA work done outside the app. Your official day count is on your Job Card at the Gram Panchayat.\n\n"
            "Reply SUBSIDY for the MGNREGA scheme number to see full details."
        ),
        "TA": (
            "📊 *MGNREGA-நாள் கணக்கு*\n({fy_start} – {fy_end})\n\n{bar}\n"
            "✅ Farm Connect மூலம் முடித்த நாட்கள்: {days_done}\n"
            "🎯 100 நாட்களுக்கு இன்னும் தேவை: {days_left}\n\n"
            "ℹ️ இது இந்த நிதியாண்டில் Farm Connect-ல் *முடிந்த வேலைகளை* மட்டும் எண்ணுகிறது — "
            "வெளியே செய்த MGNREGA வேலைகள் இதில் சேராது. அதிகாரபூர்வ எண்ணிக்கை கிராம பஞ்சாயத்தில் உங்கள் Job Card-ல் இருக்கும்.\n\n"
            "MGNREGA திட்ட விவரத்திற்கு SUBSIDY அனுப்பவும்."
        ),
        "HI": (
            "📊 *MGNREGA-शैली दिन ट्रैकर*\n({fy_start} – {fy_end})\n\n{bar}\n"
            "✅ Farm Connect से पूर्ण दिन: {days_done}\n"
            "🎯 100-दिन के अधिकार में शेष: {days_left}\n\n"
            "ℹ️ यह इस वित्तीय वर्ष में Farm Connect पर *पूर्ण नौकरियों* की गिनती है — "
            "ऐप के बाहर किए गए MGNREGA काम इसमें शामिल नहीं हैं। आधिकारिक गिनती ग्राम पंचायत में आपके Job Card पर होगी।\n\n"
            "MGNREGA योजना विवरण के लिए SUBSIDY भेजें।"
        ),
    },
    "profile_farmer": {
        "EN": (
            "🪪 *My Profile*\n\n👤 Name: {name}\n🧾 Role: Farmer\n📍 Location: {location}\n"
            "⭐ Rating: {rating_str}\n📋 Total jobs posted: {total_posted}\n🌐 Language: {lang_label}\n"
            "{penalty_line}\nReply POST JOB to post a new job."
        ),
        "TA": (
            "🪪 *என் சுயவிவரம்*\n\n👤 பெயர்: {name}\n🧾 பாத்திரம்: விவசாயி\n📍 இடம்: {location}\n"
            "⭐ மதிப்பீடு: {rating_str}\n📋 மொத்தம் போட்ட வேலைகள்: {total_posted}\n🌐 மொழி: {lang_label}\n"
            "{penalty_line}\nPOST JOB அனுப்பி புதிய வேலை போடுங்கள்."
        ),
        "HI": (
            "🪪 *मेरी प्रोफ़ाइल*\n\n👤 नाम: {name}\n🧾 भूमिका: किसान\n📍 स्थान: {location}\n"
            "⭐ रेटिंग: {rating_str}\n📋 कुल पोस्ट नौकरियाँ: {total_posted}\n🌐 भाषा: {lang_label}\n"
            "{penalty_line}\nPOST JOB भेजकर नई नौकरी पोस्ट करें।"
        ),
    },
    "profile_labourer": {
        "EN": (
            "🪪 *My Profile*\n\n👤 Name: {name}\n🧾 Role: Labourer\n📍 Location: {location}\n"
            "🛠️ Skill: {skill}\n⭐ Rating: {rating_str}\n✅ Total jobs completed: {total_done}\n"
            "🌐 Language: {lang_label}\n{no_show_line}{penalty_line}\nReply VIEW JOBS to find more work."
        ),
        "TA": (
            "🪪 *என் சுயவிவரம்*\n\n👤 பெயர்: {name}\n🧾 பாத்திரம்: தொழிலாளர்\n📍 இடம்: {location}\n"
            "🛠️ திறன்: {skill}\n⭐ மதிப்பீடு: {rating_str}\n✅ மொத்தம் முடித்த வேலைகள்: {total_done}\n"
            "🌐 மொழி: {lang_label}\n{no_show_line}{penalty_line}\nVIEW JOBS அனுப்பி மேலும் வேலைகளை தேடுங்கள்."
        ),
        "HI": (
            "🪪 *मेरी प्रोफ़ाइल*\n\n👤 नाम: {name}\n🧾 भूमिका: मजदूर\n📍 स्थान: {location}\n"
            "🛠️ कौशल: {skill}\n⭐ रेटिंग: {rating_str}\n✅ कुल पूर्ण नौकरियाँ: {total_done}\n"
            "🌐 भाषा: {lang_label}\n{no_show_line}{penalty_line}\nVIEW JOBS भेजकर और नौकरियाँ खोजें।"
        ),
    },
    "profile_rating_str": {
        "EN": "{rating}⭐ ({count} rating{s})",
        "TA": "{rating}⭐ ({count} மதிப்பீடு{s})",
        "HI": "{rating}⭐ ({count} रेटिंग{s})",
    },
    "profile_no_rating":      {"EN": "No ratings yet",                         "TA": "இன்னும் மதிப்பீடு இல்லை",                               "HI": "अभी कोई रेटिंग नहीं"},
    "profile_skill_not_set":  {"EN": "Not set — reply UPDATE SKILL to set it", "TA": "அமைக்கவில்லை — UPDATE SKILL அனுப்பி அமைக்கவும்",        "HI": "सेट नहीं — UPDATE SKILL भेजकर सेट करें"},
    "profile_penalty_line":   {"EN": "⚠️ Penalty owed: ₹{amount}\n",          "TA": "⚠️ அபராதம் நிலுவை: ₹{amount}\n",                         "HI": "⚠️ जुर्माना बाकी: ₹{amount}\n"},
    "profile_no_show_line":   {"EN": "⚠️ No-shows reported: {count}\n",       "TA": "⚠️ வரவில்லை என்று புகார்: {count}\n",                    "HI": "⚠️ नो-शो रिपोर्ट: {count}\n"},
    "subsidies_header":       {"EN": "🏛️ *Active Government Schemes:*\n\n",   "TA": "🏛️ *செயல்பாட்டில் உள்ள அரசு திட்டங்கள்:*\n\n",         "HI": "🏛️ *सक्रिय सरकारी योजनाएँ:*\n\n"},
    "subsidies_none":         {"EN": "No schemes are currently open for application.\n\n", "TA": "தற்போது விண்ணப்பிக்க திட்டங்கள் இல்லை.\n\n", "HI": "अभी कोई योजना आवेदन के लिए खुली नहीं है।\n\n"},
    "subsidies_expired_header": {"EN": "─────────────────────\n❌ *Recently Expired:*\n\n", "TA": "─────────────────────\n❌ *சமீபத்தில் காலாவதியானது:*\n\n", "HI": "─────────────────────\n❌ *हाल ही में समाप्त:*\n\n"},
    "subsidies_footer": {
        "EN": "\nReply SUBSIDY [number] for full details.\nExample: SUBSIDY 1",
        "TA": "\nSUBSIDY [எண்] அனுப்பி முழு விவரம் பெறுங்கள்.\nஉதாரணம்: SUBSIDY 1",
        "HI": "\nSUBSIDY [नंबर] भेजकर पूरी जानकारी पाएँ।\nउदाहरण: SUBSIDY 1",
    },
    "subsidies_no_schemes": {
        "EN": "No government schemes are available right now. Check back later!",
        "TA": "இப்போது அரசு திட்டங்கள் எதுவும் இல்லை. பின்னர் சரிபாருங்கள்!",
        "HI": "अभी कोई सरकारी योजना उपलब्ध नहीं। बाद में जाँचें!",
    },
    "subsidy_expired_label": {"EN": "❌ Expired", "TA": "❌ காலாவதியானது", "HI": "❌ समाप्त"},
    "subsidy_invalid_number": {
        "EN": "❌ Invalid number. Reply SUBSIDIES to see the list (1–{count}).",
        "TA": "❌ தவறான எண். பட்டியலை காண SUBSIDIES அனுப்பவும் (1–{count}).",
        "HI": "❌ अमान्य नंबर। सूची देखने के लिए SUBSIDIES भेजें (1–{count})।",
    },
    "subsidy_detail_active": {
        "EN": "🏛️ *{name}*\n{tag}\n{deadline_line}\n\n📋 *Eligibility:*\n{eligibility}\n\n💰 *Benefit:*\n{benefit}\n\n📝 *How to Apply:*\n{how_to_apply}\n\n🔗 *Apply:* {link}\n\nReply SUBSIDIES to see the full list.",
        "TA": "🏛️ *{name}*\n{tag}\n{deadline_line}\n\n📋 *தகுதி:*\n{eligibility}\n\n💰 *பலன்:*\n{benefit}\n\n📝 *விண்ணப்பிக்கும் முறை:*\n{how_to_apply}\n\n🔗 *விண்ணப்பிக்கவும்:* {link}\n\nSUBSIDIES அனுப்பி முழு பட்டியல் பாருங்கள்.",
        "HI": "🏛️ *{name}*\n{tag}\n{deadline_line}\n\n📋 *पात्रता:*\n{eligibility}\n\n💰 *लाभ:*\n{benefit}\n\n📝 *आवेदन कैसे करें:*\n{how_to_apply}\n\n🔗 *आवेदन करें:* {link}\n\nSUBSIDIES भेजकर पूरी सूची देखें।",
    },
    "subsidy_detail_expired": {
        "EN": "🏛️ *{name}*\n❌ Expired (last cycle ended {end_date})\n\n{next_cycle}\n\n📋 *Eligibility:*\n{eligibility}\n\n💰 *Benefit:*\n{benefit}\n\n📝 *How to Apply:*\n{how_to_apply}\n\n🔗 *Apply:* {link}\n\nReply SUBSIDIES to see the full list.",
        "TA": "🏛️ *{name}*\n❌ காலாவதியானது (கடைசி சுழற்சி {end_date} அன்று முடிந்தது)\n\n{next_cycle}\n\n📋 *தகுதி:*\n{eligibility}\n\n💰 *பலன்:*\n{benefit}\n\n📝 *விண்ணப்பிக்கும் முறை:*\n{how_to_apply}\n\n🔗 *விண்ணப்பிக்கவும்:* {link}\n\nSUBSIDIES அனுப்பி முழு பட்டியல் பாருங்கள்.",
        "HI": "🏛️ *{name}*\n❌ समाप्त (पिछला चक्र {end_date} को समाप्त हुआ)\n\n{next_cycle}\n\n📋 *पात्रता:*\n{eligibility}\n\n💰 *लाभ:*\n{benefit}\n\n📝 *आवेदन कैसे करें:*\n{how_to_apply}\n\n🔗 *आवेदन करें:* {link}\n\nSUBSIDIES भेजकर पूरी सूची देखें।",
    },
    "next_cycle_estimate": {
        "EN": "📆 Likely reopens around {est_start} (estimate based on last year's cycle — confirm exact dates on the official portal).",
        "TA": "📆 கிட்டத்தட்ட {est_start} அளவில் மீண்டும் திறக்கும் (கடந்த ஆண்டு சுழற்சி அடிப்படையில் மதிப்பீடு — சரியான தேதிகளை அதிகாரிக்க வலைதளத்தில் சரிபாருங்கள்).",
        "HI": "📆 लगभग {est_start} के आसपास दोबारा खुलेगी (पिछले साल के चक्र के आधार पर अनुमान — सटीक तारीखें आधिकारिक पोर्टल पर देखें)।",
    },
    "no_deadline": {
        "EN": "🟢 No fixed deadline — apply anytime.",
        "TA": "🟢 நிலையான கடைசி தேதி இல்லை — எப்போதும் விண்ணப்பிக்கலாம்.",
        "HI": "🟢 कोई निश्चित अंतिम तारीख नहीं — कभी भी आवेदन करें।",
    },
    "rent_equipment_start": {
        "EN": "🚜 *Let's list your equipment!*\n\nWhat equipment do you want to rent out?\n(e.g. Tractor, Rotavator, Sprayer, Thresher)",
        "TA": "🚜 *உங்கள் உபகரணத்தை பட்டியலிடுவோம்!*\n\nஎந்த உபகரணத்தை வாடகைக்கு கொடுக்க விரும்புகிறீர்கள்?\n(உதா: Tractor, Rotavator, Sprayer, Thresher)",
        "HI": "🚜 *अपना उपकरण सूचीबद्ध करते हैं!*\n\nकौन सा उपकरण किराए पर देना चाहते हैं?\n(जैसे: Tractor, Rotavator, Sprayer, Thresher)",
    },
    "ask_rent_per_day": {
        "EN": "What is the rent per day for your {name}? (in ₹)",
        "TA": "உங்கள் {name}-க்கு நாளொன்றுக்கு வாடகை என்ன? (₹ இல்)",
        "HI": "आपके {name} का प्रति दिन किराया क्या है? (₹ में)",
    },
    "ask_rent_invalid": {
        "EN": "Please enter a valid amount (e.g. 500). What is the rent per day?",
        "TA": "சரியான தொகை உள்ளிடவும் (உதா: 500). நாளொன்றுக்கு வாடகை என்ன?",
        "HI": "कृपया एक सही राशि दर्ज करें (जैसे 500)। प्रति दिन किराया क्या है?",
    },
    "ask_available_until": {
        "EN": "Available until which date?\n(e.g. {example}, Tomorrow, or reply *ongoing* if no end date)",
        "TA": "எந்த தேதி வரை கிடைக்கும்?\n(உதா: {example}, Tomorrow, அல்லது கடைசி தேதி இல்லையெனில் *ongoing* அனுப்பவும்)",
        "HI": "कब तक उपलब्ध है?\n(जैसे: {example}, Tomorrow, या अगर कोई अंत तारीख नहीं तो *ongoing* भेजें)",
    },
    "equipment_listed": {
        "EN": "✅ *Equipment Listed!*\n\n🚜 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📍 Location: {location}\n📅 Available until: {until}\n\nFarmers and labourers nearby can now find your equipment! 🔔",
        "TA": "✅ *உபகரணம் பட்டியலிடப்பட்டது!*\n\n🚜 உபகரணம்: {name}\n💰 வாடகை: ₹{rent}/நாள்\n📍 இடம்: {location}\n📅 வரை கிடைக்கும்: {until}\n\nஅருகிலுள்ள விவசாயிகள் மற்றும் தொழிலாளர்கள் இப்போது காணலாம்! 🔔",
        "HI": "✅ *उपकरण सूचीबद्ध!*\n\n🚜 उपकरण: {name}\n💰 किराया: ₹{rent}/दिन\n📍 स्थान: {location}\n📅 उपलब्ध: {until}\n\nपास के किसान और मजदूर अब इसे देख सकते हैं! 🔔",
    },
    "equipment_list_error": {
        "EN": "⚠️ Error listing your equipment. Please try again by sending RENT EQUIPMENT.",
        "TA": "⚠️ உபகரணத்தை பட்டியலிட பிழை. RENT EQUIPMENT அனுப்பி மீண்டும் முயற்சிக்கவும்.",
        "HI": "⚠️ उपकरण सूचीबद्ध करने में त्रुटि। RENT EQUIPMENT भेजकर पुनः प्रयास करें।",
    },
    "ongoing_label_short": {"EN": "Ongoing", "TA": "தொடர்கிறது", "HI": "जारी"},
    "view_equipment_empty": {
        "EN": "No equipment available for rent in {location} right now. 😔\nCheck back later!",
        "TA": "{location}-ல் இப்போது வாடகைக்கு உபகரணம் இல்லை. 😔\nபின்னர் சரிபாருங்கள்!",
        "HI": "{location} में अभी किराए पर कोई उपकरण नहीं। 😔\nबाद में जाँचें!",
    },
    "view_equipment_header": {
        "EN": "🚜 *Equipment Available in {location}:*\n\n",
        "TA": "🚜 *{location}-ல் வாடகைக்கு உபகரணங்கள்:*\n\n",
        "HI": "🚜 *{location} में किराए पर उपकरण:*\n\n",
    },
    "view_equipment_item": {
        "EN": "{i}. 🔧 {name}\n   💰 ₹{rent}/day\n   📅 Until: {until}\n   ➡️ Reply BOOK EQUIPMENT {eq_id} to book\n\n",
        "TA": "{i}. 🔧 {name}\n   💰 ₹{rent}/நாள்\n   📅 வரை: {until}\n   ➡️ BOOK EQUIPMENT {eq_id} அனுப்பி பதிவு செய்யவும்\n\n",
        "HI": "{i}. 🔧 {name}\n   💰 ₹{rent}/दिन\n   📅 तक: {until}\n   ➡️ BOOK EQUIPMENT {eq_id} भेजकर बुक करें\n\n",
    },
    "book_equipment_unavailable": {
        "EN": "❌ Sorry, {name} is no longer available for rent.",
        "TA": "❌ மன்னிக்கவும், {name} இப்போது வாடகைக்கு கிடைக்கவில்லை.",
        "HI": "❌ माफ करें, {name} अब किराए पर उपलब्ध नहीं है।",
    },
    "book_own_equipment":   {"EN": "❌ You can't book your own equipment.",        "TA": "❌ உங்கள் சொந்த உபகரணத்தை நீங்களே பதிவு செய்ய முடியாது.", "HI": "❌ आप अपना खुद का उपकरण बुक नहीं कर सकते।"},
    "book_equipment_error": {"EN": "❌ Could not complete booking. Please try again.", "TA": "❌ பதிவு முடியவில்லை. மீண்டும் முயற்சிக்கவும்.", "HI": "❌ बुकिंग पूरी नहीं हुई। कृपया पुनः प्रयास करें।"},
    "book_equipment_success": {
        "EN": "✅ *Equipment Booked!*\n\n🚜 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📅 Available until: {until}\n\nThe owner has been notified. They will contact you shortly! 📞",
        "TA": "✅ *உபகரணம் பதிவு செய்யப்பட்டது!*\n\n🚜 உபகரணம்: {name}\n💰 வாடகை: ₹{rent}/நாள்\n📅 வரை கிடைக்கும்: {until}\n\nசொந்தக்காரருக்கு தெரிவிக்கப்பட்டது. விரைவில் தொடர்பு கொள்வார்கள்! 📞",
        "HI": "✅ *उपकरण बुक हुआ!*\n\n🚜 उपकरण: {name}\n💰 किराया: ₹{rent}/दिन\n📅 उपलब्ध: {until}\n\nमालिक को सूचित किया गया। वे जल्द संपर्क करेंगे! 📞",
    },
    "my_equipment_empty": {
        "EN": "You haven't listed any equipment yet.\nReply RENT EQUIPMENT to add one.",
        "TA": "நீங்கள் இன்னும் எந்த உபகரணமும் பட்டியலிடவில்லை.\nRENT EQUIPMENT அனுப்பி சேர்க்கவும்.",
        "HI": "आपने अभी तक कोई उपकरण सूचीबद्ध नहीं किया।\nRENT EQUIPMENT भेजकर जोड़ें।",
    },
    "my_equipment_header":  {"EN": "🚜 *Your Equipment Listings:*\n\n", "TA": "🚜 *உங்கள் உபகரண பட்டியல்:*\n\n",      "HI": "🚜 *आपके उपकरण लिस्टिंग:*\n\n"},
    "my_equipment_footer":  {"EN": "Reply CANCEL EQUIPMENT [id] to remove a listing.", "TA": "CANCEL EQUIPMENT [id] அனுப்பி பட்டியலை நீக்கவும்.", "HI": "CANCEL EQUIPMENT [id] भेजकर लिस्टिंग हटाएँ।"},
    "equip_status_available": {"EN": "✅ Available",  "TA": "✅ கிடைக்கிறது",              "HI": "✅ उपलब्ध"},
    "equip_status_booked":    {"EN": "🔒 Booked",     "TA": "🔒 பதிவு செய்யப்பட்டது",    "HI": "🔒 बुक"},
    "cancel_equipment_not_yours": {"EN": "❌ You can only cancel your own equipment listings.", "TA": "❌ உங்கள் சொந்த உபகரண பட்டியல்களை மட்டுமே ரத்து செய்யலாம்.", "HI": "❌ आप केवल अपनी खुद की उपकरण लिस्टिंग रद्द कर सकते हैं।"},
    "cancel_equipment_error":   {"EN": "❌ Could not cancel listing. Please try again.", "TA": "❌ பட்டியலை ரத்து செய்ய முடியவில்லை. மீண்டும் முயற்சிக்கவும்.", "HI": "❌ लिस्टिंग रद्द नहीं हो सकी। कृपया पुनः प्रयास करें।"},
    "cancel_equipment_success": {
        "EN": "✅ Equipment listing #{eq_id} ({name}) has been cancelled.",
        "TA": "✅ உபகரண பட்டியல் #{eq_id} ({name}) ரத்து செய்யப்பட்டது.",
        "HI": "✅ उपकरण लिस्टिंग #{eq_id} ({name}) रद्द कर दी गई।",
    },
    "rehire_not_farmer":         {"EN": "❌ Only farmers can use REHIRE.",                                    "TA": "❌ விவசாயிகள் மட்டுமே REHIRE பயன்படுத்தலாம்.",                      "HI": "❌ केवल किसान ही REHIRE का उपयोग कर सकते हैं।"},
    "rehire_job_not_found":      {"EN": "❌ Job not found, or it's not one of your jobs.",                   "TA": "❌ வேலை கிடைக்கவில்லை, அல்லது உங்கள் வேலை இல்லை.",              "HI": "❌ नौकरी नहीं मिली, या यह आपकी नौकरी नहीं है।"},
    "rehire_no_labourer":        {"EN": "❌ That job doesn't have a labourer on record to rehire.",          "TA": "❌ அந்த வேலையில் மீண்டும் அழைக்க தொழிலாளர் பதிவு இல்லை.",      "HI": "❌ उस नौकरी में दोबारा बुलाने के लिए कोई मजदूर दर्ज नहीं है।"},
    "rehire_labourer_not_found": {"EN": "❌ Could not find that labourer's profile anymore.",                "TA": "❌ அந்த தொழிலாளரின் சுயவிவரம் கிடைக்கவில்லை.",                  "HI": "❌ उस मजदूर की प्रोफ़ाइल नहीं मिली।"},
    "rehire_start": {
        "EN": "🔁 *Rehire {labourer_name}*\n\nLet's set up the new job. You can change any detail, or reply *SAME* / *KEEP* at each step to reuse the last value.\n\n🔨 Work type (last time: *{work_type}*):",
        "TA": "🔁 *{labourer_name}-ஐ மீண்டும் அழைக்கிறோம்*\n\nபுதிய வேலையை அமைப்போம். எந்த விவரத்தையும் மாற்றலாம், அல்லது முந்தைய மதிப்பை வைக்க *SAME* / *KEEP* அனுப்பவும்.\n\n🔨 வேலை வகை (கடந்த முறை: *{work_type}*):",
        "HI": "🔁 *{labourer_name} को दोबारा बुलाना*\n\nनई नौकरी सेट करते हैं। कोई भी विवरण बदल सकते हैं, या पिछला मान रखने के लिए *SAME* / *KEEP* भेजें।\n\n🔨 काम का प्रकार (पिछली बार: *{work_type}*):",
    },
    "rehire_ask_num": {
        "EN": "👥 Number of labourers (last time: *{count}*)\nReply a number, or SAME to keep it:",
        "TA": "👥 தொழிலாளர்கள் எண்ணிக்கை (கடந்த முறை: *{count}*)\nஒரு எண் அல்லது SAME அனுப்பவும்:",
        "HI": "👥 मजदूरों की संख्या (पिछली बार: *{count}*)\nएक नंबर या SAME भेजें:",
    },
    "rehire_num_invalid":  {"EN": "Please enter a number, or reply SAME to keep the last value.",                         "TA": "ஒரு எண் அல்லது SAME அனுப்பவும்.",            "HI": "एक नंबर या SAME भेजें।"},
    "rehire_ask_wage": {
        "EN": "💰 Wage per day (last time: *₹{wage}/day*)\nReply a new amount, or SAME to keep it:",
        "TA": "💰 நாளொன்றுக்கு கூலி (கடந்த முறை: *₹{wage}/நாள்*)\nபுதிய தொகை அல்லது SAME அனுப்பவும்:",
        "HI": "💰 प्रति दिन मजदूरी (पिछली बार: *₹{wage}/दिन*)\nनई राशि या SAME भेजें:",
    },
    "rehire_wage_invalid": {"EN": "Please enter a valid amount (e.g. 600), or reply SAME to keep the last value.", "TA": "சரியான தொகை (உதா: 600) அல்லது SAME அனுப்பவும்.", "HI": "सही राशि (जैसे 600) या SAME भेजें।"},
    "rehire_ask_date": {
        "EN": "📅 When do you need them? (e.g. {example}, Tomorrow)",
        "TA": "📅 எந்த தேதியில் தேவை? (உதா: {example}, Tomorrow)",
        "HI": "📅 कब चाहिए? (जैसे: {example}, Tomorrow)",
    },
    "rehire_success": {
        "EN": "✅ *Rehire invite sent to {labourer_name}!*\n\n🔨 Work: {work_type}\n👥 Labourers needed: {num_labourers}\n📅 Date: {start_date}\n💰 Wage: ₹{wage}/day\n\nThey'll need to reply CONFIRM {job_id} to accept, just like a normal job.",
        "TA": "✅ *{labourer_name}-க்கு மீண்டும் அழைப்பு அனுப்பப்பட்டது!*\n\n🔨 வேலை: {work_type}\n👥 தேவையான தொழிலாளர்கள்: {num_labourers}\n📅 தேதி: {start_date}\n💰 கூலி: ₹{wage}/நாள்\n\nCONFIRM {job_id} அனுப்பி ஏற்றுக்கொள்ள வேண்டும்.",
        "HI": "✅ *{labourer_name} को दोबारा बुलावा भेजा गया!*\n\n🔨 काम: {work_type}\n👥 मजदूर चाहिए: {num_labourers}\n📅 तारीख: {start_date}\n💰 मजदूरी: ₹{wage}/दिन\n\nCONFIRM {job_id} भेजकर स्वीकार करना होगा।",
    },
    "rehire_error": {
        "EN": "⚠️ Error sending the rehire invite. Please try again with REHIRE [job_id].",
        "TA": "⚠️ மீண்டும் அழைப்பு அனுப்ப பிழை. REHIRE [job_id] மூலம் மீண்டும் முயற்சிக்கவும்.",
        "HI": "⚠️ दोबारा बुलावा भेजने में त्रुटि। REHIRE [job_id] से पुनः प्रयास करें।",
    },
    "voice_not_supported": {
        "EN": "🎙️ We got your voice message!\n\nVoice commands aren't supported yet — that's coming in *Phase 3 (Voice AI)* of Farm Connect. 🚀\n\nFor now, please reply with text. Send HELP to see what you can do.",
        "TA": "🎙️ உங்கள் குரல் செய்தி கிடைத்தது!\n\nகுரல் கட்டளைகள் இன்னும் ஆதரிக்கப்படவில்லை — அது *Phase 3 (Voice AI)*-ல் வரும். 🚀\n\nஇப்போது உரை மூலம் பதில் அனுப்பவும். HELP அனுப்பி என்ன செய்யலாம் என்று பாருங்கள்.",
        "HI": "🎙️ आपका वॉइस मैसेज मिला!\n\nवॉइस कमांड अभी समर्थित नहीं — यह *Phase 3 (Voice AI)* में आएगा। 🚀\n\nअभी टेक्स्ट में जवाब दें। HELP भेजकर देखें क्या-क्या कर सकते हैं।",
    },
    "media_not_supported": {
        "EN": "📎 We received your attachment, but Farm Connect only understands text messages right now.\n\nPlease describe what you need in words, or send HELP for the menu.",
        "TA": "📎 உங்கள் இணைப்பு கிடைத்தது, ஆனால் Farm Connect இப்போது உரை செய்திகளை மட்டுமே புரிந்துகொள்கிறது.\n\nதேவையானதை வார்த்தைகளில் சொல்லுங்கள், அல்லது HELP அனுப்பவும்.",
        "HI": "📎 आपका अटैचमेंट मिला, लेकिन Farm Connect अभी केवल टेक्स्ट संदेश समझता है।\n\nकृपया अपनी जरूरत शब्दों में बताएं, या HELP भेजें।",
    },
    "unknown_command": {
        "EN": "❓ Unknown command. Did you mean *{suggestion}*?\n\n{hint}\n\nSend it exactly as shown to continue.",
        "TA": "❓ தெரியாத கட்டளை. *{suggestion}* என்று சொல்ல விரும்பினீர்களா?\n\n{hint}\n\nதொடர அதை அப்படியே அனுப்பவும்.",
        "HI": "❓ अज्ञात आदेश। क्या आपका मतलब *{suggestion}* था?\n\n{hint}\n\nजारी रखने के लिए इसे ठीक वैसे भेजें।",
    },
    "start_over": {
        "EN": "Something went wrong. Let's start over.\n\nAre you a FARMER or LABOURER?\nReply FARMER or LABOURER to get started.",
        "TA": "ஏதோ தவறு நடந்தது. மீண்டும் தொடங்குவோம்.\n\nநீங்கள் FARMER அல்லது LABOURER?\nFARMER அல்லது LABOURER என்று அனுப்பவும்.",
        "HI": "कुछ गलत हो गया। दोबारा शुरू करते हैं।\n\nक्या आप FARMER हैं या LABOURER?\nFARMER या LABOURER भेजकर शुरू करें।",
    },
    "job_history_farmer_label": {
        "EN": "👨‍🌾 {f_name} | ₹{wage}/day",
        "TA": "👨‍🌾 {f_name} | ₹{wage}/நாள்",
        "HI": "👨‍🌾 {f_name} | ₹{wage}/दिन",
    },
    "no_show_penalty_whatsapp": {
        "EN": (
            "⚠️ *No-Show Reported*\n\n"
            "The farmer for Job #{job_id} ({work_type}) has reported that you did not show up.\n\n"
            "📉 A ₹{amount} penalty has been added to your account and your rating has been reduced by {drop}⭐.\n\n"
            "If you believe this was reported in error, please contact support.\n"
            "Reply VIEW JOBS to find more work."
        ),
        "TA": (
            "⚠️ *வரவில்லை என்று புகார்*\n\n"
            "வேலை #{job_id} ({work_type})-க்கான விவசாயி நீங்கள் வரவில்லை என்று தெரிவித்துள்ளார்.\n\n"
            "📉 ₹{amount} அபராதம் உங்கள் கணக்கில் சேர்க்கப்பட்டு மதிப்பீடு {drop}⭐ குறைக்கப்பட்டது.\n\n"
            "இது தவறான புகார் என்றால் ஆதரவை தொடர்பு கொள்ளுங்கள்.\n"
            "மேலும் வேலைகளுக்கு VIEW JOBS அனுப்பவும்."
        ),
        "HI": (
            "⚠️ *नो-शो रिपोर्ट की गई*\n\n"
            "नौकरी #{job_id} ({work_type}) के किसान ने बताया कि आप नहीं आए।\n\n"
            "📉 ₹{amount} जुर्माना आपके खाते में जोड़ा गया और रेटिंग {drop}⭐ कम की गई।\n\n"
            "अगर यह गलत है तो सहायता से संपर्क करें।\nVIEW JOBS भेजकर काम खोजें।"
        ),
    },
    "cancel_labourer_whatsapp": {
        "EN": (
            "⚠️ *Job Cancelled*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
            "This job has been cancelled by the farmer after you had already accepted it. "
            "Sorry for the inconvenience — the farmer has been penalized for this cancellation."
        ),
        "TA": (
            "⚠️ *வேலை ரத்து செய்யப்பட்டது*\n\n🔨 வேலை: {work_type}\n📍 இடம்: {location}\n📅 தேதி: {start_date}\n\n"
            "நீங்கள் ஏற்றுக்கொண்ட பிறகு விவசாயி இந்த வேலையை ரத்து செய்தார். "
            "தொந்தரவுக்கு மன்னிக்கவும் — விவசாயிக்கு அபராதம் விதிக்கப்பட்டது."
        ),
        "HI": (
            "⚠️ *नौकरी रद्द हुई*\n\n🔨 काम: {work_type}\n📍 स्थान: {location}\n📅 तारीख: {start_date}\n\n"
            "आपके स्वीकार करने के बाद किसान ने यह नौकरी रद्द कर दी। "
            "असुविधा के लिए खेद है — किसान को जुर्माना लगाया गया।"
        ),
    },
    "job_done_labourer_whatsapp": {
        "EN": (
            "✅ *Job Marked as Completed!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
            "The farmer has confirmed this job is done. 🎉\n\n"
            "Please rate the farmer:\nReply RATE {job_id} [1-5]\nExample: RATE {job_id} 5"
        ),
        "TA": (
            "✅ *வேலை முடிந்ததாக குறிக்கப்பட்டது!*\n\n🔨 வேலை: {work_type}\n📍 இடம்: {location}\n📅 தேதி: {start_date}\n\n"
            "விவசாயி இந்த வேலை முடிந்தது என்று உறுதிப்படுத்தினார். 🎉\n\n"
            "விவசாயியை மதிப்பிடவும்:\nRATE {job_id} [1-5] அனுப்பவும்\nஉதாரணம்: RATE {job_id} 5"
        ),
        "HI": (
            "✅ *नौकरी पूर्ण मार्क की गई!*\n\n🔨 काम: {work_type}\n📍 स्थान: {location}\n📅 तारीख: {start_date}\n\n"
            "किसान ने पुष्टि की कि यह काम पूरा हो गया। 🎉\n\n"
            "किसान को रेट करें:\nRATE {job_id} [1-5] भेजें\nउदाहरण: RATE {job_id} 5"
        ),
    },
    "confirm_farmer_whatsapp": {
        "EN": (
            "✅ *Job Confirmed!*\n\n👤 Labourer: {labourer_name}\n🛠️ Skill: {skill}\n"
            "🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
            "Your labourer will arrive on the job date. 🌾\n"
            "Once the work is finished, reply JOB DONE {job_id} to close it out and unlock ratings."
        ),
        "TA": (
            "✅ *வேலை உறுதிப்படுத்தப்பட்டது!*\n\n👤 தொழிலாளர்: {labourer_name}\n🛠️ திறன்: {skill}\n"
            "🔨 வேலை: {work_type}\n📍 இடம்: {location}\n📅 தேதி: {start_date}\n\n"
            "தொழிலாளர் வேலை தேதியில் வருவார். 🌾\n"
            "வேலை முடிந்தால் JOB DONE {job_id} அனுப்பி மூடவும்."
        ),
        "HI": (
            "✅ *नौकरी पक्की हुई!*\n\n👤 मजदूर: {labourer_name}\n🛠️ कौशल: {skill}\n"
            "🔨 काम: {work_type}\n📍 स्थान: {location}\n📅 तारीख: {start_date}\n\n"
            "मजदूर नौकरी की तारीख पर पहुँचेंगे। 🌾\n"
            "काम पूरा होने पर JOB DONE {job_id} भेजें।"
        ),
    },
    "rehire_labourer_whatsapp": {
        "EN": (
            "🔁 *{farmer_name} wants to rehire you!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n"
            "👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n\n"
            "Reply CONFIRM {job_id} to accept this job."
        ),
        "TA": (
            "🔁 *{farmer_name} உங்களை மீண்டும் அழைக்கிறார்!*\n\n🔨 வேலை: {work_type}\n📍 இடம்: {location}\n"
            "👥 தேவையான தொழிலாளர்கள்: {num_labourers}\n💰 கூலி: ₹{wage}/நாள்\n📅 தேதி: {start_date}\n\n"
            "CONFIRM {job_id} அனுப்பி ஏற்றுக்கொள்ளவும்."
        ),
        "HI": (
            "🔁 *{farmer_name} आपको दोबारा बुला रहे हैं!*\n\n🔨 काम: {work_type}\n📍 स्थान: {location}\n"
            "👥 मजदूर चाहिए: {num_labourers}\n💰 मजदूरी: ₹{wage}/दिन\n📅 तारीख: {start_date}\n\n"
            "CONFIRM {job_id} भेजकर स्वीकार करें।"
        ),
    },
    "new_job_notification": {
        "EN": (
            "🔔 *New Job Near You!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n"
            "👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n\n"
            "Reply CONFIRM {job_id} to accept this job."
        ),
        "TA": (
            "🔔 *அருகில் புதிய வேலை!*\n\n🔨 வேலை: {work_type}\n📍 இடம்: {location}\n"
            "👥 தேவையான தொழிலாளர்கள்: {num_labourers}\n💰 கூலி: ₹{wage}/நாள்\n📅 தேதி: {start_date}\n\n"
            "CONFIRM {job_id} அனுப்பி ஏற்றுக்கொள்ளவும்."
        ),
        "HI": (
            "🔔 *पास में नई नौकरी!*\n\n🔨 काम: {work_type}\n📍 स्थान: {location}\n"
            "👥 मजदूर चाहिए: {num_labourers}\n💰 मजदूरी: ₹{wage}/दिन\n📅 तारीख: {start_date}\n\n"
            "CONFIRM {job_id} भेजकर स्वीकार करें।"
        ),
    },
    "new_equipment_notification": {
        "EN": (
            "🚜 Equipment Available for Rent Near You in {location}!\n\n"
            "🔧 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📅 Available until: {until}\n\n"
            "Reply VIEW EQUIPMENT to see all listings."
        ),
        "TA": (
            "🚜 {location}-ல் உங்களுக்கு அருகில் வாடகைக்கு உபகரணம்!\n\n"
            "🔧 உபகரணம்: {name}\n💰 வாடகை: ₹{rent}/நாள்\n📅 வரை கிடைக்கும்: {until}\n\n"
            "VIEW EQUIPMENT அனுப்பி அனைத்தையும் பாருங்கள்."
        ),
        "HI": (
            "🚜 {location} में आपके पास किराए पर उपकरण!\n\n"
            "🔧 उपकरण: {name}\n💰 किराया: ₹{rent}/दिन\n📅 उपलब्ध: {until}\n\n"
            "VIEW EQUIPMENT भेजकर सभी लिस्टिंग देखें।"
        ),
    },
    "equip_booking_owner_whatsapp": {
        "EN": (
            "🔔 *Equipment Booking Confirmed!*\n\n🚜 Equipment: {name}\n👤 Booked by: {user_name}\n"
            "📞 Contact: {phone}\n💰 Rent: ₹{rent}/day\n\nPlease coordinate with them for pickup/delivery."
        ),
        "TA": (
            "🔔 *உபகரண பதிவு உறுதிப்படுத்தப்பட்டது!*\n\n🚜 உபகரணம்: {name}\n👤 பதிவு செய்தவர்: {user_name}\n"
            "📞 தொடர்பு: {phone}\n💰 வாடகை: ₹{rent}/நாள்\n\nபிக்அப்/டெலிவரிக்கு அவர்களை தொடர்புகொள்ளவும்."
        ),
        "HI": (
            "🔔 *उपकरण बुकिंग पक्की!*\n\n🚜 उपकरण: {name}\n👤 बुक करने वाले: {user_name}\n"
            "📞 संपर्क: {phone}\n💰 किराया: ₹{rent}/दिन\n\nपिकअप/डिलीवरी के लिए उनसे संपर्क करें।"
        ),
    },
    "equip_cancel_booker_whatsapp": {
        "EN": "⚠️ *Equipment Booking Cancelled*\n\n🚜 Equipment: {name}\n📍 Location: {location}\n\nThe owner has cancelled this listing. Sorry for the inconvenience.",
        "TA": "⚠️ *உபகரண பதிவு ரத்து செய்யப்பட்டது*\n\n🚜 உபகரணம்: {name}\n📍 இடம்: {location}\n\nசொந்தக்காரர் இந்த பட்டியலை ரத்து செய்தார். தொந்தரவுக்கு மன்னிக்கவும்.",
        "HI": "⚠️ *उपकरण बुकिंग रद्द हुई*\n\n🚜 उपकरण: {name}\n📍 स्थान: {location}\n\nमालिक ने यह लिस्टिंग रद्द कर दी। असुविधा के लिए खेद है।",
    },
}


def t(key: str, phone: str, **kwargs) -> str:
    """Look up a translation key for the user's language, with EN fallback."""
    lang = get_lang(phone)
    translations = T.get(key, {})
    text = translations.get(lang) or translations.get("EN", f"[{key}]")
    return text.format(**kwargs) if kwargs else text
