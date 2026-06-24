import os
import re
import hashlib
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
from deep_translator import GoogleTranslator

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
LANG_PREFS: dict[str, str] = {}

SUPPORTED_LANGS = {
    "1": "TA", "TAMIL": "TA", "தமிழ்": "TA",
    "2": "EN", "ENGLISH": "EN",
    "3": "HI", "HINDI": "HI", "हिंदी": "HI",
}

def get_lang(phone: str) -> str:
    return LANG_PREFS.get(phone, "EN")

def set_lang(phone: str, lang: str):
    LANG_PREFS[phone] = lang

# ── Translation: flat English strings + API-based tr() ───────────────────────
T = {
    "welcome_new": (
        "🌾 *Welcome to Farm Connect!*\n\n"
        "Connecting farmers and labourers across Tamil Nadu.\n\n"
        "Are you a *FARMER* or *LABOURER*?\n"
        "Reply FARMER or LABOURER to get started.\n\n"
        "🌐 Reply LANGUAGE to choose Tamil / Hindi / English."
    ),
    "ask_name": "Great! What is your name?",
    "ask_location": (
        "Nice to meet you, {name}! 🙏\n\n"
        "What is your village or town name?\n"
        "(We\'ll also notify you about jobs/equipment in nearby areas, not just an exact match.)"
    ),
    "registered_farmer": "✅ *Registered as Farmer!*\n\n👤 Name: {name}\n📍 Location: {location}\n\nReply POST JOB to post your first job! 🌾",
    "registered_labourer": "✅ *Registered as Labourer!*\n\n👤 Name: {name}\n📍 Location: {location}\n🛠️ Skill: {skill}\n\nReply VIEW JOBS to see available jobs near you! 💪",
    "language_prompt": (
        "🌐 *Choose Your Language / மொழி தேர்வு / भाषा चुनें*\n\n"
        "1️⃣  தமிழ் (Tamil)\n2️⃣  English\n3️⃣  हिंदी (Hindi)\n\nReply 1, 2, or 3."
    ),
    "language_set": "✅ Language set to *English*. All messages will now be in English.",
    "language_invalid": "❓ Please reply 1 (Tamil), 2 (English), or 3 (Hindi).",
    "register_first": "❌ Please register first. Reply HI to get started.",
    "error_saving": "⚠️ Error saving your details. Please try again.",
    "farmers_only": "❌ Only registered farmers can use this command.",
    "labourers_only": "❌ Only registered labourers can use this command.",
    "reply_farmer_or_labourer": "Please reply with FARMER or LABOURER only.",
    "already_registered_as": "⚠️ This number is already registered as a *{role}* ({name}).\n\nA phone number can only be registered under one role.\n\n{menu}",
    "skill_invalid": "Please reply with a number 1-6 or skill name.\n\n{prompt}",
    "skill_updated": "✅ Your skill has been updated to *{skill}*.\n\nReply VIEW JOBS to see work near you.",
    "skill_update_error": "⚠️ Error updating your skill. Please try again by sending UPDATE SKILL.",
    "update_skill_prompt": "🛠️ Your current skill: *{skill}*\n\n{prompt}",
    "db_fetch_error": "❌ Could not fetch data. Please try again.",
    "job_not_found": "❌ Job not found or you don\'t own this job.",
    "post_job_start": "📋 *Let\'s post your job!*\n\nWhat type of work is needed?\n(e.g. Harvesting, Planting, Irrigation, Weeding)",
    "ask_num_labourers": "How many labourers do you need?",
    "ask_num_labourers_invalid": "Please enter a number. How many labourers do you need?",
    "ask_wage": "What is the wage per day? (in ₹)",
    "ask_wage_with_avg": "What is the wage per day? (in ₹)\n\n💡 Average for {work_type} near {location} is ₹{avg}/day (based on completed jobs).",
    "ask_wage_invalid": "Please enter a valid amount (e.g. 600). What is the wage per day?",
    "wage_below_avg": "⚠️ That\'s noticeably below the area average of ₹{avg}/day for this work — you may get fewer responses.\n\n{date_prompt}",
    "ask_date": "When do you need them? (e.g. {example}, Tomorrow)",
    "job_posted": "✅ *Job Posted Successfully!*\n\n📍 Location: {location}\n🔨 Work: {work_type}\n👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n{weather_line}\nNotifying nearby labourers now! 🔔",
    "job_post_error": "⚠️ Error posting your job. Please try again by sending POST JOB.",
    "farmer_profile_not_found": "❌ Could not find your farmer profile. Please try again.",
    "my_jobs_empty": "You haven\'t posted any jobs yet.\nReply POST JOB to post one.",
    "my_jobs_header": "📋 *Your Recent Jobs:*\n\n",
    "my_jobs_footer": "Reply CANCEL [ID] to cancel a job, or JOB DONE [ID] once work is complete.",
    "status_open":      "OPEN",
    "status_confirmed": "CONFIRMED",
    "status_completed": "COMPLETED",
    "status_cancelled": "CANCELLED",
    "no_jobs_nearby": "No open jobs near {location} right now. 😔\n\nWe\'ll notify you the moment a new job is posted nearby! 🔔",
    "view_jobs_header": "🔍 *Open Jobs Near {location}:*\n\n{rating_line}",
    "rating_line": "⭐ Your rating: {rating}⭐ ({count} rating{s})\n\n",
    "no_rating_yet": "⭐ Your rating: No ratings yet\n\n",
    "view_jobs_item": "{i}. 🔨 {work_type}\n   📍 Location: {location}\n   👥 {num_labourers} needed | ₹{wage}/day\n   📅 {start_date}\n   ➡️ Reply CONFIRM {job_id} to accept\n\n",
    "job_confirmed_labourer": "✅ *Job Confirmed!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n💰 Wage: ₹{wage}/day\n\nPlease arrive on time. Good luck! 💪",
    "confirm_not_labourer": "❌ Only registered labourers can confirm jobs.",
    "confirm_own_job": "❌ You can\'t confirm your own posted job.",
    "confirm_already_taken": "❌ Job not found or already confirmed.",
    "job_done_not_farmer": "❌ Only the farmer who posted the job can mark it as done.",
    "job_done_not_found": "❌ Job not found, not yours, or not in an accepted state.",
    "job_done_success": "✅ *Job #{job_id} marked as completed!*\n\n🔨 Work: {work_type}\n👤 Labourer: {labourer_name}\n\nPlease rate the labourer:\nReply RATE {job_id} [1-5]\nExample: RATE {job_id} 5",
    "rate_invalid_format": "❓ Couldn\'t read that.\n\nFormat: RATE [job_id] [stars 1–5]\nExample: RATE 12 5",
    "rate_stars_range": "Stars must be between 1 and 5.",
    "rate_job_not_completed": "❌ You can only rate jobs after the farmer marks them as JOB DONE.",
    "rate_already_rated": "You\'ve already rated this job.",
    "rate_no_labourer": "❌ No labourer assigned to this job.",
    "rate_no_farmer": "❌ No farmer found for this job.",
    "rate_person_not_found": "❌ {role} not found.",
    "rate_success": "✅ Rated {name} — {stars}\nTheir new rating: {new_rating}⭐ ({total} total ratings)",
    "rate_not_your_job": "❌ Job not found or doesn\'t belong to you.",
    "cancel_success": "✅ Job #{job_id} has been cancelled.",
    "cancel_penalty_line": "\n📉 Since a labourer had already accepted this job, a ₹{amount} penalty and a {rating_drop}⭐ rating drop have been applied to your account.",
    "no_show_not_farmer": "❌ Only the farmer who posted the job can report a no-show.",
    "no_show_not_found": "❌ Job not found, not yours, or not in an accepted (confirmed) state.",
    "no_show_no_labourer": "✅ Job #{job_id} marked cancelled, but no labourer was on record to flag.",
    "no_show_success": "✅ Reported. Job #{job_id} has been cancelled and {labourer_name} has been flagged for not showing up.\n\n📉 A ₹{amount} penalty and a {rating_drop}⭐ rating drop have been applied to their account.\n\nReply POST JOB to re-post this work.",
    "my_labourers_empty": "No accepted jobs found.\nReply POST JOB to post one.",
    "my_labourers_pending_header": "👥 *Accepted — Not Yet Completed:*\n\n",
    "my_labourers_pending_footer": "Reply JOB DONE [job_id] once the work is finished.\nExample: JOB DONE 12\nIf they didn\'t show up, reply NO SHOW [job_id] instead.\n\n",
    "my_labourers_completed_header": "✅ *Completed Jobs:*\n\n",
    "my_labourers_completed_footer": "Reply RATE [job_id] [1-5] to rate a labourer.\nExample: RATE 12 5\nWorked well with someone? Reply REHIRE [job_id] to invite them again.",
    "in_progress":      "🕓 In progress",
    "rated_label":      "✅ Rated",
    "not_rated_label":  "⭐ Not rated yet",
    "no_show_flag":     " ⚠️ {count} past no-show(s)",
    "unknown_name":     "Unknown",
    "my_farmers_empty": "No accepted jobs found.\nReply VIEW JOBS to find work.",
    "my_farmers_pending_header": "👨\u200d🌾 *Accepted — Not Yet Completed:*\n\n",
    "my_farmers_pending_waiting": "🕓 Waiting for farmer to mark JOB DONE",
    "my_farmers_completed_header": "✅ *Completed Jobs:*\n\n",
    "my_farmers_completed_footer": "Reply RATE [job_id] [1-5] to rate a farmer.\nExample: RATE 12 5",
    "job_history_empty": "No past jobs yet.\nReply VIEW JOBS to find work.",
    "job_history_header":    "📜 *Your Job History:*\n\n",
    "job_history_ongoing":   "🕓 *Ongoing:*\n\n",
    "job_history_completed": "✅ *Completed:*\n\n",
    "job_history_cancelled": "❌ *Cancelled:*\n\n",
    "job_history_footer": "Reply VIEW JOBS to find more work.",
    "job_history_farmer_label": "👨\u200d🌾 {f_name} | ₹{wage}/day",
    "today_header_farmer": "📅 *Today for {name}* — {date}\n\n",
    "today_header_labourer": "📅 *Today for {name}* — {date}\n\n",
    "today_in_progress": "🕓 *{count} job(s) in progress:*\n",
    "today_pending_rate": "⭐ *{count} job(s) waiting for your rating:*\n",
    "today_subsidy_deadlines": "🏛️ *Subsidy deadlines this week:*\n",
    "today_nothing_farmer": "✅ Nothing urgent today. Reply POST JOB to find labourers.\n",
    "today_open_jobs": "🔍 *{count} open job(s) near {location}:*\n",
    "today_accepted_jobs": "🕓 *{count} job(s) you\'ve accepted, awaiting JOB DONE from farmer:*\n",
    "today_nothing_labourer": "😔 Nothing nearby right now. We\'ll notify you when a job is posted.\n",
    "my_days_result": (
        "📊 *MGNREGA-style Day Tracker*\n({fy_start} – {fy_end})\n\n{bar}\n"
        "✅ Days completed via Farm Connect: {days_done}\n"
        "🎯 Remaining toward 100-day entitlement: {days_left}\n\n"
        "ℹ️ This counts your *completed Farm Connect jobs* this financial year as a rough guide — "
        "it does not include MGNREGA work done outside the app. Your official day count is on your Job Card at the Gram Panchayat.\n\n"
        "Reply SUBSIDY for the MGNREGA scheme number to see full details."
    ),
    "profile_farmer": (
        "🪪 *My Profile*\n\n👤 Name: {name}\n🧾 Role: Farmer\n📍 Location: {location}\n"
        "⭐ Rating: {rating_str}\n📋 Total jobs posted: {total_posted}\n🌐 Language: {lang_label}\n"
        "{penalty_line}\nReply POST JOB to post a new job."
    ),
    "profile_labourer": (
        "🪪 *My Profile*\n\n👤 Name: {name}\n🧾 Role: Labourer\n📍 Location: {location}\n"
        "🛠️ Skill: {skill}\n⭐ Rating: {rating_str}\n✅ Total jobs completed: {total_done}\n"
        "🌐 Language: {lang_label}\n{no_show_line}{penalty_line}\nReply VIEW JOBS to find more work."
    ),
    "profile_rating_str":    "{rating}⭐ ({count} rating{s})",
    "profile_no_rating":     "No ratings yet",
    "profile_skill_not_set": "Not set — reply UPDATE SKILL to set it",
    "profile_penalty_line":  "⚠️ Penalty owed: ₹{amount}\n",
    "profile_no_show_line":  "⚠️ No-shows reported: {count}\n",
    "subsidies_header":      "🏛️ *Active Government Schemes:*\n\n",
    "subsidies_none":        "No schemes are currently open for application.\n\n",
    "subsidies_expired_header": "─────────────────────\n❌ *Recently Expired:*\n\n",
    "subsidies_footer": "\nReply SUBSIDY [number] for full details.\nExample: SUBSIDY 1",
    "subsidies_no_schemes": "No government schemes are available right now. Check back later!",
    "subsidy_expired_label": "❌ Expired",
    "subsidy_invalid_number": "❌ Invalid number. Reply SUBSIDIES to see the list (1–{count}).",
    "subsidy_detail_active": "🏛️ *{name}*\n{tag}\n{deadline_line}\n\n📋 *Eligibility:*\n{eligibility}\n\n💰 *Benefit:*\n{benefit}\n\n📝 *How to Apply:*\n{how_to_apply}\n\n🔗 *Apply:* {link}\n\nReply SUBSIDIES to see the full list.",
    "subsidy_detail_expired": "🏛️ *{name}*\n❌ Expired (last cycle ended {end_date})\n\n{next_cycle}\n\n📋 *Eligibility:*\n{eligibility}\n\n💰 *Benefit:*\n{benefit}\n\n📝 *How to Apply:*\n{how_to_apply}\n\n🔗 *Apply:* {link}\n\nReply SUBSIDIES to see the full list.",
    "next_cycle_estimate": "📆 Likely reopens around {est_start} (estimate based on last year\'s cycle — confirm exact dates on the official portal).",
    "no_deadline": "🟢 No fixed deadline — apply anytime.",
    "rent_equipment_start": "🚜 *Let\'s list your equipment!*\n\nWhat equipment do you want to rent out?\n(e.g. Tractor, Rotavator, Sprayer, Thresher)",
    "ask_rent_per_day": "What is the rent per day for your {name}? (in ₹)",
    "ask_rent_invalid": "Please enter a valid amount (e.g. 500). What is the rent per day?",
    "ask_available_until": "Available until which date?\n(e.g. {example}, Tomorrow, or reply *ongoing* if no end date)",
    "equipment_listed": "✅ *Equipment Listed!*\n\n🚜 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📍 Location: {location}\n📅 Available until: {until}\n\nFarmers and labourers nearby can now find your equipment! 🔔",
    "equipment_list_error": "⚠️ Error listing your equipment. Please try again by sending RENT EQUIPMENT.",
    "ongoing_label_short": "Ongoing",
    "view_equipment_empty": "No equipment available for rent in {location} right now. 😔\nCheck back later!",
    "view_equipment_header": "🚜 *Equipment Available in {location}:*\n\n",
    "view_equipment_item": "{i}. 🔧 {name}\n   💰 ₹{rent}/day\n   📅 Until: {until}\n   ➡️ Reply BOOK EQUIPMENT {eq_id} to book\n\n",
    "book_equipment_unavailable": "❌ Sorry, {name} is no longer available for rent.",
    "book_own_equipment":   "❌ You can\'t book your own equipment.",
    "book_equipment_error": "❌ Could not complete booking. Please try again.",
    "book_equipment_success": "✅ *Equipment Booked!*\n\n🚜 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📅 Available until: {until}\n\nThe owner has been notified. They will contact you shortly! 📞",
    "my_equipment_empty": "You haven\'t listed any equipment yet.\nReply RENT EQUIPMENT to add one.",
    "my_equipment_header":  "🚜 *Your Equipment Listings:*\n\n",
    "my_equipment_footer":  "Reply CANCEL EQUIPMENT [id] to remove a listing.",
    "equip_status_available": "✅ Available",
    "equip_status_booked":    "🔒 Booked",
    "cancel_equipment_not_yours": "❌ You can only cancel your own equipment listings.",
    "cancel_equipment_error":     "❌ Could not cancel listing. Please try again.",
    "cancel_equipment_success": "✅ Equipment listing #{eq_id} ({name}) has been cancelled.",
    "rehire_not_farmer":          "❌ Only farmers can use REHIRE.",
    "rehire_job_not_found":       "❌ Job not found, or it\'s not one of your jobs.",
    "rehire_no_labourer":         "❌ That job doesn\'t have a labourer on record to rehire.",
    "rehire_labourer_not_found":  "❌ Could not find that labourer\'s profile anymore.",
    "rehire_start": "🔁 *Rehire {labourer_name}*\n\nLet\'s set up the new job. You can change any detail, or reply *SAME* / *KEEP* at each step to reuse the last value.\n\n🔨 Work type (last time: *{work_type}*):",
    "rehire_ask_num": "👥 Number of labourers (last time: *{count}*)\nReply a number, or SAME to keep it:",
    "rehire_num_invalid":  "Please enter a number, or reply SAME to keep the last value.",
    "rehire_ask_wage": "💰 Wage per day (last time: *₹{wage}/day*)\nReply a new amount, or SAME to keep it:",
    "rehire_wage_invalid": "Please enter a valid amount (e.g. 600), or reply SAME to keep the last value.",
    "rehire_ask_date": "📅 When do you need them? (e.g. {example}, Tomorrow)",
    "rehire_success": "✅ *Rehire invite sent to {labourer_name}!*\n\n🔨 Work: {work_type}\n👥 Labourers needed: {num_labourers}\n📅 Date: {start_date}\n💰 Wage: ₹{wage}/day\n\nThey\'ll need to reply CONFIRM {job_id} to accept, just like a normal job.",
    "rehire_error": "⚠️ Error sending the rehire invite. Please try again with REHIRE [job_id].",
    "voice_not_supported": "🎙️ We got your voice message!\n\nVoice commands aren\'t supported yet — that\'s coming in *Phase 3 (Voice AI)* of Farm Connect. 🚀\n\nFor now, please reply with text. Send HELP to see what you can do.",
    "media_not_supported": "📎 We received your attachment, but Farm Connect only understands text messages right now.\n\nPlease describe what you need in words, or send HELP for the menu.",
    "unknown_command": "❓ Unknown command. Did you mean *{suggestion}*?\n\n{hint}\n\nSend it exactly as shown to continue.",
    "start_over": "Something went wrong. Let\'s start over.\n\nAre you a FARMER or LABOURER?\nReply FARMER or LABOURER to get started.",
    # WhatsApp notification strings
    "no_show_penalty_whatsapp": (
        "⚠️ *No-Show Reported*\n\n"
        "The farmer for Job #{job_id} ({work_type}) has reported that you did not show up.\n\n"
        "📉 A ₹{amount} penalty has been added to your account and your rating has been reduced by {drop}⭐.\n\n"
        "If you believe this was reported in error, please contact support.\n"
        "Reply VIEW JOBS to find more work."
    ),
    "cancel_labourer_whatsapp": (
        "⚠️ *Job Cancelled*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
        "This job has been cancelled by the farmer after you had already accepted it. "
        "Sorry for the inconvenience — the farmer has been penalized for this cancellation."
    ),
    "job_done_labourer_whatsapp": (
        "✅ *Job Marked as Completed!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
        "The farmer has confirmed this job is done. 🎉\n\n"
        "Please rate the farmer:\nReply RATE {job_id} [1-5]\nExample: RATE {job_id} 5"
    ),
    "confirm_farmer_whatsapp": (
        "✅ *Job Confirmed!*\n\n👤 Labourer: {labourer_name}\n🛠️ Skill: {skill}\n"
        "🔨 Work: {work_type}\n📍 Location: {location}\n📅 Date: {start_date}\n\n"
        "Your labourer will arrive on the job date. 🌾\n"
        "Once the work is finished, reply JOB DONE {job_id} to close it out and unlock ratings."
    ),
    "rehire_labourer_whatsapp": (
        "🔁 *{farmer_name} wants to rehire you!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n"
        "👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n\n"
        "Reply CONFIRM {job_id} to accept this job."
    ),
    "new_job_notification": (
        "🔔 *New Job Near You!*\n\n🔨 Work: {work_type}\n📍 Location: {location}\n"
        "👥 Labourers needed: {num_labourers}\n💰 Wage: ₹{wage}/day\n📅 Date: {start_date}\n\n"
        "Reply CONFIRM {job_id} to accept this job."
    ),
    "new_equipment_notification": (
        "🚜 Equipment Available for Rent Near You in {location}!\n\n"
        "🔧 Equipment: {name}\n💰 Rent: ₹{rent}/day\n📅 Available until: {until}\n\n"
        "Reply VIEW EQUIPMENT to see all listings."
    ),
    "equip_booking_owner_whatsapp": (
        "🔔 *Equipment Booking Confirmed!*\n\n🚜 Equipment: {name}\n👤 Booked by: {user_name}\n"
        "📞 Contact: {phone}\n💰 Rent: ₹{rent}/day\n\nPlease coordinate with them for pickup/delivery."
    ),
    "equip_cancel_booker_whatsapp": "⚠️ *Equipment Booking Cancelled*\n\n🚜 Equipment: {name}\n📍 Location: {location}\n\nThe owner has cancelled this listing. Sorry for the inconvenience.",
}

# ── In-memory translation cache ───────────────────────────────────────────────
_tr_cache: dict[str, str] = {}

def tr(text: str, lang: str) -> str:
    """Translate text to target language using Google Translate API with caching.
    Protects {placeholder} tokens so they survive translation."""
    if lang == "EN":
        return text
    # Protect {var} placeholders: replace {name} → [0], {location} → [1] etc.
    slots = re.findall(r'\{(\w+)\}', text)
    safe = text
    for i, v in enumerate(slots):
        safe = safe.replace(f'{{{v}}}', f'[{i}]', 1)
    ck = f"{lang}:{hashlib.md5(safe.encode()).hexdigest()}"
    if ck not in _tr_cache:
        code = {"TA": "ta", "HI": "hi"}[lang]
        try:
            result = GoogleTranslator(source="en", target=code).translate(safe)
            _tr_cache[ck] = result or safe
        except Exception as e:
            print(f"[TR] Translation error: {e}")
            _tr_cache[ck] = safe  # silent fallback to English
    out = _tr_cache[ck]
    # Restore placeholders
    for i, v in enumerate(slots):
        out = out.replace(f'[{i}]', f'{{{v}}}')
    return out


def t(key: str, phone: str, **kwargs) -> str:
    """Look up English string by key, translate to user's language, then format."""
    lang = get_lang(phone)
    # language_prompt and language_set are special: they are always shown in
    # all three languages or in the newly selected language, so skip API translation.
    SKIP_TRANSLATE = {"language_prompt", "language_set", "language_invalid"}
    english_text = T.get(key, f"[{key}]")
    if lang == "EN" or key in SKIP_TRANSLATE:
        translated = english_text
    else:
        translated = tr(english_text, lang)
    return translated.format(**kwargs) if kwargs else translated

# ── Greeting keywords ─────────────────────────────────────────────────────────
GREETINGS = {
    "HI", "HELLO", "HEY", "HELP", "START", "MENU",
    "VANAKKAM", "NAMASTE", "HAI", "HELO", "VANAKAM",
    "GOOD MORNING", "GOOD AFTERNOON", "GOOD EVENING",
    "GM", "SUP", "YO", "HOWDY"
}

# ── Skill map ─────────────────────────────────────────────────────────────────
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
    "1️⃣  Harvesting\n2️⃣  Planting\n3️⃣  Irrigation\n4️⃣  Weeding\n"
    "5️⃣  General Labour\n6️⃣  Any Work (No Preference)\n\nReply with the number or skill name."
)
SKILL_PROMPT_TA = (
    "உங்கள் முக்கிய திறன் என்ன?\n\n"
    "1️⃣  அறுவடை (Harvesting)\n2️⃣  நடவு (Planting)\n3️⃣  நீர்ப்பாசனம் (Irrigation)\n"
    "4️⃣  களை எடுத்தல் (Weeding)\n5️⃣  பொது உழைப்பு (General Labour)\n6️⃣  எந்த வேலையும் (Any Work)\n\n"
    "எண் அல்லது திறன் பெயர் அனுப்பவும்."
)
SKILL_PROMPT_HI = (
    "आपका मुख्य कौशल क्या है?\n\n"
    "1️⃣  कटाई (Harvesting)\n2️⃣  रोपाई (Planting)\n3️⃣  सिंचाई (Irrigation)\n"
    "4️⃣  निराई (Weeding)\n5️⃣  सामान्य मजदूरी (General Labour)\n6️⃣  कोई भी काम (Any Work)\n\n"
    "नंबर या कौशल का नाम टाइप करें।"
)

def skill_prompt_for(phone: str) -> str:
    lang = get_lang(phone)
    if lang == "TA": return SKILL_PROMPT_TA
    if lang == "HI": return SKILL_PROMPT_HI
    return SKILL_PROMPT

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
    "SUBSIDIES", "SUBSIDY", "MY PROFILE", "JOB HISTORY", "TODAY", "REHIRE", "MY DAYS",
    "NO SHOW", "HELP", "LANGUAGE",
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
        "JOB HISTORY":      "Just send: JOB HISTORY",
        "TODAY":            "Just send: TODAY",
        "REHIRE":           "Format: REHIRE [job_id]  •  Example: REHIRE 12",
        "MY DAYS":          "Just send: MY DAYS",
        "NO SHOW":          "Format: NO SHOW [job_id]  •  Example: NO SHOW 12",
        "HELP":             "Just send: HELP",
        "LANGUAGE":         "Just send: LANGUAGE",
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

# ── Date parsing / validation ─────────────────────────────────────────────────
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
    t_low = text.strip().lower()
    today = date.today()
    if t_low == "today":    return today
    if t_low == "tomorrow": return today.fromordinal(today.toordinal() + 1)
    if t_low == "next week":return today.fromordinal(today.toordinal() + 7)
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
        month_num = MONTHS.get(month_name.lower())
        if month_num and day_str.isdigit():
            day_num  = int(day_str)
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
    today = date.today()
    return today.fromordinal(today.toordinal() + days_ahead).strftime(fmt)

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

# ── Days-until helper ─────────────────────────────────────────────────────────
def days_until(d: date) -> str:
    delta = (d - date.today()).days
    if delta < 0:  return "expired"
    if delta == 0: return "⚠️ Last day today!"
    if delta <= 7: return f"⚠️ Only {delta} day{'s' if delta != 1 else ''} left!"
    if delta <= 30:return f"🔔 {delta} days left"
    return f"📅 Deadline: {d.strftime('%d %b %Y')}"

# ── Government subsidy schemes ────────────────────────────────────────────────
SUBSIDY_SCHEMES = [
    {"name": "PM-KISAN", "short": "₹6,000/year direct income support for farmers",
     "eligibility": "All landholding farmer families (subject to exclusion criteria like income tax payers, government employees in certain categories).",
     "benefit": "₹6,000 per year, paid in 3 installments of ₹2,000 directly to bank account.",
     "how_to_apply": "Apply online at pmkisan.gov.in or visit your nearest Common Service Centre (CSC) with Aadhaar, land records, and bank account details.",
     "link": "https://pmkisan.gov.in", "start_date": date(2019, 2, 24), "end_date": None,
     "renewal_note": "🔁 e-KYC must be renewed yearly to keep receiving installments."},
    {"name": "PMFBY – Kharif 2025", "short": "Crop insurance for Kharif 2025 season",
     "eligibility": "All farmers growing notified crops in notified areas, including sharecroppers and tenant farmers.",
     "benefit": "Low premium (2% of sum insured) crop insurance covering losses from natural calamities, pests, and diseases.",
     "how_to_apply": "Apply through your bank, CSC, or pmfby.gov.in before 31 July 2025 (Kharif cutoff).",
     "link": "https://pmfby.gov.in", "start_date": date(2025, 4, 1), "end_date": date(2025, 7, 31),
     "renewal_note": None},
    {"name": "PMFBY – Rabi 2025–26", "short": "Crop insurance for Rabi 2025–26 season",
     "eligibility": "All farmers growing notified Rabi crops.",
     "benefit": "Low premium (1.5% of sum insured) crop insurance covering losses from natural calamities.",
     "how_to_apply": "Apply through your bank, CSC, or pmfby.gov.in before 31 December 2025 (Rabi cutoff).",
     "link": "https://pmfby.gov.in", "start_date": date(2025, 10, 1), "end_date": date(2025, 12, 31),
     "renewal_note": None},
    {"name": "KCC (Kisan Credit Card)", "short": "Easy credit access for farming needs at low interest",
     "eligibility": "Farmers, tenant farmers, sharecroppers, and self-help group members.",
     "benefit": "Short-term loans at subsidized interest rates (4–7%) for crop production, equipment, and allied activities.",
     "how_to_apply": "Apply at any nearby bank branch with land documents and identity proof.",
     "link": "https://www.myscheme.gov.in/schemes/kcc", "start_date": date(1998, 8, 1), "end_date": None,
     "renewal_note": "🔁 Credit limit is reviewed and renewed annually by your bank."},
    {"name": "Soil Health Card Scheme", "short": "Free soil testing and crop-wise nutrient advice",
     "eligibility": "All farmers.",
     "benefit": "Free soil testing every 2 years with crop-wise fertilizer and nutrient recommendations to reduce input costs.",
     "how_to_apply": "Contact your local Krishi Vigyan Kendra (KVK) or agriculture department office to get your soil tested.",
     "link": "https://soilhealth.dac.gov.in", "start_date": date(2015, 2, 19), "end_date": None,
     "renewal_note": "🔁 Re-test and renew your card every 2 years."},
    {"name": "MGNREGA", "short": "100 days guaranteed rural employment",
     "eligibility": "Any rural household willing to do unskilled manual work (relevant for labourers).",
     "benefit": "Guaranteed 100 days of wage employment per year at the notified minimum wage.",
     "how_to_apply": "Register at your local Gram Panchayat to get a Job Card, then apply for work as needed.",
     "link": "https://nrega.nic.in", "start_date": date(2006, 2, 2), "end_date": None,
     "renewal_note": "🔁 100-day work entitlement resets every financial year (April–March)."},
    {"name": "PM Krishi Sinchayee Yojana (PMKSY)", "short": "Subsidy on drip & sprinkler irrigation systems",
     "eligibility": "All farmers; SC/ST and small/marginal farmers get higher subsidy (55%).",
     "benefit": "55% subsidy for small/marginal farmers, 45% for others on drip and sprinkler irrigation systems.",
     "how_to_apply": "Apply through your State Agriculture Department or pmksy.gov.in with land and Aadhaar details.",
     "link": "https://pmksy.gov.in", "start_date": date(2015, 7, 1), "end_date": None, "renewal_note": None},
    {"name": "National Food Security Mission (NFSM)", "short": "Free certified seeds & input support for key crops",
     "eligibility": "Farmers in notified districts growing rice, wheat, pulses, or coarse cereals.",
     "benefit": "Free/subsidised certified seeds, farm machinery, training, and demonstrations.",
     "how_to_apply": "Contact your Block Agriculture Officer or local Krishi Vigyan Kendra (KVK).",
     "link": "https://nfsm.gov.in", "start_date": date(2007, 10, 1), "end_date": None,
     "renewal_note": "🔁 Input support is allocated freshly each crop season — re-check with your KVK."},
    {"name": "Tamil Nadu CM's Drought Relief – 2025", "short": "One-time ₹2,000/acre relief for drought-affected TN farmers",
     "eligibility": "Farmers in Tamil Nadu districts declared drought-affected for 2024–25 season.",
     "benefit": "₹2,000 per acre (up to 5 acres) direct bank transfer to eligible farmers.",
     "how_to_apply": "Apply at your Village Administrative Office (VAO) with patta/chitta and bank passbook before 30 September 2025.",
     "link": "https://www.tn.gov.in", "start_date": date(2025, 3, 1), "end_date": date(2025, 9, 30),
     "renewal_note": None},
    {"name": "e-NAM (National Agriculture Market)", "short": "Sell crops online directly to buyers across India",
     "eligibility": "All farmers with produce registered at a linked APMC mandi.",
     "benefit": "Access to buyers across India, transparent online bidding, and direct bank payment — better prices, no middlemen.",
     "how_to_apply": "Register at enam.gov.in or through your local APMC/mandi office with Aadhaar and bank details.",
     "link": "https://enam.gov.in", "start_date": date(2016, 4, 14), "end_date": None, "renewal_note": None},
    {"name": "Agri Infrastructure Fund (AIF)", "short": "Low-interest loans for farm storage & processing",
     "eligibility": "Farmers, FPOs, PACS, SHGs, agri-entrepreneurs for post-harvest infrastructure.",
     "benefit": "Loans up to ₹2 crore at 3% interest subsidy for warehouses, cold storage, processing units.",
     "how_to_apply": "Apply through any scheduled bank or at agriinfra.dac.gov.in with project report and land documents.",
     "link": "https://agriinfra.dac.gov.in", "start_date": date(2020, 8, 9), "end_date": None, "renewal_note": None},
    {"name": "PM Fasal Bima (PMFBY) – Horticulture TN", "short": "Crop insurance for banana, tomato, onion (TN)",
     "eligibility": "Tamil Nadu farmers growing banana, tomato, onion, or other notified horticultural crops.",
     "benefit": "5% premium cap, covers crop loss from drought, flood, pests, and unseasonal rain.",
     "how_to_apply": "Apply at nearest Tamil Nadu Horticulture Department office or through your cooperative bank before season cutoff.",
     "link": "https://pmfby.gov.in", "start_date": date(2025, 6, 1), "end_date": date(2025, 8, 31),
     "renewal_note": None},
]

def schemes_deadline_within(days: int, today: date = None) -> list:
    today = today or date.today()
    return [s for s in active_schemes(today) if s["end_date"] and 0 <= (s["end_date"] - today).days <= days]

def active_schemes(today: date = None) -> list:
    today = today or date.today()
    return [s for s in SUBSIDY_SCHEMES if s["start_date"] <= today and (s["end_date"] is None or s["end_date"] >= today)]

def expired_schemes(today: date = None) -> list:
    today = today or date.today()
    return [s for s in SUBSIDY_SCHEMES if s["end_date"] and s["end_date"] < today]

def expiry_tag(scheme: dict) -> str:
    if scheme["end_date"] is None: return "🟢 Ongoing"
    return days_until(scheme["end_date"])

def next_cycle_estimate_str(scheme: dict, phone: str) -> str:
    est_start = scheme["start_date"].replace(year=scheme["start_date"].year + 1)
    return t("next_cycle_estimate", phone, est_start=est_start.strftime("%b %Y"))

def renewal_or_deadline_line(scheme: dict, phone: str) -> str:
    if scheme["end_date"] is None:
        return scheme.get("renewal_note") or t("no_deadline", phone)
    return f"📅 Deadline: {scheme['end_date'].strftime('%d %B %Y')}"

# ── Nearby-areas lookup ───────────────────────────────────────────────────────
NEARBY_AREAS = {
    "TIRUCHENGODE": ["Tiruchengode","Elacipalayam","Sankari","Mallasamudram","Pallipalayam","Komarapalayam","Sankaridurg","Erode","Karumanur","Mallasamudram West","Vennandur"],
    "SANKARI":      ["Sankari","Tiruchengode","Mallasamudram","Erode","Komarapalayam"],
    "ELACIPALAYAM": ["Elacipalayam","Tiruchengode","Sankari"],
    "MALLASAMUDRAM":["Mallasamudram","Mallasamudram West","Tiruchengode","Sankari","Karumanur"],
    "KOMARAPALAYAM":["Komarapalayam","Pallipalayam","Tiruchengode","Sankari"],
    "PALLIPALAYAM": ["Pallipalayam","Komarapalayam","Tiruchengode"],
    "ERODE":        ["Erode","Tiruchengode","Sankari","Perundurai"],
    "NAMAKKAL":     ["Namakkal","Tiruchengode","Rasipuram","Paramathi Velur"],
    "RASIPURAM":    ["Rasipuram","Namakkal","Tiruchengode"],
    "DINDIGUL":     ["Dindigul","Palani","Oddanchatram","Natham","Vedasandur","Nilakottai","Kodaikanal"],
    "PALANI":       ["Palani","Dindigul","Oddanchatram"],
}

def expand_nearby_locations(location: str) -> list:
    key = (location or "").strip().upper()
    if key in NEARBY_AREAS:
        return NEARBY_AREAS[key]
    for cluster_key, places in NEARBY_AREAS.items():
        if key in [p.upper() for p in places]:
            return places
    return [location] if location else []

def build_location_or_filter(location: str) -> str:
    places = expand_nearby_locations(location)
    conditions = ",".join(f"location.ilike.{quote(f'%{p}%', safe='')}" for p in places)
    return f"or=({conditions})"

# ── Weather ───────────────────────────────────────────────────────────────────
LOCATION_COORDS = {
    "TIRUCHENGODE": (11.3814, 77.8949), "SANKARI":      (11.4745, 77.8784),
    "ELACIPALAYAM": (11.3667, 77.8167), "MALLASAMUDRAM":(11.3333, 77.9333),
    "KOMARAPALAYAM":(11.4347, 77.7044), "PALLIPALAYAM": (11.4203, 77.7280),
    "ERODE":        (11.3410, 77.7172), "NAMAKKAL":     (11.2189, 78.1677),
    "RASIPURAM":    (11.4612, 78.1881), "DINDIGUL":     (10.3624, 77.9695),
    "PALANI":       (10.4486, 77.5240),
}

def get_rain_risk(location: str, target_date: date):
    key = (location or "").strip().upper()
    coords = LOCATION_COORDS.get(key)
    if not coords: return None
    lat, lon = coords
    days_ahead = (target_date - date.today()).days
    if days_ahead < 0 or days_ahead > 15: return None
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=precipitation_probability_max"
            "&timezone=Asia%2FKolkata"
            f"&forecast_days={max(days_ahead + 1, 1)}"
        )
        res = req.get(url, timeout=8)
        res.raise_for_status()
        data = res.json()
        dates = data.get("daily", {}).get("time", [])
        probs = data.get("daily", {}).get("precipitation_probability_max", [])
        target_str = target_date.strftime("%Y-%m-%d")
        if target_str not in dates: return None
        idx = dates.index(target_str)
        chance = probs[idx]
        if chance is None: return None
        if chance >= 60:   label = f"⚠️ {chance}% chance of rain — you may want to plan around it."
        elif chance >= 30: label = f"🌦️ {chance}% chance of rain."
        else:              label = f"☀️ Low rain risk ({chance}%)."
        return chance, label
    except Exception as e:
        print(f"[WEATHER] ERROR: {e}")
        return None

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
        res = req.get(url, headers=HEADERS, timeout=10)
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
        res = req.patch(url, json=data, headers=HEADERS, timeout=10)
        res.raise_for_status()
        result = res.json()
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"[DB] update_db ERROR: {e}\n{traceback.format_exc()}")
        return []

def get_jobs_by_phone(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{ep}&order=created_at.desc&limit=5"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_jobs_by_phone ERROR: {e}")
        return []

def get_open_jobs_by_location(location):
    try:
        url = f"{SUPABASE_URL}/rest/v1/jobs?{build_location_or_filter(location)}&status=eq.open&limit=5"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_open_jobs_by_location ERROR: {e}")
        return []

def get_labourers_by_location(location):
    try:
        url = f"{SUPABASE_URL}/rest/v1/labourers?{build_location_or_filter(location)}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_labourers_by_location ERROR: {e}")
        return []

def get_confirmed_jobs_for_farmer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{ep}&status=eq.confirmed&order=start_date.desc&limit=10"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_confirmed_jobs_for_farmer ERROR: {e}")
        return []

def get_confirmed_jobs_for_labourer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?labourer_phone=eq.{ep}&status=eq.confirmed&order=start_date.desc&limit=10"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_confirmed_jobs_for_labourer ERROR: {e}")
        return []

def get_completed_jobs_for_farmer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{ep}&status=eq.completed&order=start_date.desc&limit=10"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_completed_jobs_for_farmer ERROR: {e}")
        return []

def get_completed_jobs_for_labourer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?labourer_phone=eq.{ep}&status=eq.completed&order=start_date.desc&limit=10"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_completed_jobs_for_labourer ERROR: {e}")
        return []

def get_job_history_for_labourer(phone, limit=30):
    try:
        ep = quote(phone, safe="")
        url = (f"{SUPABASE_URL}/rest/v1/jobs?labourer_phone=eq.{ep}"
               f"&status=in.(confirmed,completed,cancelled)&order=start_date.desc&limit={limit}")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[DB] get_job_history_for_labourer ERROR: {e}")
        return []

def count_jobs_posted_by_farmer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?farmer_phone=eq.{ep}&select=id"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[DB] count_jobs_posted_by_farmer ERROR: {e}")
        return 0

def count_jobs_done_by_labourer(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?labourer_phone=eq.{ep}&status=eq.completed&select=id"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        data = res.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[DB] count_jobs_done_by_labourer ERROR: {e}")
        return 0

PENALTY_AMOUNT = 200
RATING_PENALTY = 0.5

def apply_penalty(table: str, phone: str, reason: str):
    record = get_from_db(table, phone)
    if not record: return []
    current_balance = record.get("penalty_balance", 0) or 0
    current_rating  = record.get("rating", 0) or 0
    new_balance = current_balance + PENALTY_AMOUNT
    new_rating  = max(0, round(current_rating - RATING_PENALTY, 1))
    print(f"[PENALTY] {table}/{phone} | reason={reason} | balance {current_balance}->{new_balance} | rating {current_rating}->{new_rating}")
    return update_db(table, {"phone": phone}, {"penalty_balance": new_balance, "rating": new_rating})

def increment_no_show(labourer_phone):
    labourer = get_from_db("labourers", labourer_phone)
    if not labourer: return []
    current = labourer.get("no_show_count", 0) or 0
    update_db("labourers", {"phone": labourer_phone}, {"no_show_count": current + 1})
    return apply_penalty("labourers", labourer_phone, "no_show")

def get_average_wage(work_type, location):
    try:
        words = [w for w in re.findall(r"[A-Za-z]+", work_type or "") if len(w) >= 3]
        if not words: return None
        key_word = words[0]
        url = (f"{SUPABASE_URL}/rest/v1/jobs?{build_location_or_filter(location)}"
               f"&status=eq.completed&work_type=ilike.{quote(f'%{key_word}%', safe='')}&select=wage")
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        rows = res.json()
        if not isinstance(rows, list) or not rows: return None
        wages = [float(r["wage"]) for r in rows if r.get("wage") not in (None, "")]
        return round(sum(wages) / len(wages)) if wages else None
    except Exception as e:
        print(f"[DB] get_average_wage ERROR: {e}")
        return None

def current_financial_year_bounds(today: date = None):
    today = today or date.today()
    if today.month >= 4:
        return date(today.year, 4, 1), date(today.year + 1, 3, 31)
    return date(today.year - 1, 4, 1), date(today.year, 3, 31)

def count_completed_days_in_range(phone, start: date, end: date):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/jobs?labourer_phone=eq.{ep}&status=eq.completed&select=start_date"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        rows = res.json()
        if not isinstance(rows, list): return 0
        count = 0
        for row in rows:
            raw = row.get("start_date")
            if not raw: continue
            try:
                d = datetime.strptime(raw, "%d %B %Y").date()
            except ValueError:
                continue
            if start <= d <= end:
                count += 1
        return count
    except Exception as e:
        print(f"[DB] count_completed_days_in_range ERROR: {e}")
        return 0

# ── Equipment DB helpers ──────────────────────────────────────────────────────
def save_equipment(data):
    return save_to_db("equipment", data)

def get_equipment_by_owner(phone):
    try:
        ep = quote(phone, safe="")
        url = f"{SUPABASE_URL}/rest/v1/equipment?owner_phone=eq.{ep}&order=created_at.desc&limit=10"
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
        url = f"{SUPABASE_URL}/rest/v1/equipment?{build_location_or_filter(location)}&available=eq.true&limit=10"
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

def notify_nearby_users_about_equipment(equipment, owner_phone: str = None):
    location = equipment["location"]
    op = owner_phone or equipment.get("owner_phone", "")
    farmers = []
    try:
        url = f"{SUPABASE_URL}/rest/v1/farmers?{build_location_or_filter(location)}"
        res = req.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        farmers = res.json() if isinstance(res.json(), list) else []
    except Exception as e:
        print(f"[NOTIFY] get farmers ERROR: {e}")
    labourers = get_labourers_by_location(location)
    until_str = equipment.get("available_until") or "Ongoing"
    for user in farmers + labourers:
        if user.get("phone") != op:
            send_whatsapp(
                user["phone"],
                t("new_equipment_notification", user["phone"],
                  location=location, name=equipment["name"],
                  rent=equipment["rent_per_day"], until=until_str)
            )

def notify_nearby_labourers(job):
    labourers = get_labourers_by_location(job["location"])
    print(f"[NOTIFY] Found {len(labourers)} labourer(s) near {job['location']}")
    for labourer in labourers:
        if labourer.get("phone") != job.get("farmer_phone"):
            send_whatsapp(
                labourer["phone"],
                t("new_job_notification", labourer["phone"],
                  work_type=job["work_type"], location=job["location"],
                  num_labourers=job["num_labourers"], wage=job["wage"],
                  start_date=job["start_date"], job_id=job["id"])
            )

def twiml_response(text):
    print(f"[REPLY] {text[:120]}")
    r = MessagingResponse()
    r.message(text)
    return Response(content=str(r), media_type="application/xml")

# ── HELP menus ────────────────────────────────────────────────────────────────
def help_farmer(phone: str) -> str:
    lang = get_lang(phone)
    if lang == "TA":
        return (
            "╔══════════════════════════╗\n║  🌾 விவசாயி உதவி         ║\n╚══════════════════════════╝\n\n"
            "📅 *TODAY* — தினசரி சுருக்கம்\n📋 *POST JOB* — புதிய வேலை போடுங்கள்\n"
            "📂 *MY JOBS* — உங்கள் வேலைகளை பாருங்கள்\n👥 *MY LABOURERS* — ஏற்றுக்கொண்ட / முடிந்த வேலைகள்\n"
            "✅ *JOB DONE [id]* — வேலை முடிந்தது என்று குறிக்கவும்\n   உதாரணம்: JOB DONE 12\n"
            "🔁 *REHIRE [id]* — முன்பு வேலை செய்தவரை மீண்டும் அழையுங்கள்\n   உதாரணம்: REHIRE 12\n"
            "❌ *CANCEL [id]* — வேலையை ரத்து செய்யுங்கள்\n   உதாரணம்: CANCEL 7\n"
            "⭐ *RATE [id] [1-5]* — தொழிலாளரை மதிப்பிடுங்கள்\n   உதாரணம்: RATE 12 5\n"
            "⚠️ *NO SHOW [id]* — வராத தொழிலாளரை புகார் செய்யுங்கள்\n"
            "🚜 *RENT EQUIPMENT* — உபகரணங்களை வாடகைக்கு போடுங்கள்\n"
            "🔧 *MY EQUIPMENT* — உங்கள் உபகரண பட்டியல்\n"
            "🏛️ *SUBSIDIES* — அரசு திட்டங்கள்\n🪪 *MY PROFILE* — உங்கள் சுயவிவரம்\n"
            "🌐 *LANGUAGE* — மொழி மாற்றவும்\n\nHELP என்று அனுப்பினால் இந்த பட்டியல் எப்போதும் வரும்."
        )
    if lang == "HI":
        return (
            "╔══════════════════════════╗\n║  🌾 किसान सहायता          ║\n╚══════════════════════════╝\n\n"
            "📅 *TODAY* — आज का सारांश\n📋 *POST JOB* — नई नौकरी पोस्ट करें\n"
            "📂 *MY JOBS* — अपनी नौकरियाँ देखें\n👥 *MY LABOURERS* — स्वीकृत/पूर्ण नौकरियाँ\n"
            "✅ *JOB DONE [id]* — काम पूरा होने पर मार्क करें\n   उदाहरण: JOB DONE 12\n"
            "🔁 *REHIRE [id]* — पुराने मजदूर को दोबारा बुलाएँ\n   उदाहरण: REHIRE 12\n"
            "❌ *CANCEL [id]* — नौकरी रद्द करें\n   उदाहरण: CANCEL 7\n"
            "⭐ *RATE [id] [1-5]* — मजदूर को रेट करें\n   उदाहरण: RATE 12 5\n"
            "⚠️ *NO SHOW [id]* — नहीं आने की रिपोर्ट करें\n"
            "🚜 *RENT EQUIPMENT* — उपकरण किराए पर दें\n🔧 *MY EQUIPMENT* — अपने उपकरण देखें\n"
            "🏛️ *SUBSIDIES* — सरकारी योजनाएँ\n🪪 *MY PROFILE* — अपनी प्रोफ़ाइल देखें\n"
            "🌐 *LANGUAGE* — भाषा बदलें\n\nHELP भेजने पर यह सूची कभी भी देख सकते हैं।"
        )
    return (
        "╔══════════════════════════╗\n║  🌾 FARMER HELP           ║\n╚══════════════════════════╝\n\n"
        "📅 *TODAY* — Your daily digest\n📋 *POST JOB* — Post a new job\n"
        "📂 *MY JOBS* — View your posted jobs\n👥 *MY LABOURERS* — Accepted/completed jobs\n"
        "✅ *JOB DONE [id]* — Mark a job complete\n   Example: JOB DONE 12\n"
        "🔁 *REHIRE [id]* — Invite a past labourer again\n   Example: REHIRE 12\n"
        "❌ *CANCEL [id]* — Cancel a job\n   Example: CANCEL 7\n"
        "⭐ *RATE [id] [1-5]* — Rate a labourer after work\n   Example: RATE 12 5\n"
        "⚠️ *NO SHOW [id]* — Report a labourer who didn't arrive\n   Example: NO SHOW 12\n"
        "🚜 *RENT EQUIPMENT* — List your equipment for rent\n🔧 *MY EQUIPMENT* — View your equipment listings\n"
        "🏛️ *SUBSIDIES* — Browse government schemes\n   Example: SUBSIDY 1\n"
        "🪪 *MY PROFILE* — View your profile & rating\n🌐 *LANGUAGE* — Change language (Tamil/Hindi/English)\n\n"
        "Send HELP anytime to see this list."
    )

def help_labourer(phone: str) -> str:
    lang = get_lang(phone)
    if lang == "TA":
        return (
            "╔══════════════════════════╗\n║  👷 தொழிலாளர் உதவி       ║\n╚══════════════════════════╝\n\n"
            "📅 *TODAY* — தினசரி சுருக்கம்\n🔍 *VIEW JOBS* — அருகிலுள்ள வேலைகளை பாருங்கள்\n"
            "✅ *CONFIRM [id]* — வேலையை ஏற்றுக்கொள்ளுங்கள்\n   உதாரணம்: CONFIRM 3\n"
            "👨‍🌾 *MY FARMERS* — ஏற்றுக்கொண்ட / முடிந்த வேலைகள்\n"
            "📜 *JOB HISTORY* — கடந்த வேலைகளின் வரலாறு\n📊 *MY DAYS* — MGNREGA நாள் கணக்கு\n"
            "⭐ *RATE [id] [1-5]* — விவசாயியை மதிப்பிடுங்கள்\n   உதாரணம்: RATE 12 4\n"
            "🚜 *VIEW EQUIPMENT* — வாடகை உபகரணங்களை பாருங்கள்\n"
            "🔖 *BOOK EQUIPMENT [id]* — உபகரணம் பதிவு செய்யுங்கள்\n"
            "🏛️ *SUBSIDIES* — அரசு திட்டங்கள்\n🛠️ *UPDATE SKILL* — உங்கள் திறனை மாற்றுங்கள்\n"
            "🪪 *MY PROFILE* — உங்கள் சுயவிவரம்\n🌐 *LANGUAGE* — மொழி மாற்றவும்\n\n"
            "HELP என்று அனுப்பினால் இந்த பட்டியல் எப்போதும் வரும்."
        )
    if lang == "HI":
        return (
            "╔══════════════════════════╗\n║  👷 मजदूर सहायता          ║\n╚══════════════════════════╝\n\n"
            "📅 *TODAY* — आज का सारांश\n🔍 *VIEW JOBS* — पास की नौकरियाँ देखें\n"
            "✅ *CONFIRM [id]* — नौकरी स्वीकार करें\n   उदाहरण: CONFIRM 3\n"
            "👨‍🌾 *MY FARMERS* — स्वीकृत/पूर्ण नौकरियाँ\n"
            "📜 *JOB HISTORY* — पुरानी नौकरियाँ\n📊 *MY DAYS* — MGNREGA दिन गिनती\n"
            "⭐ *RATE [id] [1-5]* — किसान को रेट करें\n   उदाहरण: RATE 12 4\n"
            "🚜 *VIEW EQUIPMENT* — किराए के उपकरण देखें\n🔖 *BOOK EQUIPMENT [id]* — उपकरण बुक करें\n"
            "🏛️ *SUBSIDIES* — सरकारी योजनाएँ\n🛠️ *UPDATE SKILL* — अपना कौशल बदलें\n"
            "🪪 *MY PROFILE* — अपनी प्रोफ़ाइल देखें\n🌐 *LANGUAGE* — भाषा बदलें\n\n"
            "HELP भेजने पर यह सूची कभी भी देख सकते हैं।"
        )
    return (
        "╔══════════════════════════╗\n║  👷 LABOURER HELP         ║\n╚══════════════════════════╝\n\n"
        "📅 *TODAY* — Your daily digest\n🔍 *VIEW JOBS* — See jobs near you\n"
        "✅ *CONFIRM [id]* — Accept a job\n   Example: CONFIRM 3\n"
        "👨‍🌾 *MY FARMERS* — Accepted/completed jobs\n"
        "📜 *JOB HISTORY* — Your full job history\n📊 *MY DAYS* — MGNREGA 100-day tracker\n"
        "⭐ *RATE [id] [1-5]* — Rate a farmer after work\n   Example: RATE 12 4\n"
        "🚜 *VIEW EQUIPMENT* — Browse equipment for rent\n"
        "🔖 *BOOK EQUIPMENT [id]* — Book equipment\n   Example: BOOK EQUIPMENT 3\n"
        "🏛️ *SUBSIDIES* — Browse government schemes\n   Example: SUBSIDY 1\n"
        "🛠️ *UPDATE SKILL* — Change your listed skill\n🪪 *MY PROFILE* — View your profile & rating\n"
        "🌐 *LANGUAGE* — Change language (Tamil/Hindi/English)\n\nSend HELP anytime to see this list."
    )

def help_unregistered(phone: str) -> str:
    lang = get_lang(phone)
    if lang == "TA":
        return (
            "🌾 *Farm Connect உதவி*\n\nநீங்கள் இன்னும் பதிவு செய்யவில்லை.\n\n"
            "தொடங்க:\n• *FARMER* — விவசாயியாக பதிவு செய்யவும்\n"
            "• *LABOURER* — தொழிலாளராக பதிவு செய்யவும்\n• *LANGUAGE* — மொழி மாற்றவும்\n\n"
            "HI என்று அனுப்பி தொடங்கவும்."
        )
    if lang == "HI":
        return (
            "🌾 *Farm Connect सहायता*\n\nआप अभी पंजीकृत नहीं हैं।\n\n"
            "शुरू करने के लिए:\n• *FARMER* — किसान के रूप में पंजीकरण\n"
            "• *LABOURER* — मजदूर के रूप में पंजीकरण\n• *LANGUAGE* — भाषा बदलें\n\n"
            "HI भेजकर शुरू करें।"
        )
    return (
        "🌾 *Farm Connect Help*\n\nYou're not registered yet.\n\n"
        "To get started:\n• *FARMER* — Register as a farmer\n"
        "• *LABOURER* — Register as a labourer\n• *LANGUAGE* — Change your language\n\n"
        "Send HI to begin."
    )

# ── Menu helpers ──────────────────────────────────────────────────────────────
def farmer_menu(name: str, phone: str = "") -> str:
    lang = get_lang(phone) if phone else "EN"
    lang_tip = {"TA": "🌐 LANGUAGE — மொழி மாற்றவும்", "HI": "🌐 LANGUAGE — भाषा बदलें"}.get(lang, "🌐 LANGUAGE — Change language")
    if lang == "TA":
        return (
            f"வணக்கம் {name}! 🌾 இன்று என்ன உதவி வேண்டும்?\n\n"
            f"╔══════════════════════╗\n║  🌾 விவசாயி மெனு      ║\n╚══════════════════════╝\n\n"
            f"📅 TODAY — தினசரி சுருக்கம்\n📋 POST JOB — புதிய வேலை போடுங்கள்\n"
            f"📂 MY JOBS — உங்கள் வேலைகள்\n👥 MY LABOURERS — தொழிலாளர் பட்டியல்\n"
            f"✅ JOB DONE [id] — வேலை முடிந்தது\n🔁 REHIRE [id] — மீண்டும் அழையுங்கள்\n"
            f"🚜 RENT EQUIPMENT — உபகரணம் வாடகை\n🔧 MY EQUIPMENT — உங்கள் உபகரணங்கள்\n"
            f"🏛️ SUBSIDIES — அரசு திட்டங்கள்\n🪪 MY PROFILE — சுயவிவரம்\n"
            f"{lang_tip}\n\n💡 HELP என்று அனுப்பி முழு விவரம் பெறுங்கள்."
        )
    if lang == "HI":
        return (
            f"नमस्ते {name}! 🌾 आज कैसे मदद करूँ?\n\n"
            f"╔══════════════════════╗\n║  🌾 किसान मेनू         ║\n╚══════════════════════╝\n\n"
            f"📅 TODAY — आज का सारांश\n📋 POST JOB — नई नौकरी पोस्ट करें\n"
            f"📂 MY JOBS — अपनी नौकरियाँ देखें\n👥 MY LABOURERS — मजदूरों की सूची\n"
            f"✅ JOB DONE [id] — काम पूरा मार्क करें\n🔁 REHIRE [id] — दोबारा बुलाएँ\n"
            f"🚜 RENT EQUIPMENT — उपकरण किराए पर दें\n🔧 MY EQUIPMENT — अपने उपकरण देखें\n"
            f"🏛️ SUBSIDIES — सरकारी योजनाएँ\n🪪 MY PROFILE — प्रोफ़ाइल देखें\n"
            f"{lang_tip}\n\n💡 HELP भेजें — पूरी कमांड सूची देखें।"
        )
    return (
        f"Hello {name}! 🌾 How can I help you today?\n\n"
        f"╔══════════════════════╗\n║  🌾 FARMER MENU      ║\n╚══════════════════════╝\n\n"
        f"📅 TODAY — Your daily digest\n📋 POST JOB — Post a new job\n"
        f"📂 MY JOBS — View your posted jobs\n👥 MY LABOURERS — Accepted/completed jobs\n"
        f"✅ JOB DONE [id] — Mark a job as completed\n🔁 REHIRE [id] — Invite a past labourer again\n"
        f"🚜 RENT EQUIPMENT — List equipment for rent\n🔧 MY EQUIPMENT — View your listings\n"
        f"🏛️ SUBSIDIES — Government schemes\n🪪 MY PROFILE — View your profile\n"
        f"{lang_tip}\n\n💡 Send HELP anytime for the full command list."
    )

def labourer_menu(name: str, phone: str = "") -> str:
    lang = get_lang(phone) if phone else "EN"
    lang_tip = {"TA": "🌐 LANGUAGE — மொழி மாற்றவும்", "HI": "🌐 LANGUAGE — भाषा बदलें"}.get(lang, "🌐 LANGUAGE — Change language")
    if lang == "TA":
        return (
            f"வணக்கம் {name}! 👋 இன்று என்ன உதவி வேண்டும்?\n\n"
            f"╔══════════════════════╗\n║  👷 தொழிலாளர் மெனு   ║\n╚══════════════════════╝\n\n"
            f"📅 TODAY — தினசரி சுருக்கம்\n🔍 VIEW JOBS — அருகிலுள்ள வேலைகள்\n"
            f"👨‍🌾 MY FARMERS — விவசாயி பட்டியல்\n📜 JOB HISTORY — கடந்த வேலைகள்\n"
            f"📊 MY DAYS — MGNREGA நாள் கணக்கு\n🚜 VIEW EQUIPMENT — உபகரணங்கள்\n"
            f"🏛️ SUBSIDIES — அரசு திட்டங்கள்\n🛠️ UPDATE SKILL — திறன் மாற்று\n"
            f"🪪 MY PROFILE — சுயவிவரம்\n{lang_tip}\n\n💡 HELP என்று அனுப்பி முழு விவரம் பெறுங்கள்."
        )
    if lang == "HI":
        return (
            f"नमस्ते {name}! 👋 आज कैसे मदद करूँ?\n\n"
            f"╔══════════════════════╗\n║  👷 मजदूर मेनू         ║\n╚══════════════════════╝\n\n"
            f"📅 TODAY — आज का सारांश\n🔍 VIEW JOBS — पास की नौकरियाँ\n"
            f"👨‍🌾 MY FARMERS — किसानों की सूची\n📜 JOB HISTORY — पुरानी नौकरियाँ\n"
            f"📊 MY DAYS — MGNREGA दिन गिनती\n🚜 VIEW EQUIPMENT — उपकरण देखें\n"
            f"🏛️ SUBSIDIES — सरकारी योजनाएँ\n🛠️ UPDATE SKILL — कौशल बदलें\n"
            f"🪪 MY PROFILE — प्रोफ़ाइल देखें\n{lang_tip}\n\n💡 HELP भेजें — पूरी कमांड सूची देखें।"
        )
    return (
        f"Hello {name}! 👋 How can I help you today?\n\n"
        f"╔══════════════════════╗\n║  👷 LABOURER MENU    ║\n╚══════════════════════╝\n\n"
        f"📅 TODAY — Your daily digest\n🔍 VIEW JOBS — See jobs near you\n"
        f"👨‍🌾 MY FARMERS — Accepted/completed jobs\n📜 JOB HISTORY — Your past jobs\n"
        f"📊 MY DAYS — MGNREGA day tracker\n🚜 VIEW EQUIPMENT — Browse equipment for rent\n"
        f"🏛️ SUBSIDIES — Government schemes\n🛠️ UPDATE SKILL — Change your listed skill\n"
        f"🪪 MY PROFILE — View your profile\n{lang_tip}\n\n💡 Send HELP anytime for the full command list."
    )

def welcome_back(phone: str) -> str | None:
    farmer = get_from_db("farmers", phone)
    if farmer:   return farmer_menu(farmer["name"], phone)
    labourer = get_from_db("labourers", phone)
    if labourer: return labourer_menu(labourer["name"], phone)
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

    # ── LANGUAGE command — available at ALL steps ─────────────────────────────
    if message == "LANGUAGE":
        sessions[phone]["prev_step"] = step
        sessions[phone]["step"] = "language"
        return t("language_prompt", phone)

    # ── LANGUAGE selection step ────────────────────────────────────────────────
    if step == "language":
        lang_key = SUPPORTED_LANGS.get(message)
        if not lang_key:
            return t("language_invalid", phone)
        set_lang(phone, lang_key)
        prev = sessions[phone].pop("prev_step", "done")
        sessions[phone]["step"] = prev
        _lang_conf = {
            "EN": "✅ Language set to *English*. All messages will now be in English.",
            "TA": "✅ மொழி *தமிழ்* ஆக அமைக்கப்பட்டது. இனி எல்லா செய்திகளும் தமிழில் இருக்கும்.",
            "HI": "✅ भाषा *हिंदी* पर सेट की गई। अब सभी संदेश हिंदी में होंगे।",
        }
        conf = _lang_conf[lang_key]
        if prev == "done":
            menu = welcome_back(phone)
            if menu:
                return f"{conf}\n\n{menu}"
        return conf

    # ── START ─────────────────────────────────────────────────────────────────
    if step == "start":
        farmer = get_from_db("farmers", phone)
        if farmer:
            sessions[phone] = {"step": "done", "role": "farmer"}
            return farmer_menu(farmer["name"], phone)
        labourer = get_from_db("labourers", phone)
        if labourer:
            sessions[phone] = {"step": "done", "role": "labourer"}
            return labourer_menu(labourer["name"], phone)
        sessions[phone]["step"] = "role"
        return t("welcome_new", phone)

    # ── REGISTRATION ──────────────────────────────────────────────────────────
    elif step == "role":
        if message in ("FARMER", "LABOURER"):
            opposite_table = "labourers" if message == "FARMER" else "farmers"
            opposite_role  = "labourer"  if message == "FARMER" else "farmer"
            existing_opposite = get_from_db(opposite_table, phone)
            if existing_opposite:
                sessions[phone] = {"step": "done", "role": opposite_role}
                menu = (farmer_menu(existing_opposite["name"], phone) if opposite_role == "farmer"
                        else labourer_menu(existing_opposite["name"], phone))
                return t("already_registered_as", phone,
                         role=opposite_role.upper(), name=existing_opposite["name"], menu=menu)
            sessions[phone]["role"] = message.lower()
            sessions[phone]["step"] = "name"
            return t("ask_name", phone)
        return t("reply_farmer_or_labourer", phone)

    elif step == "name":
        sessions[phone]["name"] = raw_body
        sessions[phone]["step"] = "location"
        return t("ask_location", phone, name=raw_body)

    elif step == "location":
        sessions[phone]["location"] = raw_body.title()
        role = sessions[phone].get("role")
        if role == "labourer":
            sessions[phone]["step"] = "skill"
            return skill_prompt_for(phone)
        else:
            saved = save_to_db("farmers", {
                "phone": phone, "name": sessions[phone]["name"],
                "location": sessions[phone]["location"]
            })
            if not saved:
                return t("error_saving", phone)
            sessions[phone]["step"] = "done"
            return t("registered_farmer", phone,
                     name=sessions[phone]["name"], location=sessions[phone]["location"])

    elif step == "skill":
        skill = SKILL_MAP.get(message)
        if not skill:
            return t("skill_invalid", phone, prompt=skill_prompt_for(phone))
        saved = save_to_db("labourers", {
            "phone": phone, "name": sessions[phone]["name"],
            "location": sessions[phone]["location"], "skill": skill
        })
        if not saved:
            return t("error_saving", phone)
        sessions[phone]["step"] = "done"
        sessions[phone]["role"] = "labourer"
        return t("registered_labourer", phone,
                 name=sessions[phone]["name"], location=sessions[phone]["location"], skill=skill)

    # ── UPDATE SKILL FLOW ─────────────────────────────────────────────────────
    elif step == "update_skill":
        skill = SKILL_MAP.get(message)
        if not skill:
            return t("skill_invalid", phone, prompt=skill_prompt_for(phone))
        updated = update_db("labourers", {"phone": phone}, {"skill": skill})
        sessions[phone]["step"] = "done"
        if not updated:
            return t("skill_update_error", phone)
        return t("skill_updated", phone, skill=skill)

    # ── MAIN MENU ─────────────────────────────────────────────────────────────
    elif step == "done":
        print(f"[FLOW] DONE menu — message='{message}'")

        # HELP
        if message == "HELP":
            farmer = get_from_db("farmers", phone)
            if farmer:   return help_farmer(phone)
            labourer = get_from_db("labourers", phone)
            if labourer: return help_labourer(phone)
            return help_unregistered(phone)

        # Greeting / menu
        normalised = message.strip("!?.👋🌾 ")
        if normalised in GREETINGS or message in GREETINGS:
            menu = welcome_back(phone)
            if menu: return menu
            sessions[phone] = {"step": "start"}
            return t("welcome_new", phone)

        # UPDATE SKILL
        if message == "UPDATE SKILL":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("labourers_only", phone)
            sessions[phone]["step"] = "update_skill"
            current = labourer.get("skill") or "Not set"
            return t("update_skill_prompt", phone, skill=current, prompt=skill_prompt_for(phone))

        # POST JOB
        elif message == "POST JOB":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("farmers_only", phone)
            sessions[phone]["step"] = "job_work_type"
            sessions[phone]["job"] = {}
            return t("post_job_start", phone)

        # MY PROFILE
        elif message == "MY PROFILE":
            farmer = get_from_db("farmers", phone)
            if farmer:
                total_posted = count_jobs_posted_by_farmer(phone)
                rating = farmer.get("rating")
                total_ratings = farmer.get("total_ratings", 0)
                s = "s" if total_ratings != 1 else ""
                rating_str = (t("profile_rating_str", phone, rating=rating, count=total_ratings, s=s)
                              if rating and total_ratings else t("profile_no_rating", phone))
                penalty_balance = farmer.get("penalty_balance", 0) or 0
                penalty_line = t("profile_penalty_line", phone, amount=penalty_balance) if penalty_balance else ""
                lang_label = {"EN": "English", "TA": "தமிழ்", "HI": "हिंदी"}.get(get_lang(phone), "English")
                return t("profile_farmer", phone, name=farmer["name"], location=farmer["location"],
                         rating_str=rating_str, total_posted=total_posted,
                         lang_label=lang_label, penalty_line=penalty_line)
            labourer = get_from_db("labourers", phone)
            if labourer:
                total_done = count_jobs_done_by_labourer(phone)
                rating = labourer.get("rating")
                total_ratings = labourer.get("total_ratings", 0)
                no_show = labourer.get("no_show_count", 0)
                s = "s" if total_ratings != 1 else ""
                rating_str = (t("profile_rating_str", phone, rating=rating, count=total_ratings, s=s)
                              if rating and total_ratings else t("profile_no_rating", phone))
                skill_str = labourer.get("skill") or t("profile_skill_not_set", phone)
                no_show_line = t("profile_no_show_line", phone, count=no_show) if no_show else ""
                penalty_balance = labourer.get("penalty_balance", 0) or 0
                penalty_line = t("profile_penalty_line", phone, amount=penalty_balance) if penalty_balance else ""
                lang_label = {"EN": "English", "TA": "தமிழ்", "HI": "हिंदी"}.get(get_lang(phone), "English")
                return t("profile_labourer", phone, name=labourer["name"], location=labourer["location"],
                         skill=skill_str, rating_str=rating_str, total_done=total_done,
                         lang_label=lang_label, no_show_line=no_show_line, penalty_line=penalty_line)
            return t("register_first", phone)

        # MY LABOURERS
        elif message == "MY LABOURERS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("farmers_only", phone)
            pending   = get_confirmed_jobs_for_farmer(phone)
            completed = get_completed_jobs_for_farmer(phone)
            if not pending and not completed:
                return t("my_labourers_empty", phone)
            msg = ""
            if pending:
                msg += t("my_labourers_pending_header", phone)
                for job in pending:
                    lab = get_from_db("labourers", job.get("labourer_phone")) if job.get("labourer_phone") else None
                    lab_name = lab["name"] if lab else t("unknown_name", phone)
                    no_show  = lab.get("no_show_count", 0) if lab else 0
                    flag = t("no_show_flag", phone, count=no_show) if no_show else ""
                    msg += (f"🔹 Job #{job['id']}\n"
                            f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                            f"   Labourer: {lab_name}{flag}\n"
                            f"   {t('in_progress', phone)}\n\n")
                msg += t("my_labourers_pending_footer", phone)
            if completed:
                msg += t("my_labourers_completed_header", phone)
                for job in completed:
                    rated = t("rated_label", phone) if job.get("rated") else t("not_rated_label", phone)
                    lab = get_from_db("labourers", job.get("labourer_phone")) if job.get("labourer_phone") else None
                    lab_name = lab["name"] if lab else t("unknown_name", phone)
                    rating_str = f" ({lab['rating']}⭐)" if lab and lab.get("rating") else ""
                    msg += (f"🔹 Job #{job['id']}\n"
                            f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                            f"   Labourer: {lab_name}{rating_str}\n"
                            f"   {rated}\n\n")
                msg += t("my_labourers_completed_footer", phone)
            return msg

        # REHIRE
        elif message.startswith("REHIRE"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return t("rehire_not_farmer", phone) if not get_from_db("farmers", phone) else \
                       "❓ Couldn't read that.\n\nFormat: REHIRE [job_id]\nExample: REHIRE 12"
            job_id = parts[1]
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("rehire_not_farmer", phone)
            try:
                res = req.get(f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}", headers=HEADERS, timeout=10)
                lookup = res.json()
            except Exception:
                return t("db_fetch_error", phone)
            if not lookup or lookup[0].get("farmer_phone") != phone:
                return t("rehire_job_not_found", phone)
            old_job = lookup[0]
            labourer_phone = old_job.get("labourer_phone")
            if not labourer_phone:
                return t("rehire_no_labourer", phone)
            labourer = get_from_db("labourers", labourer_phone)
            if not labourer:
                return t("rehire_labourer_not_found", phone)
            sessions[phone]["step"] = "rehire_work_type"
            sessions[phone]["rehire"] = {
                "work_type": old_job["work_type"], "num_labourers": old_job.get("num_labourers", 1),
                "wage": old_job["wage"], "labourer_phone": labourer_phone, "labourer_name": labourer["name"],
            }
            return t("rehire_start", phone, labourer_name=labourer["name"], work_type=old_job["work_type"])

        # MY FARMERS
        elif message == "MY FARMERS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("labourers_only", phone)
            pending   = get_confirmed_jobs_for_labourer(phone)
            completed = get_completed_jobs_for_labourer(phone)
            if not pending and not completed:
                return t("my_farmers_empty", phone)
            msg = ""
            if pending:
                msg += t("my_farmers_pending_header", phone)
                for job in pending:
                    farm = get_from_db("farmers", job.get("farmer_phone")) if job.get("farmer_phone") else None
                    farm_name = farm["name"] if farm else t("unknown_name", phone)
                    msg += (f"🔹 Job #{job['id']}\n"
                            f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                            f"   Farmer: {farm_name}\n"
                            f"   {t('my_farmers_pending_waiting', phone)}\n\n")
            if completed:
                msg += t("my_farmers_completed_header", phone)
                for job in completed:
                    rated = t("rated_label", phone) if job.get("labourer_rated") else t("not_rated_label", phone)
                    farm = get_from_db("farmers", job.get("farmer_phone")) if job.get("farmer_phone") else None
                    farm_name = farm["name"] if farm else t("unknown_name", phone)
                    rating_str = f" ({farm['rating']}⭐)" if farm and farm.get("rating") else ""
                    msg += (f"🔹 Job #{job['id']}\n"
                            f"   Work: {job['work_type']} | Date: {job['start_date']}\n"
                            f"   Farmer: {farm_name}{rating_str}\n"
                            f"   {rated}\n\n")
                msg += t("my_farmers_completed_footer", phone)
            return msg

        # JOB HISTORY
        elif message == "JOB HISTORY":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("labourers_only", phone)
            history = get_job_history_for_labourer(phone)
            if not history:
                return t("job_history_empty", phone)
            ongoing   = [j for j in history if j["status"] == "confirmed"]
            completed = [j for j in history if j["status"] == "completed"]
            cancelled = [j for j in history if j["status"] == "cancelled"]
            def _fmt_job(job, icon):
                farm = get_from_db("farmers", job.get("farmer_phone")) if job.get("farmer_phone") else None
                farm_name = farm["name"] if farm else t("unknown_name", phone)
                return (
                    f"{icon} Job #{job['id']} — {job['work_type']}\n"
                    f"   📍 {job['location']} | 📅 {job['start_date']}\n"
                    f"   {t('job_history_farmer_label', phone, f_name=farm_name, wage=job['wage'])}\n"
                    f"   Status: {job['status'].upper()}\n\n"
                )
            msg = t("job_history_header", phone)
            if ongoing:
                msg += t("job_history_ongoing", phone)
                for job in ongoing: msg += _fmt_job(job, "🕓")
            if completed:
                msg += t("job_history_completed", phone)
                for job in completed: msg += _fmt_job(job, "✅")
            if cancelled:
                msg += t("job_history_cancelled", phone)
                for job in cancelled: msg += _fmt_job(job, "❌")
            msg += t("job_history_footer", phone)
            return msg

        # TODAY
        elif message == "TODAY":
            farmer = get_from_db("farmers", phone)
            if farmer:
                pending_confirm = get_confirmed_jobs_for_farmer(phone)
                pending_rate    = [j for j in get_completed_jobs_for_farmer(phone) if not j.get("rated")]
                deadlines       = schemes_deadline_within(7)
                msg = t("today_header_farmer", phone, name=farmer["name"], date=date.today().strftime("%d %B %Y"))
                if pending_confirm:
                    msg += t("today_in_progress", phone, count=len(pending_confirm))
                    for job in pending_confirm[:3]:
                        msg += f"   • #{job['id']} {job['work_type']} — JOB DONE {job['id']}\n"
                    msg += "\n"
                if pending_rate:
                    msg += t("today_pending_rate", phone, count=len(pending_rate))
                    for job in pending_rate[:3]:
                        msg += f"   • RATE {job['id']} [1-5]\n"
                    msg += "\n"
                if deadlines:
                    msg += t("today_subsidy_deadlines", phone)
                    for s in deadlines:
                        msg += f"   • {s['name']} — {days_until(s['end_date'])}\n"
                    msg += "\n"
                if not pending_confirm and not pending_rate and not deadlines:
                    msg += t("today_nothing_farmer", phone)
                return msg.strip()
            labourer = get_from_db("labourers", phone)
            if labourer:
                open_jobs       = get_open_jobs_by_location(labourer["location"])
                pending_confirm = get_confirmed_jobs_for_labourer(phone)
                pending_rate    = [j for j in get_completed_jobs_for_labourer(phone) if not j.get("labourer_rated")]
                deadlines       = schemes_deadline_within(7)
                msg = t("today_header_labourer", phone, name=labourer["name"], date=date.today().strftime("%d %B %Y"))
                if open_jobs:
                    msg += t("today_open_jobs", phone, count=len(open_jobs), location=labourer["location"])
                    for job in open_jobs[:3]:
                        msg += f"   • #{job['id']} {job['work_type']} — ₹{job['wage']}/day — CONFIRM {job['id']}\n"
                    msg += "\n"
                if pending_confirm:
                    msg += t("today_accepted_jobs", phone, count=len(pending_confirm))
                    for job in pending_confirm[:3]:
                        msg += f"   • #{job['id']} {job['work_type']} on {job['start_date']}\n"
                    msg += "\n"
                if pending_rate:
                    msg += t("today_pending_rate", phone, count=len(pending_rate))
                    for job in pending_rate[:3]:
                        msg += f"   • RATE {job['id']} [1-5]\n"
                    msg += "\n"
                if deadlines:
                    msg += t("today_subsidy_deadlines", phone)
                    for s in deadlines:
                        msg += f"   • {s['name']} — {days_until(s['end_date'])}\n"
                    msg += "\n"
                if not open_jobs and not pending_confirm and not pending_rate and not deadlines:
                    msg += t("today_nothing_labourer", phone)
                return msg.strip()
            return t("register_first", phone)

        # MY DAYS
        elif message == "MY DAYS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("labourers_only", phone)
            fy_start, fy_end = current_financial_year_bounds()
            days_done = count_completed_days_in_range(phone, fy_start, fy_end)
            days_left = max(0, 100 - days_done)
            bar_filled = min(20, round((days_done / 100) * 20))
            bar = "🟩" * bar_filled + "⬜" * (20 - bar_filled)
            return t("my_days_result", phone,
                     fy_start=fy_start.strftime("%b %Y"), fy_end=fy_end.strftime("%b %Y"),
                     bar=bar, days_done=days_done, days_left=days_left)

        # JOB DONE
        elif message.startswith("JOB DONE"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: JOB DONE [job_id]\nExample: JOB DONE 12"
            job_id = parts[2]
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("job_done_not_farmer", phone)
            updated = update_db("jobs", {"id": job_id, "farmer_phone": phone, "status": "confirmed"}, {"status": "completed"})
            if not updated:
                return t("job_done_not_found", phone)
            job = updated[0]
            labourer_phone = job.get("labourer_phone")
            labourer = get_from_db("labourers", labourer_phone) if labourer_phone else None
            labourer_name = labourer["name"] if labourer else t("unknown_name", phone)
            if labourer_phone:
                send_whatsapp(labourer_phone,
                    t("job_done_labourer_whatsapp", labourer_phone,
                      work_type=job["work_type"], location=job["location"],
                      start_date=job["start_date"], job_id=job["id"]))
            return t("job_done_success", phone,
                     job_id=job["id"], work_type=job["work_type"], labourer_name=labourer_name)

        # RATE
        elif message.startswith("RATE"):
            parts = raw_body.split()
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                return t("rate_invalid_format", phone)
            job_id, stars = parts[1], int(parts[2])
            if stars < 1 or stars > 5:
                return t("rate_stars_range", phone)
            farmer   = get_from_db("farmers", phone)
            labourer = get_from_db("labourers", phone)
            if not farmer and not labourer:
                return t("register_first", phone)
            try:
                res  = req.get(f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}", headers=HEADERS, timeout=10)
                jobs = res.json()
            except Exception:
                return t("db_fetch_error", phone)
            if not jobs:
                return t("job_not_found", phone)
            job = jobs[0]
            if job["status"] != "completed":
                return t("rate_job_not_completed", phone)
            star_display = "⭐" * stars
            if farmer and job.get("farmer_phone") == phone:
                if job.get("rated"):
                    return t("rate_already_rated", phone)
                labourer_phone = job.get("labourer_phone")
                if not labourer_phone:
                    return t("rate_no_labourer", phone)
                target = get_from_db("labourers", labourer_phone)
                if not target:
                    return t("rate_person_not_found", phone, role="Labourer")
                old_total = target.get("total_ratings", 0)
                old_rating = target.get("rating", 0)
                new_total  = old_total + 1
                new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
                update_db("labourers", {"phone": labourer_phone}, {"rating": new_rating, "total_ratings": new_total})
                update_db("jobs", {"id": job_id}, {"rated": True})
                return t("rate_success", phone, name=target["name"], stars=star_display,
                         new_rating=new_rating, total=new_total)
            elif labourer and job.get("labourer_phone") == phone:
                if job.get("labourer_rated"):
                    return t("rate_already_rated", phone)
                farmer_phone = job.get("farmer_phone")
                if not farmer_phone:
                    return t("rate_no_farmer", phone)
                target = get_from_db("farmers", farmer_phone)
                if not target:
                    return t("rate_person_not_found", phone, role="Farmer")
                old_total = target.get("total_ratings", 0)
                old_rating = target.get("rating", 0)
                new_total  = old_total + 1
                new_rating = round(((old_rating * old_total) + stars) / new_total, 1)
                update_db("farmers", {"phone": farmer_phone}, {"rating": new_rating, "total_ratings": new_total})
                update_db("jobs", {"id": job_id}, {"labourer_rated": True})
                return t("rate_success", phone, name=target["name"], stars=star_display,
                         new_rating=new_rating, total=new_total)
            else:
                return t("rate_not_your_job", phone)

        # MY JOBS
        elif message == "MY JOBS":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("farmers_only", phone)
            jobs = get_jobs_by_phone(phone)
            if not jobs:
                return t("my_jobs_empty", phone)
            status_icon = {"open": "🟢", "confirmed": "🕓", "completed": "✅", "cancelled": "❌"}
            status_label = {
                "open":      t("status_open", phone),
                "confirmed": t("status_confirmed", phone),
                "completed": t("status_completed", phone),
                "cancelled": t("status_cancelled", phone),
            }
            msg = t("my_jobs_header", phone)
            for i, job in enumerate(jobs):
                icon  = status_icon.get(job["status"], "⚪")
                label = status_label.get(job["status"], job["status"].upper())
                msg += (f"{i+1}. {job['work_type']} — {job['location']}\n"
                        f"   👥 {job['num_labourers']} labourers | ₹{job['wage']}/day\n"
                        f"   📅 {job['start_date']} | {icon} {label}\n"
                        f"   ID: {job['id']}\n\n")
            msg += t("my_jobs_footer", phone)
            return msg

        # VIEW JOBS
        elif message == "VIEW JOBS":
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("labourers_only", phone)
            jobs = get_open_jobs_by_location(labourer["location"])
            rating = labourer.get("rating")
            total_ratings = labourer.get("total_ratings", 0)
            s = "s" if total_ratings != 1 else ""
            if rating and total_ratings:
                rating_line = t("rating_line", phone, rating=rating, count=total_ratings, s=s)
            else:
                rating_line = t("no_rating_yet", phone)
            if not jobs:
                return rating_line + t("no_jobs_nearby", phone, location=labourer["location"])
            msg = t("view_jobs_header", phone, location=labourer["location"], rating_line=rating_line)
            for i, job in enumerate(jobs):
                msg += t("view_jobs_item", phone, i=i+1, work_type=job["work_type"],
                         location=job["location"], num_labourers=job["num_labourers"],
                         wage=job["wage"], start_date=job["start_date"], job_id=job["id"])
            return msg

        # CONFIRM
        elif message.startswith("CONFIRM"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CONFIRM [job_id]\nExample: CONFIRM 3"
            job_id   = parts[1]
            labourer = get_from_db("labourers", phone)
            if not labourer:
                return t("confirm_not_labourer", phone)
            try:
                res    = req.get(f"{SUPABASE_URL}/rest/v1/jobs?id=eq.{job_id}", headers=HEADERS, timeout=10)
                lookup = res.json()
            except Exception:
                return t("db_fetch_error", phone)
            if not lookup:
                return t("confirm_already_taken", phone)
            if lookup[0].get("farmer_phone") == phone:
                return t("confirm_own_job", phone)
            updated = update_db("jobs", {"id": job_id, "status": "open"},
                                {"status": "confirmed", "labourer_phone": phone})
            if not updated:
                return t("confirm_already_taken", phone)
            job = updated[0]
            send_whatsapp(job["farmer_phone"],
                t("confirm_farmer_whatsapp", job["farmer_phone"],
                  labourer_name=labourer["name"], skill=labourer.get("skill", "General"),
                  work_type=job["work_type"], location=job["location"],
                  start_date=job["start_date"], job_id=job["id"]))
            return t("job_confirmed_labourer", phone,
                     work_type=job["work_type"], location=job["location"],
                     start_date=job["start_date"], wage=job["wage"])

        # NO SHOW
        elif message.startswith("NO SHOW"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: NO SHOW [job_id]\nExample: NO SHOW 12"
            job_id = parts[2]
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("no_show_not_farmer", phone)
            updated = update_db("jobs", {"id": job_id, "farmer_phone": phone, "status": "confirmed"}, {"status": "cancelled"})
            if not updated:
                return t("no_show_not_found", phone)
            job = updated[0]
            labourer_phone = job.get("labourer_phone")
            if not labourer_phone:
                return t("no_show_no_labourer", phone, job_id=job_id)
            increment_no_show(labourer_phone)
            labourer = get_from_db("labourers", labourer_phone)
            labourer_name = labourer["name"] if labourer else t("unknown_name", phone)
            send_whatsapp(labourer_phone,
                t("no_show_penalty_whatsapp", labourer_phone,
                  job_id=job_id, work_type=job["work_type"],
                  amount=PENALTY_AMOUNT, drop=RATING_PENALTY))
            return t("no_show_success", phone, job_id=job_id, labourer_name=labourer_name,
                     amount=PENALTY_AMOUNT, rating_drop=RATING_PENALTY)

        # CANCEL JOB
        elif message.startswith("CANCEL") and not message.startswith("CANCEL EQUIPMENT"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CANCEL [job_id]\nExample: CANCEL 7"
            job_id  = parts[1]
            updated = update_db("jobs", {"id": job_id, "farmer_phone": phone}, {"status": "cancelled"})
            if not updated:
                return t("job_not_found", phone)
            job = updated[0]
            labourer_phone = job.get("labourer_phone")
            penalty_line = ""
            if labourer_phone:
                apply_penalty("farmers", phone, "cancel_confirmed_job")
                penalty_line = t("cancel_penalty_line", phone, amount=PENALTY_AMOUNT, rating_drop=RATING_PENALTY)
                send_whatsapp(labourer_phone,
                    t("cancel_labourer_whatsapp", labourer_phone,
                      work_type=job["work_type"], location=job["location"], start_date=job["start_date"]))
            return t("cancel_success", phone, job_id=job_id) + penalty_line

        # RENT EQUIPMENT
        elif message == "RENT EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("farmers_only", phone)
            sessions[phone]["step"] = "equip_name"
            sessions[phone]["equip"] = {}
            return t("rent_equipment_start", phone)

        # VIEW EQUIPMENT
        elif message == "VIEW EQUIPMENT":
            user = get_from_db("farmers", phone) or get_from_db("labourers", phone)
            if not user:
                return t("register_first", phone)
            location = user.get("location", "")
            items = get_equipment_by_location(location)
            if not items:
                return t("view_equipment_empty", phone, location=location)
            msg = t("view_equipment_header", phone, location=location)
            for i, item in enumerate(items):
                until = item.get("available_until") or t("ongoing_label_short", phone)
                msg += t("view_equipment_item", phone, i=i+1, name=item["name"],
                         rent=item["rent_per_day"], until=until, eq_id=item["id"])
            return msg

        # BOOK EQUIPMENT
        elif message.startswith("BOOK EQUIPMENT"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: BOOK EQUIPMENT [id]\nExample: BOOK EQUIPMENT 3"
            equipment_id = parts[2]
            user = get_from_db("farmers", phone) or get_from_db("labourers", phone)
            if not user:
                return t("register_first", phone)
            item = get_equipment_by_id(equipment_id)
            if not item:
                return t("job_not_found", phone)
            if not item.get("available"):
                return t("book_equipment_unavailable", phone, name=item["name"])
            if item.get("owner_phone") == phone:
                return t("book_own_equipment", phone)
            updated = update_db("equipment", {"id": equipment_id}, {"available": False, "booked_by": phone})
            if not updated:
                return t("book_equipment_error", phone)
            until = item.get("available_until") or t("ongoing_label_short", phone)
            send_whatsapp(item["owner_phone"],
                t("equip_booking_owner_whatsapp", item["owner_phone"],
                  name=item["name"], user_name=user["name"], phone=phone, rent=item["rent_per_day"]))
            return t("book_equipment_success", phone, name=item["name"], rent=item["rent_per_day"], until=until)

        # MY EQUIPMENT
        elif message == "MY EQUIPMENT":
            farmer = get_from_db("farmers", phone)
            if not farmer:
                return t("farmers_only", phone)
            items = get_equipment_by_owner(phone)
            if not items:
                return t("my_equipment_empty", phone)
            msg = t("my_equipment_header", phone)
            for i, item in enumerate(items):
                status = t("equip_status_available", phone) if item.get("available") else t("equip_status_booked", phone)
                until  = item.get("available_until") or t("ongoing_label_short", phone)
                msg += (f"{i+1}. 🔧 {item['name']}\n"
                        f"   💰 ₹{item['rent_per_day']}/day | {status}\n"
                        f"   📅 Until: {until}\n"
                        f"   ID: {item['id']}\n\n")
            msg += t("my_equipment_footer", phone)
            return msg

        # CANCEL EQUIPMENT
        elif message.startswith("CANCEL EQUIPMENT"):
            parts = raw_body.split()
            if len(parts) < 3 or not parts[2].isdigit():
                return "❓ Couldn't read that.\n\nFormat: CANCEL EQUIPMENT [id]\nExample: CANCEL EQUIPMENT 3"
            equipment_id = parts[2]
            item = get_equipment_by_id(equipment_id)
            if not item:
                return t("job_not_found", phone)
            if item.get("owner_phone") != phone:
                return t("cancel_equipment_not_yours", phone)
            updated = update_db("equipment", {"id": equipment_id}, {"available": False})
            if not updated:
                return t("cancel_equipment_error", phone)
            booked_by = item.get("booked_by")
            if booked_by:
                send_whatsapp(booked_by,
                    t("equip_cancel_booker_whatsapp", booked_by,
                      name=item["name"], location=item["location"]))
            return t("cancel_equipment_success", phone, eq_id=equipment_id, name=item["name"])

        # SUBSIDIES
        elif message == "SUBSIDIES":
            schemes = active_schemes()
            expired = expired_schemes()
            if not schemes and not expired:
                return t("subsidies_no_schemes", phone)
            lang = get_lang(phone)
            msg = t("subsidies_header", phone) if schemes else t("subsidies_none", phone)
            for i, scheme in enumerate(schemes):
                short = tr(scheme["short"], lang)
                msg += f"{i+1}. 📌 {scheme['name']}\n   {short}\n   {expiry_tag(scheme)}\n\n"
            if expired:
                msg += t("subsidies_expired_header", phone)
                offset = len(schemes)
                for i, scheme in enumerate(expired):
                    msg += f"{offset+i+1}. 📌 {scheme['name']} — {t('subsidy_expired_label', phone)}\n"
                msg += "\n"
            msg += t("subsidies_footer", phone)
            return msg

        # SUBSIDY [n]
        elif message.startswith("SUBSIDY"):
            parts = raw_body.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return "❓ Couldn't read that.\n\nFormat: SUBSIDY [number]\nExample: SUBSIDY 2"
            schemes  = active_schemes()
            expired  = expired_schemes()
            combined = schemes + expired
            index    = int(parts[1]) - 1
            if index < 0 or index >= len(combined):
                return t("subsidy_invalid_number", phone, count=len(combined))
            scheme     = combined[index]
            is_expired = index >= len(schemes)
            lang = get_lang(phone)
            eligibility  = tr(scheme["eligibility"],  lang)
            benefit      = tr(scheme["benefit"],       lang)
            how_to_apply = tr(scheme["how_to_apply"], lang)
            short        = tr(scheme["short"],         lang)
            if is_expired:
                return t("subsidy_detail_expired", phone,
                         name=scheme["name"],
                         end_date=scheme["end_date"].strftime("%d %B %Y"),
                         next_cycle=next_cycle_estimate_str(scheme, phone),
                         eligibility=eligibility, benefit=benefit,
                         how_to_apply=how_to_apply, link=scheme["link"])
            return t("subsidy_detail_active", phone,
                     name=scheme["name"], tag=expiry_tag(scheme),
                     deadline_line=renewal_or_deadline_line(scheme, phone),
                     eligibility=eligibility, benefit=benefit,
                     how_to_apply=how_to_apply, link=scheme["link"])

        # Unknown command
        else:
            suggestion, hint = fuzzy_suggestion(message)
            if suggestion:
                return t("unknown_command", phone, suggestion=suggestion, hint=hint)
            farmer = get_from_db("farmers", phone)
            if farmer:   return farmer_menu(farmer["name"], phone)
            labourer = get_from_db("labourers", phone)
            if labourer: return labourer_menu(labourer["name"], phone)
            sessions[phone] = {"step": "start"}
            return t("welcome_new", phone)

    # ── JOB POSTING FLOW ──────────────────────────────────────────────────────
    elif step == "job_work_type":
        sessions[phone]["job"]["work_type"] = raw_body
        sessions[phone]["step"] = "job_num_labourers"
        return t("ask_num_labourers", phone)

    elif step == "job_num_labourers":
        if not raw_body.isdigit():
            return t("ask_num_labourers_invalid", phone)
        sessions[phone]["job"]["num_labourers"] = int(raw_body)
        sessions[phone]["step"] = "job_wage"
        farmer   = get_from_db("farmers", phone)
        location = farmer.get("location", "") if farmer else ""
        avg_wage = get_average_wage(sessions[phone]["job"]["work_type"], location) if location else None
        if avg_wage:
            return t("ask_wage_with_avg", phone, work_type=sessions[phone]["job"]["work_type"],
                     location=location, avg=avg_wage)
        return t("ask_wage", phone)

    elif step == "job_wage":
        if not raw_body.replace(".", "", 1).isdigit():
            return t("ask_wage_invalid", phone)
        sessions[phone]["job"]["wage"] = raw_body
        sessions[phone]["step"] = "job_date"
        farmer   = get_from_db("farmers", phone)
        location = farmer.get("location", "") if farmer else ""
        avg_wage = get_average_wage(sessions[phone]["job"]["work_type"], location) if location else None
        date_prompt = t("ask_date", phone, example=example_future_date_str())
        if avg_wage:
            entered = float(raw_body)
            if entered < avg_wage * 0.8:
                return t("wage_below_avg", phone, avg=avg_wage, date_prompt=date_prompt)
        return date_prompt

    elif step == "job_date":
        is_valid, normalized_date, err = validate_future_date(raw_body)
        if not is_valid:
            return err
        job    = sessions[phone]["job"]
        farmer = get_from_db("farmers", phone)
        if not farmer:
            sessions[phone]["step"] = "done"
            return t("farmer_profile_not_found", phone)
        location = farmer.get("location", "Unknown")
        job["start_date"] = normalized_date
        saved = save_to_db("jobs", {
            "farmer_phone": phone, "work_type": job["work_type"],
            "num_labourers": job["num_labourers"], "wage": job["wage"],
            "start_date": job["start_date"], "location": location, "status": "open"
        })
        sessions[phone]["step"] = "done"
        if not saved:
            return t("job_post_error", phone)
        notify_nearby_labourers(saved)
        weather_line = ""
        parsed_date, _ = parse_job_date(raw_body)
        if parsed_date:
            risk = get_rain_risk(location, parsed_date)
            if risk:
                _, label = risk
                weather_line = f"\n🌤️ Weather for {job['start_date']}: {label}\n"
        return t("job_posted", phone,
                 location=location, work_type=job["work_type"], num_labourers=job["num_labourers"],
                 wage=job["wage"], start_date=job["start_date"], weather_line=weather_line)

    # ── REHIRE FLOW ───────────────────────────────────────────────────────────
    elif step == "rehire_work_type":
        rehire = sessions[phone]["rehire"]
        if message not in ("SAME", "KEEP"):
            rehire["work_type"] = raw_body
        sessions[phone]["step"] = "rehire_num_labourers"
        return t("rehire_ask_num", phone, count=rehire["num_labourers"])

    elif step == "rehire_num_labourers":
        rehire = sessions[phone]["rehire"]
        if message not in ("SAME", "KEEP"):
            if not raw_body.isdigit():
                return t("rehire_num_invalid", phone)
            rehire["num_labourers"] = int(raw_body)
        sessions[phone]["step"] = "rehire_wage"
        return t("rehire_ask_wage", phone, wage=rehire["wage"])

    elif step == "rehire_wage":
        rehire = sessions[phone]["rehire"]
        if message not in ("SAME", "KEEP"):
            if not raw_body.replace(".", "", 1).isdigit():
                return t("rehire_wage_invalid", phone)
            rehire["wage"] = raw_body
        sessions[phone]["step"] = "rehire_date"
        return t("rehire_ask_date", phone, example=example_future_date_str())

    elif step == "rehire_date":
        is_valid, normalized_date, err = validate_future_date(raw_body)
        if not is_valid:
            return err
        rehire = sessions[phone]["rehire"]
        farmer = get_from_db("farmers", phone)
        if not farmer:
            sessions[phone]["step"] = "done"
            return t("farmer_profile_not_found", phone)
        location = farmer.get("location", "Unknown")
        saved = save_to_db("jobs", {
            "farmer_phone": phone, "work_type": rehire["work_type"],
            "num_labourers": rehire["num_labourers"], "wage": rehire["wage"],
            "start_date": normalized_date, "location": location, "status": "open"
        })
        sessions[phone]["step"] = "done"
        if not saved:
            return t("rehire_error", phone)
        send_whatsapp(rehire["labourer_phone"],
            t("rehire_labourer_whatsapp", rehire["labourer_phone"],
              farmer_name=farmer["name"], work_type=rehire["work_type"], location=location,
              num_labourers=rehire["num_labourers"], wage=rehire["wage"],
              start_date=normalized_date, job_id=saved["id"]))
        return t("rehire_success", phone,
                 labourer_name=rehire["labourer_name"], work_type=rehire["work_type"],
                 num_labourers=rehire["num_labourers"], start_date=normalized_date,
                 wage=rehire["wage"], job_id=saved["id"])

    # ── EQUIPMENT LISTING FLOW ────────────────────────────────────────────────
    elif step == "equip_name":
        sessions[phone]["equip"]["name"] = raw_body
        sessions[phone]["step"] = "equip_rent"
        return t("ask_rent_per_day", phone, name=raw_body)

    elif step == "equip_rent":
        if not raw_body.replace(".", "", 1).isdigit():
            return t("ask_rent_invalid", phone)
        sessions[phone]["equip"]["rent_per_day"] = raw_body
        sessions[phone]["step"] = "equip_available_until"
        return t("ask_available_until", phone, example=example_future_date_str(days_ahead=10))

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
            return t("equipment_list_error", phone)
        notify_nearby_users_about_equipment(saved, owner_phone=phone)
        until_str = available_until or t("ongoing_label_short", phone)
        return t("equipment_listed", phone,
                 name=equip["name"], rent=equip["rent_per_day"],
                 location=farmer.get("location", "Unknown"), until=until_str)

    # ── FALLBACK ──────────────────────────────────────────────────────────────
    else:
        print(f"[FLOW] Unknown step '{step}' — resetting")
        sessions[phone] = {"step": "start"}
        return t("start_over", phone)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "ok"}

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"message": "Farm Connect API is running 🌾"}

@app.post("/webhook")
async def whatsapp_webhook(
    Body: str = Form(""),
    From: str = Form(...),
    NumMedia: str = Form("0"),
    MediaContentType0: str = Form(None),
):
    try:
        if NumMedia and NumMedia.isdigit() and int(NumMedia) > 0:
            content_type = (MediaContentType0 or "").lower()
            if content_type.startswith("audio"):
                reply = t("voice_not_supported", From)
            else:
                reply = t("media_not_supported", From)
            return twiml_response(reply)
        reply = handle_message(From, Body.strip())
        return twiml_response(reply)
    except Exception:
        print(f"[WEBHOOK] UNHANDLED EXCEPTION:\n{traceback.format_exc()}")
        err = MessagingResponse()
        err.message("⚠️ Something went wrong. Please try again in a moment.")
        return Response(content=str(err), media_type="application/xml")

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

@app.post("/reset")
async def reset_session(request: Request):
    data  = await request.json()
    phone = data.get("phone", "").strip()
    if phone in sessions:
        del sessions[phone]
        print(f"[RESET] Session cleared for {phone}")
    if phone in LANG_PREFS:
        del LANG_PREFS[phone]
        print(f"[RESET] Language preference cleared for {phone}")
    return JSONResponse({"status": "reset", "phone": phone})

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

  .chips { display: flex; gap: 6px; flex-wrap: wrap; padding: 6px 16px; background: #111b21; border-top: 1px solid #1f2c33; flex-shrink: 0; }
  .chip { background: #2a3942; border: 1px solid #3b4a54; color: #aebac1; font-size: 11px; padding: 4px 10px; border-radius: 14px; cursor: pointer; transition: background 0.15s; user-select: none; }
  .chip:hover { background: #3b4a54; color: #e9edef; }
  .chip-lang { border-color: #4a7c59; color: #7bc99a; }
  .chip-lang:hover { background: #1a3a25; }
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
  <span class="chip" onclick="quickSend('HELP')">HELP</span>
  <span class="chip chip-lang" onclick="quickSend('LANGUAGE')">🌐 LANGUAGE</span>
  <span class="chip" onclick="quickSend('TODAY')">TODAY</span>
  <span class="chip" onclick="quickSend('POST JOB')">POST JOB</span>
  <span class="chip" onclick="quickSend('MY JOBS')">MY JOBS</span>
  <span class="chip" onclick="quickSend('VIEW JOBS')">VIEW JOBS</span>
  <span class="chip" onclick="quickSend('SUBSIDIES')">SUBSIDIES</span>
  <span class="chip" onclick="quickSend('RENT EQUIPMENT')">RENT EQUIPMENT</span>
  <span class="chip" onclick="quickSend('VIEW EQUIPMENT')">VIEW EQUIPMENT</span>
  <span class="chip" onclick="quickSend('MY LABOURERS')">MY LABOURERS</span>
  <span class="chip" onclick="quickSend('MY FARMERS')">MY FARMERS</span>
  <span class="chip" onclick="quickSend('JOB HISTORY')">JOB HISTORY</span>
  <span class="chip" onclick="quickSend('MY DAYS')">MY DAYS</span>
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

  function escHtml(str) {
    let s = str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    s = s.replace(/(https?:\\/\\/[^\\s<]+)/g,
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
