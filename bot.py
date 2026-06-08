import io
import json
import re
import os
import asyncio
import httpx
import time
import requests
import secrets
import sqlite3
import threading
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)
log = logging.getLogger(__name__)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# =========================================================
# CONFIG
# =========================================================

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN      = _require_env("BOT_TOKEN")
STAFF_CHAT_ID  = -1003941910641
GROUP_LINK     = "https://t.me/cornballsv2"

LTC_ADDRESSES = [
    "ltc1qvyj99enxgk2n8akc42vehkh680hxnrhtcqwhg9",
    "ltc1qzpw5qpm3jdjvhmcfmpmrt2khknujvdf8aa6ghf",
    "ltc1q775l7da8y7j05dschhhnqqmdlpc2afjlx9z0hk",
    "ltc1qme0vvjsmktsemwv49txpkajq2zrkjdtvvz3zal",
]

ADMIN_IDS      = {8910478622}

LEAKOSINT_KEY  = _require_env("LEAKOSINT_KEY")
LEAKOSINT_URL  = "https://leakosintapi.com/"

OSINT_SX_KEY   = _require_env("OSINT_SX_KEY")
OSINT_SX_URL   = "https://api.osint.sx/search"
LIVE_OSINT_COST = 2
BREACH_FULL_COST = 1
ENDATO_AP_NAME     = _require_env("ENDATO_AP_NAME")
ENDATO_AP_PASSWORD = _require_env("ENDATO_AP_PASSWORD")
ENDATO_URL         = "https://devapi.enformion.com/PersonSearch"
ENDATO_PERSON_COST = 12

ORDER_TIMEOUT  = 10800  # 3 hours in seconds

# =========================================================
# DATABASE
# Three tables:
#   accounts — one row per user, holds serial code + credits
#   orders   — one row per order placed
#   topups   — one row per credit top-up request
# =========================================================

conn     = sqlite3.connect("bot.db", check_same_thread=False, timeout=30)
conn.execute("PRAGMA journal_mode=WAL")
cursor   = conn.cursor()
db_lock  = threading.RLock()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    serial_code TEXT,
    credits REAL DEFAULT 0,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    user_id INTEGER,
    serial_code TEXT,
    product TEXT,
    note TEXT,
    credit_cost INTEGER,
    status TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS topups (
    topup_id TEXT PRIMARY KEY,
    user_id INTEGER,
    serial_code TEXT,
    credits REAL,
    ltc_amount TEXT,
    ltc_address TEXT,
    tx_hash TEXT,
    status TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS vouchers (
    code TEXT PRIMARY KEY,
    credits REAL,
    used INTEGER DEFAULT 0,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS referral_redemptions (
    redeemer_id INTEGER PRIMARY KEY,
    referrer_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    redeemed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS last_searches (
    user_id INTEGER PRIMARY KEY,
    search_type TEXT NOT NULL,
    query TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")

try:
    cursor.execute("ALTER TABLE accounts ADD COLUMN referral_code TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass
cursor.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_referral_code ON accounts(referral_code)"
)
cursor.execute(
    "CREATE INDEX IF NOT EXISTS idx_credit_log_user ON credit_log(user_id, created_at DESC)"
)
conn.commit()

SEARCH_TYPE_LABELS = {
    "breach_free": "Breach Search (Free)",
    "breach_full": "Full Breach Search",
    "live": "Live OSINT Lookup",
    "endato": "Enformion Person Search",
}

REFERRAL_BONUS = 1
MAX_REFERRALS  = 15

# =========================================================
# CREDIT PACKAGES — edit names, credits, and LTC prices here
# =========================================================

CREDIT_PACKAGES = {
    "starter":  {"name": "Lite          — $5",    "credits": 10,  "price": "0.12 LTC"},
    "basic":    {"name": "Hobby         — $15",   "credits": 35,  "price": "0.36 LTC"},
    "standard": {"name": "Researcher    — $30",   "credits": 80,  "price": "0.71 LTC"},
    "pro":      {"name": "Investigator  — $85",   "credits": 200, "price": "2.02 LTC"},
    "elite":    {"name": "Enterprise    — $150",  "credits": 350, "price": "3.57 LTC"},
}

# =========================================================
# PRODUCTS — set credit_cost for each product
# 0 = free
# =========================================================

PRODUCT_CATEGORIES = {

    "breaches": {
        "name": "▶︎ Breaches & Exposure",
        "products": {
            "intelx": {"name": "IntelX Lookup",         "credit_cost": 1},
            "db":     {"name": "Data Breach Report",    "credit_cost": 5},
            "stl":    {"name": "Stealer Log Scan",      "credit_cost": 5},
            "logs":   {"name": "Website ULP Logs",      "credit_cost": 8},
        }
    },

    "people": {
        "name": "▶︎ Person Reports",
        "products": {
            "basic":         {"name": "Basic Person Search",    "credit_cost": 10},
            "comprehensive": {"name": "Comprehensive Report",   "credit_cost": 28},
            "full":          {"name": "Full Background Report", "credit_cost": 74},
            "uk":            {"name": "UK Person Lookup",       "credit_cost": 70},
            "can":           {"name": "Canada Person Lookup",   "credit_cost": 12},
            "property":      {"name": "Property Records",       "credit_cost": 3},
            "nab":           {"name": "Neighbourhood Profile",  "credit_cost": 8},
            "bnd":           {"name": "Birth / Death Records",  "credit_cost": 8},
            "deeds":         {"name": "Deeds Records",          "credit_cost": 8},
            "div":           {"name": "Divorce / Marriage",     "credit_cost": 12},
            "crim":          {"name": "Criminal Profile",       "credit_cost": 12},
            "work":          {"name": "Workplace Records",      "credit_cost": 18},
            "corp":          {"name": "Corporate Records",      "credit_cost": 20},
            "civil":         {"name": "Civil Judgments",        "credit_cost": 20},
        }
    },

    "osint": {
        "name": "▶︎ OSINT Lookups",
        "products": {
            "phone":    {"name": "Social Catfish Report",    "credit_cost": 0},
            "email":    {"name": "Osint.Industries Report",  "credit_cost": 1},
            "osintsx":  {"name": "Osint.sx Report",          "credit_cost": 1},
            "aihoto":   {"name": "AI Photo Geo-Location",    "credit_cost": 40},
            "aiperson": {"name": "AI Person Search",         "credit_cost": 35},
            "discord":  {"name": "Discord ID Lookup",        "credit_cost": 2},
            "emaphone": {"name": "Phone / Email Lookup",     "credit_cost": 4},
            "website":  {"name": "Website Analysis",         "credit_cost": 10},
        }
    },

    "csint": {
        "name": "▶︎ CSINT Lookups",
        "products": {
            "npd":     {"name": "NPD",                 "credit_cost": 15},
            "ssn":     {"name": "SSN + DOB",           "credit_cost": 20},
            "tlo":     {"name": "TLO",                 "credit_cost": 25},
            "dl":      {"name": "DL Lookup",           "credit_cost": 32},
            "hunt":    {"name": "Hunt & Fish LN",      "credit_cost": 14},
            "creport": {"name": "Credit Report",       "credit_cost": 35},
            "prop":    {"name": "Propstream Skiptrace","credit_cost": 20},
            "bank":    {"name": "Bankruptcy Records",  "credit_cost": 8},
            "dea":     {"name": "DEA LN",              "credit_cost": 8},
        }
    },

}

# =========================================================
# STATE TRACKING
# These dicts track what step each user is currently on
# =========================================================

order_drafts  = {}  # user_id -> order info while placing an order
status_waiting = {} # user_id -> True while waiting for order ID input
lookup_waiting = {}  # user_id -> lookup mode string
lookup_pages = {}
voucher_waiting = {}

# =========================================================
# ACCOUNT HELPERS
# =========================================================

def gen_referral_code():
    return "REF-" + secrets.token_hex(4).upper()


def ensure_referral_code(user_id):
    """Return the user's referral code, creating one if needed."""
    row = cursor.execute(
        "SELECT referral_code FROM accounts WHERE user_id=?", (user_id,)
    ).fetchone()
    if row and row[0]:
        return row[0]

    while True:
        code = gen_referral_code()
        if not cursor.execute(
            "SELECT 1 FROM accounts WHERE referral_code=?", (code,)
        ).fetchone():
            break

    cursor.execute(
        "UPDATE accounts SET referral_code=? WHERE user_id=?", (code, user_id)
    )
    conn.commit()
    return code


def get_referral_count(referrer_id):
    """How many people this user has successfully referred."""
    row = cursor.execute(
        "SELECT COUNT(*) FROM referral_redemptions WHERE referrer_id=?",
        (referrer_id,),
    ).fetchone()
    return row[0] if row else 0


def try_redeem_referral(redeemer_id, code):
    """
    Redeem a referral code. Returns (True, referrer_id) on success,
    or (False, reason) where reason is 'not_referral', 'own_code',
    'already_used', or 'referrer_limit'.
    """
    code = code.strip().upper()
    referrer_row = cursor.execute(
        "SELECT user_id FROM accounts WHERE referral_code=?", (code,)
    ).fetchone()
    if not referrer_row:
        return False, "not_referral"

    referrer_id = referrer_row[0]
    if referrer_id == redeemer_id:
        return False, "own_code"

    if cursor.execute(
        "SELECT 1 FROM referral_redemptions WHERE redeemer_id=?", (redeemer_id,)
    ).fetchone():
        return False, "already_used"

    if get_referral_count(referrer_id) >= MAX_REFERRALS:
        return False, "referrer_limit"

    add_credits(redeemer_id, REFERRAL_BONUS, "Referral bonus (redeemed code)")
    add_credits(referrer_id, REFERRAL_BONUS, "Referral bonus (code used)")
    cursor.execute(
        "INSERT INTO referral_redemptions VALUES (?,?,?,?)",
        (redeemer_id, referrer_id, code, int(time.time())),
    )
    conn.commit()
    return True, referrer_id


def create_account(user_id):
    """Create a new account with a random serial code and referral code."""
    serial = secrets.token_hex(6).upper()
    ref_code = gen_referral_code()
    while cursor.execute(
        "SELECT 1 FROM accounts WHERE referral_code=?", (ref_code,)
    ).fetchone():
        ref_code = gen_referral_code()

    cursor.execute(
        "INSERT OR IGNORE INTO accounts "
        "(user_id, serial_code, credits, created_at, referral_code) "
        "VALUES (?, ?, 0, ?, ?)",
        (user_id, serial, int(time.time()), ref_code),
    )
    conn.commit()

def get_account(user_id):
    """Get account row. Returns dict with serial_code and credits."""
    create_account(user_id)  # creates only if doesn't exist
    row = cursor.execute(
        "SELECT serial_code, credits FROM accounts WHERE user_id=?",
        (user_id,)
    ).fetchone()
    return {"serial_code": row[0], "credits": row[1]}

def get_balance(user_id):
    """Return credit balance as a number."""
    row = cursor.execute(
        "SELECT credits FROM accounts WHERE user_id=?", (user_id,)
    ).fetchone()
    return row[0] if row else 0

def deduct_credits(user_id, amount, reason=None):
    """Remove credits. Returns True if successful, False if not enough."""
    if get_balance(user_id) < amount:
        return False
    cursor.execute(
        "UPDATE accounts SET credits = credits - ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()
    if reason:
        log_credit(user_id, -amount, reason)
    return True


def log_credit(user_id, amount, reason):
    with db_lock:
        cursor.execute(
            "INSERT INTO credit_log (user_id, amount, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, amount, reason, int(time.time())),
        )
        conn.commit()


def get_credit_log(user_id, limit=20):
    return cursor.execute(
        "SELECT amount, reason, created_at FROM credit_log "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def format_credit_log(user_id, limit=20):
    rows = get_credit_log(user_id, limit)
    if not rows:
        return "📊 CREDIT LOG\n\nNo activity yet."
    text = "📊 CREDIT LOG\n\n"
    for amount, reason, created_at in rows:
        ts = time.strftime("%d/%m/%Y %H:%M", time.localtime(created_at))
        sign = "+" if amount >= 0 else ""
        text += f"{sign}{amount:g} — {reason}\n📅 {ts}\n\n"
    return text.rstrip()


def save_last_search(user_id, search_type, query, result_count):
    with db_lock:
        cursor.execute(
            "INSERT OR REPLACE INTO last_searches VALUES (?, ?, ?, ?, ?)",
            (user_id, search_type, query[:500], result_count, int(time.time())),
        )
        conn.commit()


def get_last_search(user_id):
    return cursor.execute(
        "SELECT search_type, query, result_count, created_at "
        "FROM last_searches WHERE user_id=?",
        (user_id,),
    ).fetchone()


def format_last_search(user_id):
    row = get_last_search(user_id)
    if not row:
        return "🔎 LAST SEARCH\n\nNo searches yet."
    search_type, query, result_count, created_at = row
    label = SEARCH_TYPE_LABELS.get(search_type, search_type)
    ts = time.strftime("%d/%m/%Y %H:%M", time.localtime(created_at))
    return (
        f"🔎 LAST SEARCH\n\n"
        f"Type: {label}\n"
        f"Query: {query}\n"
        f"Results: {result_count}\n"
        f"Date: {ts}"
    )


def is_maintenance():
    row = cursor.execute(
        "SELECT value FROM settings WHERE key='maintenance'"
    ).fetchone()
    return row and row[0] == "1"


def set_maintenance(enabled):
    with db_lock:
        cursor.execute(
            "INSERT OR REPLACE INTO settings VALUES ('maintenance', ?)",
            ("1" if enabled else "0",),
        )
        conn.commit()


def add_credits(user_id, amount, reason=None):
    """Add credits to account."""
    cursor.execute(
        "UPDATE accounts SET credits = credits + ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()
    if reason:
        log_credit(user_id, amount, reason)


def get_free_address():
    """Return an address not currently tied to a pending topup, or None if all busy."""
    busy = {
        row[0] for row in cursor.execute(
            "SELECT ltc_address FROM topups WHERE status='Awaiting Payment'"
        ).fetchall()
    }
    for addr in LTC_ADDRESSES:
        if addr not in busy:
            return addr
    return None


def ltc_to_satoshis(ltc_str):
    """Convert '0.12 LTC' string to satoshis integer."""
    return int(float(ltc_str.split()[0]) * 1e8)


def is_banned(user_id):
    return get_balance(user_id) <= -999999


def _topup_status(topup_id):
    with db_lock:
        row = cursor.execute(
            "SELECT status FROM topups WHERE topup_id=?", (topup_id,)
        ).fetchone()
    return row[0] if row else None


def _complete_topup(topup_id, tx_hash, user_id, credits):
    """Mark topup completed and add credits atomically. Returns True if credited."""
    with db_lock:
        already_used = cursor.execute(
            "SELECT topup_id FROM topups WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        if already_used:
            return False

        cursor.execute(
            "UPDATE topups SET status='Completed', tx_hash=? "
            "WHERE topup_id=? AND status='Awaiting Payment'",
            (tx_hash, topup_id),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return False

        add_credits(user_id, credits, f"Top-up payment ({topup_id})")
        conn.commit()
    return True


async def poll_payment(
    app, topup_id, ltc_address, expected_ltc, user_id, credits, deadline=None
):
    """Poll Blockcypher every 60s until payment confirmed or timeout."""
    if deadline is None:
        deadline = time.time() + ORDER_TIMEOUT
    expected_sat = ltc_to_satoshis(expected_ltc)

    while time.time() < deadline:
        if _topup_status(topup_id) != "Awaiting Payment":
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"https://api.blockcypher.com/v1/ltc/main/addrs/{ltc_address}/full?limit=10"
                )
            r.raise_for_status()
            data = r.json()

            for tx in data.get("txs", []):
                if tx.get("confirmations", 0) < 1:
                    continue
                tx_hash = tx["hash"]

                for output in tx.get("outputs", []):
                    if ltc_address not in output.get("addresses", []):
                        continue
                    if abs(output["value"] - expected_sat) >= 10000:
                        continue

                    if _complete_topup(topup_id, tx_hash, user_id, credits):
                        new_bal = get_balance(user_id)
                        await app.bot.send_message(
                            user_id,
                            f"✅ PAYMENT CONFIRMED\n\n"
                            f"⚡ +{credits} credits added\n"
                            f"💰 New balance: {new_bal} credits\n\n"
                            f"🆔 Top Up ID: {topup_id}",
                        )
                        await app.bot.send_message(
                            STAFF_CHAT_ID,
                            f"✅ AUTO PAYMENT CONFIRMED\n\n"
                            f"🆔 {topup_id}\n"
                            f"👤 {user_id}\n"
                            f"⚡ {credits} credits\n"
                            f"🔗 {tx_hash}",
                        )
                        return
                    if _topup_status(topup_id) != "Awaiting Payment":
                        return
        except Exception as e:
            log.exception("[poller] %s error: %s", topup_id, e)

        await asyncio.sleep(60)

    with db_lock:
        cursor.execute(
            "UPDATE topups SET status='Expired' "
            "WHERE topup_id=? AND status='Awaiting Payment'",
            (topup_id,),
        )
        conn.commit()
    if _topup_status(topup_id) == "Expired":
        await app.bot.send_message(
            user_id,
            f"⌛ TOP UP EXPIRED\n\n🆔 {topup_id}\n\n"
            f"Payment not received in time. Please create a new top up.",
        )


async def resume_pending_topups(app):
    """Resume polling for topups that were pending when the bot last stopped."""
    with db_lock:
        rows = cursor.execute(
            "SELECT topup_id, ltc_address, ltc_amount, user_id, credits, created_at "
            "FROM topups WHERE status='Awaiting Payment'"
        ).fetchall()

    now = time.time()
    for topup_id, ltc_addr, ltc_amount, user_id, credits, created_at in rows:
        deadline = created_at + ORDER_TIMEOUT
        if now >= deadline:
            with db_lock:
                cursor.execute(
                    "UPDATE topups SET status='Expired' "
                    "WHERE topup_id=? AND status='Awaiting Payment'",
                    (topup_id,),
                )
                conn.commit()
            continue

        app.create_task(
            poll_payment(
                app, topup_id, ltc_addr, ltc_amount, user_id, credits, deadline
            )
        )



# =========================================================
# ORDER / ID HELPERS
# =========================================================

def gen_order_id():
    return "ORD-" + secrets.token_hex(4).upper()

def gen_topup_id():
    return "TOP-" + secrets.token_hex(4).upper()

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_product(key):
    """Find a product by key across all categories."""
    for category in PRODUCT_CATEGORIES.values():
        if key in category["products"]:
            return category["products"][key]
    return None

# =========================================================
# LEAKOSINT LOOKUP
# =========================================================

SAFE_FIELDS = {
    "Email", "Nick", "Surname", "FullName", "Phone", "Phone2",
    "Address", "City", "State", "Country", "PostCode", "IP",
    "Username", "DOB", "Gender", "Password", "EncryptedPassword",
    "JobTitle", "Company", "Status", "LastActivity", "FacebookId",
    "TwitterId", "InstagramId", "LinkedInId", "Avatar", "ISP", "Carrier",
}

# Fields blocked on the free breach search only
FREE_BLOCKED_LABELS = {
    "Password", "Hashed Password", "SSN", "ID", "DLN", "DOB",
}

# Map API field names → display labels
BREACH_FIELD_ALIASES = {
    "SSN": "SSN", "Ssn": "SSN", "ssn": "SSN",
    "SocialSecurityNumber": "SSN", "Social_Security": "SSN",
    "SocialSecurity": "SSN",
    "DLN": "DLN", "Dln": "DLN", "dln": "DLN",
    "DLNumber": "DLN", "LicenseNumber": "DLN",
    "DriverLicenseNumber": "DLN", "DriversLicenseNumber": "DLN",
    "DriverLicense": "Driver License", "DriversLicense": "Driver License",
    "DL": "Driver License", "Dl": "Driver License",
    "License": "Driver License", "DrivingLicense": "Driver License",
    "Drivers_License": "Driver License",
    "Password": "Password", "pass": "Password", "Pass": "Password",
    "passwd": "Password",
    "EncryptedPassword": "Hashed Password", "PasswordHash": "Hashed Password",
    "HashedPassword": "Hashed Password", "Hash": "Hashed Password",
    "ID": "ID", "Id": "ID", "UserId": "ID", "UserID": "ID",
    "NationalId": "ID", "NationalID": "ID", "IdentityId": "ID",
    "DocumentId": "ID",
    "Passport": "Passport", "PassportNumber": "Passport",
    "PassportNo": "Passport", "Passport_ID": "Passport",
    "DOB": "DOB", "Dob": "DOB", "dob": "DOB",
    "DateOfBirth": "DOB", "BirthDate": "DOB", "Birthday": "DOB",
}

FULL_BREACH_PRIORITY = (
    "SSN", "DLN", "Driver License", "Passport",
    "Password", "Hashed Password", "ID",
)


def normalize_breach_field(key):
    return BREACH_FIELD_ALIASES.get(key, key)


def is_free_blocked_field(key):
    return normalize_breach_field(key) in FREE_BLOCKED_LABELS


def ordered_breach_fields(record):
    """Return fields for full breach display, priority fields first."""
    fields = {}
    for key, val in record.items():
        if not val:
            continue
        label = normalize_breach_field(key)
        fields[label] = val

    ordered = []
    for label in FULL_BREACH_PRIORITY:
        if label in fields:
            ordered.append((label, fields.pop(label)))
    for label in sorted(fields.keys()):
        ordered.append((label, fields[label]))
    return ordered


def leakosint_records(data):
    """Extract (database, record) tuples from a LeakOSINT response."""
    return [
        (db, rec)
        for db, d in data.get("List", {}).items()
        for rec in d.get("Data", [])
    ]

def leakosint_search(query):
    payload = {"token": LEAKOSINT_KEY, "request": query, "limit": 100, "lang": "en"}
    response = requests.post(LEAKOSINT_URL, json=payload, timeout=60)
    return response.json()


def detect_module_type(query):
    """Guess osint.sx moduleType from user input. Returns (module_type, identifier)."""
    q = query.strip()
    if "@" in q:
        return "email", q
    digits = q.replace("+", "").replace(" ", "").replace("-", "")
    if q.startswith("+") and digits.isdigit():
        return "phone", q
    if digits.isdigit() and len(digits) >= 7:
        return "phone", "+" + digits
    if " " in q:
        return "name", q
    return "username", q


def _flatten_value(val, depth=0):
    """Turn nested dict/list values into readable strings."""
    if depth > 2:
        return str(val)[:120]
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        s = str(val)
        return s if len(s) <= 200 else s[:200] + "..."
    if isinstance(val, dict):
        parts = []
        for k, v in val.items():
            if k.startswith("__"):
                continue
            flat = _flatten_value(v, depth + 1)
            if flat:
                parts.append(f"{k}={flat}")
        return ", ".join(parts) if parts else None
    if isinstance(val, list):
        parts = [_flatten_value(v, depth + 1) for v in val[:5]]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None
    return str(val)[:120]


def osint_sx_search(module_type, identifier):
    """Stream osint.sx search and return hits with platform data."""
    resp = requests.post(
        OSINT_SX_URL,
        headers={"x-api-key": OSINT_SX_KEY, "content-type": "application/json"},
        json={"moduleType": module_type, "identifier": identifier},
        stream=True,
        timeout=120,
    )
    if resp.status_code == 401:
        raise ValueError("Invalid API key")
    if resp.status_code == 402:
        raise ValueError("API credits exhausted — contact support")
    if resp.status_code == 429:
        raise ValueError("Rate limit reached — try again in a minute")
    if resp.status_code == 503:
        raise ValueError("Server busy — try again shortly")
    resp.raise_for_status()

    hits = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        data = json.loads(line)
        if "progress" in data or "totalModules" in data:
            continue
        if data.get("error"):
            continue
        result = data.get("result", {})
        if result.get("type") == "NO_SUCH_USER":
            continue
        if result.get("show") or result.get("full"):
            hits.append(data)
    return hits


ENDATO_INCLUDES = [
    "Addresses", "PhoneNumbers", "EmailAddresses", "RelativesSummary",
    "AssociatesSummary", "Indicators", "DatesOfBirth", "MergedNames",
    "SocialSecurityNumbers", "DriverLicenseRecords",
]


def endato_api_error(resp):
    """Extract a readable error message from an Endato API response."""
    try:
        data = resp.json()
    except Exception:
        return resp.text[:200] or f"HTTP {resp.status_code}"
    if data.get("isError"):
        err = data.get("error") or {}
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or str(err)
            inputs = err.get("inputErrors")
            if inputs:
                msg = f"{msg} ({inputs[0]})"
            return msg
        return str(err)
    return resp.text[:200] or f"HTTP {resp.status_code}"


def parse_endato_query(text):
    """Parse user input into an Endato PersonSearch payload. Returns (payload, search_type)."""
    q = text.strip()
    payload = {
        "Includes": ENDATO_INCLUDES,
        "Page": 1,
        "ResultsPerPage": 100,
    }
    search_type = "Person"

    if "@" in q:
        payload["Email"] = q
        return payload, search_type

    phone_digits = re.sub(r"\D", "", q)
    if re.match(r"^[\d\s\-\(\)\+\.]+$", q) and len(phone_digits) >= 10:
        payload["Phone"] = q
        return payload, "ReversePhonePerson"

    dob_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*$", q)
    if dob_match:
        payload["Dob"] = dob_match.group(1)
        q = q[:dob_match.start()].strip()

    if "," in q:
        parts = [p.strip() for p in q.split(",")]
        name_words = parts[0].split()
        if len(name_words) >= 1:
            payload["FirstName"] = name_words[0]
        if len(name_words) >= 2:
            payload["LastName"] = name_words[-1]
        if len(name_words) >= 3:
            payload["MiddleName"] = " ".join(name_words[1:-1])
        if len(parts) >= 2:
            payload["Addresses"] = [{"AddressLine2": ", ".join(parts[1:])}]
        return payload, search_type

    words = q.split()
    if len(words) >= 2:
        payload["FirstName"] = words[0]
        payload["LastName"] = words[-1]
        if len(words) >= 3:
            payload["MiddleName"] = " ".join(words[1:-1])
        return payload, search_type

    if len(words) == 1:
        payload["LastName"] = words[0]
        return payload, search_type

    raise ValueError(
        "Could not parse search. Examples:\n"
        "• John Smith, Sacramento, CA\n"
        "• John Smith 1/1/1980\n"
        "• john@gmail.com\n"
        "• 916-555-1234"
    )


def endato_person_search(payload, search_type="Person"):
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "galaxy-ap-name": ENDATO_AP_NAME,
        "galaxy-ap-password": ENDATO_AP_PASSWORD,
        "galaxy-search-type": search_type,
    }
    resp = requests.post(ENDATO_URL, headers=headers, json=payload, timeout=90)
    if not resp.ok:
        raise ValueError(endato_api_error(resp))
    data = resp.json()
    if data.get("isError"):
        raise ValueError(endato_api_error(resp))
    return data.get("persons") or []


def _endato_name(person):
    n = person.get("name") or {}
    parts = [n.get("firstName"), n.get("middleName"), n.get("lastName"), n.get("suffix")]
    return " ".join(p for p in parts if p) or "Unknown"


def _endato_list(items, key, limit=3):
    vals = []
    for item in items or []:
        if isinstance(item, dict):
            val = item.get(key)
        else:
            val = item
        if val:
            vals.append(str(val))
    if len(vals) > limit:
        return ", ".join(vals[:limit]) + f" (+{len(vals) - limit} more)"
    return ", ".join(vals) if vals else None


def format_endato_person_brief(person):
    name = _endato_name(person)
    age = person.get("age")
    dob = person.get("dob")
    tahoe = person.get("tahoeId", "")

    header = f"👤 {name}"
    if age:
        header += f" (Age {age})"
    elif dob:
        header += f" (DOB {dob})"
    text = header + "\n"

    if tahoe:
        text += f"• ID: {tahoe}\n"

    phones = _endato_list(person.get("phoneNumbers"), "phoneNumber")
    if phones:
        text += f"• Phones: {phones}\n"

    emails = _endato_list(person.get("emailAddresses"), "emailAddress")
    if emails:
        text += f"• Emails: {emails}\n"

    addrs = person.get("addresses") or []
    if addrs:
        addr = addrs[0].get("fullAddress") or addrs[0].get("city", "")
        if addr:
            text += f"• Address: {addr}\n"
        if len(addrs) > 1:
            text += f"• +{len(addrs) - 1} more address(es)\n"

    relatives = person.get("relativesSummary") or []
    if relatives:
        rel_names = []
        for r in relatives[:3]:
            rel_names.append(
                " ".join(p for p in [r.get("firstName"), r.get("lastName")] if p)
            )
        rel_line = ", ".join(rel_names)
        if len(relatives) > 3:
            rel_line += f" (+{len(relatives) - 3} more)"
        text += f"• Relatives: {rel_line}\n"

    indicators = person.get("indicators") or {}
    active = [k.replace("has", "") for k, v in indicators.items() if v and str(v) != "0"]
    if active:
        text += f"• Records: {', '.join(active[:6])}\n"

    akas = person.get("akas") or []
    if akas:
        aka_names = []
        for a in akas[:2]:
            aka_names.append(
                " ".join(p for p in [a.get("firstName"), a.get("lastName")] if p)
            )
        if aka_names:
            text += f"• AKAs: {', '.join(aka_names)}"
            if len(akas) > 2:
                text += f" (+{len(akas) - 2} more)"
            text += "\n"

    return text + "━━━━━━━━━━\n"


def format_endato_person_full(person):
    text = f"👤 {_endato_name(person)}\n"
    if person.get("tahoeId"):
        text += f"Tahoe ID: {person['tahoeId']}\n"
    if person.get("age"):
        text += f"Age: {person['age']}\n"
    if person.get("dob"):
        text += f"DOB: {person['dob']}\n"

    for label, key, subkey in (
        ("Phones", "phoneNumbers", "phoneNumber"),
        ("Emails", "emailAddresses", "emailAddress"),
    ):
        items = _endato_list(person.get(key), subkey, limit=50)
        if items:
            text += f"{label}: {items}\n"

    for addr in person.get("addresses") or []:
        line = addr.get("fullAddress")
        if not line:
            parts = [
                addr.get("houseNumber"), addr.get("streetName"), addr.get("streetType"),
                addr.get("city"), addr.get("state"), addr.get("zip"),
            ]
            line = " ".join(p for p in parts if p)
        if line:
            text += f"Address: {line}\n"

    for r in person.get("relativesSummary") or []:
        rname = " ".join(
            p for p in [r.get("firstName"), r.get("middleName"), r.get("lastName")] if p
        )
        extra = f" ({r.get('relativeType')})" if r.get("relativeType") else ""
        if r.get("dob"):
            extra += f" DOB:{r['dob']}"
        if rname:
            text += f"Relative: {rname}{extra}\n"

    for a in person.get("associatesSummary") or []:
        aname = " ".join(p for p in [a.get("firstName"), a.get("lastName")] if p)
        if aname:
            text += f"Associate: {aname}\n"

    indicators = person.get("indicators") or {}
    active = [f"{k}: {v}" for k, v in indicators.items() if v and str(v) != "0"]
    if active:
        text += "Indicators: " + ", ".join(active) + "\n"

    for a in person.get("akas") or []:
        aname = " ".join(
            p for p in [a.get("firstName"), a.get("middleName"), a.get("lastName")] if p
        )
        if aname:
            text += f"AKA: {aname}\n"

    return text + "\n"


def format_endato_page(persons, page):
    total = len(persons)
    pages = max(1, -(-total // 10))
    text = f"Enformion Person Search — Page {page + 1}/{pages} — {total} results\n\n"
    for person in persons[page * 10 : page * 10 + 10]:
        text += format_endato_person_brief(person)
    return text[:4000]


def format_endato_txt(query, persons):
    text = f"ENFORMION PERSON SEARCH — {query}\n{'=' * 40}\n\n"
    for person in persons:
        text += format_endato_person_full(person)
        text += "-" * 40 + "\n"
    return text


def format_breach_free_page(records, page):
    total = len(records)
    pages = max(1, -(-total // 10))
    text = f"Breach Search — Page {page + 1}/{pages} — {total} results\n"
    text += "🔒 Free preview — sensitive IDs hidden\n\n"
    for db_name, record in records[page * 10 : page * 10 + 10]:
        text += f"📂 {db_name}\n"
        hidden = 0
        for key, val in record.items():
            if not val:
                continue
            if is_free_blocked_field(key):
                hidden += 1
                continue
            label = normalize_breach_field(key)
            text += f"• {label}: {val}\n"
        if hidden:
            text += (
                f"• 🔒 {hidden} sensitive field(s) hidden "
                f"(password, SSN, ID, DLN, DOB) — unlock with Full Breach (1 credit)\n"
            )
        text += "━━━━━━━━━━\n"
    return text[:4000]


def format_breach_full_page(records, page):
    total = len(records)
    pages = max(1, -(-total // 10))
    text = f"Full Breach Search — Page {page + 1}/{pages} — {total} results\n\n"
    for db_name, record in records[page * 10 : page * 10 + 10]:
        text += f"📂 {db_name}\n"
        for key, val in ordered_breach_fields(record):
            text += f"• {key}: {val}\n"
        text += "━━━━━━━━━━\n"
    return text[:4000]


def format_breach_free_txt(query, records):
    text = f"BREACH SEARCH (FREE) — {query}\n{'=' * 40}\n\n"
    for db, record in records:
        text += f"[{db}]\n"
        for key, val in record.items():
            if not val or is_free_blocked_field(key):
                continue
            label = normalize_breach_field(key)
            text += f"  {label}: {val}\n"
        text += "\n"
    text += (
        "\nHidden on free: Password, Hashed Password, SSN, ID, DLN, DOB.\n"
        "Upgrade to Full Breach Search (1 credit) for all fields.\n"
    )
    return text


def format_breach_full_txt(query, records):
    text = f"FULL BREACH SEARCH — {query}\n{'=' * 40}\n\n"
    for db, record in records:
        text += f"[{db}]\n"
        for key, val in ordered_breach_fields(record):
            text += f"  {key}: {val}\n"
        text += "\n"
    return text


def format_qosint_page(records, page):
    return format_breach_free_page(records, page)


def format_live_hit(hit):
    class_name = hit.get("className", "unknown")
    result = hit.get("result", {})
    text = f"📂 {class_name}\n"
    seen_keys = set()

    for section in ("show", "full"):
        block = result.get(section)
        if not isinstance(block, dict):
            continue
        for key, val in block.items():
            if key.startswith("__") or key in seen_keys:
                continue
            flat = _flatten_value(val)
            if not flat:
                continue
            seen_keys.add(key)
            text += f"• {key}: {flat}\n"
    return text + "━━━━━━━━━━\n"


def format_live_page(records, page):
    total = len(records)
    pages = max(1, -(-total // 10))
    text = f"Live OSINT Lookup — Page {page + 1}/{pages} — {total} hits\n\n"
    for hit in records[page * 10 : page * 10 + 10]:
        text += format_live_hit(hit)
    return text[:4000]


def format_lookup_page(state, page):
    kind = state.get("kind")
    records = state["records"]
    if kind == "live":
        return format_live_page(records, page)
    if kind == "breach_full":
        return format_breach_full_page(records, page)
    if kind == "endato":
        return format_endato_page(records, page)
    return format_breach_free_page(records, page)


def format_page(records, page):
    return format_qosint_page(records, page)

def build_lookup_download(state):
    """Build filename, text content, and caption for a lookup download."""
    kind = state.get("kind")
    query = state.get("query", "")
    records = state["records"]

    if kind == "breach_free":
        return (
            "breach_preview.txt",
            format_breach_free_txt(query, records),
            "📥 Free breach preview",
        )
    if kind == "breach_full":
        return (
            "full_breach_results.txt",
            format_breach_full_txt(query, records),
            "📥 Full breach results",
        )
    if kind == "live":
        module_type = state.get("module_type", "")
        text = f"LIVE OSINT LOOKUP — {query} ({module_type})\n{'=' * 40}\n\n"
        for hit in records:
            text += format_live_hit(hit) + "\n"
        return (
            "live_osint_results.txt",
            text,
            f"Live OSINT Lookup results ({len(records)} hits)",
        )
    if kind == "endato":
        return (
            "enformion_person_search.txt",
            format_endato_txt(query, records),
            f"Enformion Person Search results ({len(records)} people)",
        )
    return None


def page_keyboard(user_id):
    page = lookup_pages[user_id]["page"]
    total = len(lookup_pages[user_id]["records"])
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀", callback_data="lp"))
    if (page + 1) * 10 < total:
        row.append(InlineKeyboardButton("▶", callback_data="ln"))
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("📥 Download Results", callback_data="lookup_dl")])
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="back")])
    return InlineKeyboardMarkup(buttons)


def lookup_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Breach Search — FREE", callback_data="lookup_breach_free")],
        [InlineKeyboardButton(f"Full Breach Search — {BREACH_FULL_COST} credit", callback_data="lookup_breach_full")],
        [InlineKeyboardButton(f"Live OSINT Lookup — {LIVE_OSINT_COST} credits", callback_data="lookup_live")],
        [InlineKeyboardButton(f"Enformion Person Search — {ENDATO_PERSON_COST} credits", callback_data="lookup_endato")],
        [InlineKeyboardButton("⬅ Back", callback_data="back")],
    ])

# =========================================================
# TEXT STRINGS
# =========================================================

WELCOME_TEXT = """
◇ Welcome to v2 ◇

▸ Premium lookup & investigative services.
▸ Use the menu below to begin.
"""

INFO_TEXT = """
ℹ️ INFORMATION

How to use bot:
1. You need to type /start - it will automatically make you an account when you do.
2. Go into your account by clicking on the button, then click top-up.
3. Top up the amount of credits you want. You need to top-up at least $5 worth of credits.
4. After you send the LTC, credits are added automatically once the payment confirms on-chain (usually a few minutes).

NOTE: 1 credit is valued at 0.50 cents USD.

If you don't understand what to do or want to learn more about a specific lookup, feel free to DM us.
I can't get too specific as we don't want to get this bot banned.
⚠️ No fake serial codes. Abuse = restricted access.

💬 Support: @SlowlyFallingDown
💬 Channel: @cornballsv2
💬 Chat: @cornballschat
"""

FAQ_TEXT = """
❓ FAQ

Q: How do I get free items?
A: Just click them — 0 credits needed.

Q: How long do orders take?
A: Usually within 30 minutes when staff are online, the OSINT lookup is instant.

Q: What goes in the search term?
A: Exactly what you want searched — name, email, username, ID etc.

Q: How do I top up credits?
A: My Account → Top Up Credits → pick a package → send LTC.

Q: Is this legal?
A: For lawful use only. You are responsible for how you use results.
"""

# =========================================================
# MENUS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Order", callback_data="buy"), InlineKeyboardButton("🔎 Lookup", callback_data="lookup")],
        [InlineKeyboardButton("👤 My Account", callback_data="account"), InlineKeyboardButton("ℹ️ Info", callback_data="info")],
        [InlineKeyboardButton("💬 Group", url=GROUP_LINK)],
    ])

def back(dest="back"):
    """Single back button pointing to a destination."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=dest)]])

def buy_menu():
    """Category list."""
    buttons = [
        [InlineKeyboardButton(cat["name"], callback_data=f"cat_{key}")]
        for key, cat in PRODUCT_CATEGORIES.items()
    ]
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="back")])
    return InlineKeyboardMarkup(buttons)

def category_menu(cat_key):
    """Product list inside a category."""
    products = PRODUCT_CATEGORIES[cat_key]["products"]
    buttons = []
    for key, p in products.items():
        cost = f"{p['credit_cost']} credits" if p["credit_cost"] > 0 else "FREE"
        buttons.append([InlineKeyboardButton(f"{p['name']} — {cost}", callback_data=f"buy_{key}")])
    buttons.append([InlineKeyboardButton("⬅ Categories", callback_data="buy")])
    return InlineKeyboardMarkup(buttons)

def topup_menu():
    """Credit package list."""
    buttons = [
        [InlineKeyboardButton(
            f"{pkg['name']} — {pkg['credits']} credits ({pkg['price']})",
            callback_data=f"topup_{key}"
        )]
        for key, pkg in CREDIT_PACKAGES.items()
    ]
    buttons.append([InlineKeyboardButton("⬅ Back", callback_data="account")])
    return InlineKeyboardMarkup(buttons)

# =========================================================
# /start COMMAND
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_account(update.effective_user.id)
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu())

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_account(update.effective_user.id)
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu())

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acc = get_account(update.effective_user.id)
    await update.message.reply_text(
        f"👤 YOUR ACCOUNT\n\n"
        f"🔑 Serial Code: {acc['serial_code']}\n"
        f"💳 Credits:     {acc['credits']}"
    )

async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛒 Select a Category", reply_markup=buy_menu())

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Type /menu to get started.")

# =========================================================
# BUTTON HANDLER
# Handles every inline button tap
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = q.from_user
    await q.answer()
    if is_banned(user.id):
        await q.answer("⛔ You are banned.", show_alert=True)
        return

    # ── Main menu ──────────────────────────────────────────
    if q.data == "back":
        await q.edit_message_text(WELCOME_TEXT, reply_markup=main_menu())

    # ── Category list ──────────────────────────────────────
    elif q.data == "buy":
        await q.edit_message_text("🛒 Select a Category", reply_markup=buy_menu())

    # ── Open a category ────────────────────────────────────
    elif q.data.startswith("cat_"):
        key = q.data[4:]
        cat = PRODUCT_CATEGORIES.get(key)
        if cat:
            await q.edit_message_text(f"📂 {cat['name']}", reply_markup=category_menu(key))


    # ── My Account ─────────────────────────────────────────
    elif q.data == "account":
        acc = get_account(user.id)
        await q.edit_message_text(
            f"👤 MY ACCOUNT\n\n"
            f"🔑 Serial Code: {acc['serial_code']}\n"
            f"💳 Credits: {acc['credits']}\n\n"
            f"Use your serial code when placing orders.\n"
            f"Top up below to add credits.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Top Up Credits", callback_data="topup")],
                [InlineKeyboardButton("🎟 Redeem Voucher", callback_data="voucher")],
                [InlineKeyboardButton("🔗 Refer", callback_data="refer")],
                [InlineKeyboardButton("📋 Order Status", callback_data="status")],
                [InlineKeyboardButton("📊 Credit Log", callback_data="credit_log")],
                [InlineKeyboardButton("🔎 Last Search", callback_data="last_search")],
                [InlineKeyboardButton("⬅ Back", callback_data="back")],
            ]),
        )

    elif q.data == "credit_log":
        await q.edit_message_text(
            format_credit_log(user.id),
            reply_markup=back("account"),
        )

    elif q.data == "last_search":
        await q.edit_message_text(
            format_last_search(user.id),
            reply_markup=back("account"),
        )

    elif q.data == "refer":
        code = ensure_referral_code(user.id)
        used = get_referral_count(user.id)
        remaining = max(0, MAX_REFERRALS - used)
        max_credits = MAX_REFERRALS * REFERRAL_BONUS
        await q.edit_message_text(
            f"🔗 REFER A FRIEND\n\n"
            f"Your referral code:\n`{code}`\n\n"
            f"Share this code with friends.\n"
            f"When they redeem it under 🎟 Redeem Voucher,\n"
            f"you both get {REFERRAL_BONUS} credit!\n\n"
            f"📊 Referrals: {used}/{MAX_REFERRALS} "
            f"({remaining} remaining)\n"
            f"💰 Max earnings: {max_credits} credits (${max_credits})\n\n"
            f"⚠️ Each person can only use one referral code.",
            reply_markup=back("account"),
            parse_mode="Markdown",
        )

    # ── Top Up: show packages ──────────────────────────────
    elif q.data == "topup":
        await q.edit_message_text("💳 Choose a Credit Package:", reply_markup=topup_menu())

    elif q.data.startswith("topup_"):
        key = q.data[6:]
        pkg = CREDIT_PACKAGES.get(key)
        if not pkg:
            return

        ltc_addr = get_free_address()
        if not ltc_addr:
            await q.edit_message_text(
                "⌛ All payment slots are currently busy.\n\nPlease try again in a few minutes.",
                reply_markup=back("account"),
            )
            return

        acc = get_account(user.id)
        topup_id = gen_topup_id()
        expires = int(time.time()) + ORDER_TIMEOUT

        with db_lock:
            cursor.execute(
                "INSERT INTO topups VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    topup_id, user.id, acc["serial_code"], pkg["credits"],
                    pkg["price"], ltc_addr, None, "Awaiting Payment", int(time.time()),
                ),
            )
            conn.commit()

        context.application.create_task(
            poll_payment(
                context.application, topup_id, ltc_addr, pkg["price"], user.id, pkg["credits"]
            )
        )

        await q.edit_message_text(
            f"💳 TOP UP ORDER CREATED\n\n"
            f"🆔 Top Up ID:  {topup_id}\n"
            f"⚡ Credits:    {pkg['credits']}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"💸 Send LTC to:\n`{ltc_addr}`\n\n"
            f"💰 Exact amount:\n`{pkg['price']}`\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"⌛ Expires: {time.strftime('%H:%M:%S', time.localtime(expires))}\n\n"
            f"✅ Credits will be added automatically once payment is confirmed.",
            reply_markup=back("account"),
            parse_mode="Markdown",
        )

        await context.bot.send_message(
            STAFF_CHAT_ID,
            f"💳 NEW TOP UP\n\n"
            f"🆔 {topup_id}\n"
            f"👤 @{user.username}\n"
            f"🔑 {acc['serial_code']}\n"
            f"⚡ {pkg['credits']} credits\n"
            f"💰 {pkg['price']}\n"
            f"📬 {ltc_addr}",
        )

    # ── OSINT Lookup ────────────────────────────────────────
    elif q.data == "lookup":
        await q.edit_message_text(
            "LOOKUP\n\n"
            "Choose a search type:\n\n"
            "Free Breach Search — leak database scan (FREE, censored)\n"
            f"Full Breach Search — all fields ({BREACH_FULL_COST} credit)\n"
            f"OSINT.SX Lookup — live platform scan ({LIVE_OSINT_COST} credits)\n"
            f"Enformion Person Search ({ENDATO_PERSON_COST} credits)",
            reply_markup=lookup_menu(),
        )

    elif q.data == "lookup_breach_free":
        lookup_waiting[user.id] = "breach_free"
        await q.edit_message_text(
            "BREACH SEARCH (FREE)\n\n"
            "Send what you want to search:\n"
            "• Email\n• Phone\n• Username\n• Full Name\n• IP Address\n\n"
            "Example: john@gmail.com",
            reply_markup=back("lookup"),
        )

    elif q.data == "lookup_breach_full":
        if is_maintenance():
            await q.edit_message_text(
                "🛠 Bot is under maintenance.\n\nPaid lookups are temporarily unavailable.",
                reply_markup=back("lookup"),
            )
            return
        acc = get_account(user.id)
        if acc["credits"] < BREACH_FULL_COST:
            await q.edit_message_text(
                f"❌ NOT ENOUGH CREDITS\n\n"
                f"Full Breach Search costs {BREACH_FULL_COST} credit.\n"
                f"Your balance: {acc['credits']} credits.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Top Up", callback_data="topup")],
                    [InlineKeyboardButton("⬅ Back", callback_data="lookup")],
                ]),
            )
            return
        lookup_waiting[user.id] = "breach_full"
        await q.edit_message_text(
            f"FULL BREACH SEARCH ({BREACH_FULL_COST} credit)\n\n"
            "Uncensored — all fields shown including:\n"
            "• SSN • DLN • Driver License • Passport\n"
            "• Password • Hashed Password • ID\n"
            "• Email, phone, address & more\n\n"
            "Send what you want to search:\n"
            "• Email\n• Phone\n• Username\n• Full Name\n• IP Address\n\n"
            "Example: john@gmail.com",
            reply_markup=back("lookup"),
        )

    elif q.data == "lookup_live":
        if is_maintenance():
            await q.edit_message_text(
                "🛠 Bot is under maintenance.\n\nPaid lookups are temporarily unavailable.",
                reply_markup=back("lookup"),
            )
            return
        acc = get_account(user.id)
        if acc["credits"] < LIVE_OSINT_COST:
            await q.edit_message_text(
                f"❌ NOT ENOUGH CREDITS\n\n"
                f"Live OSINT Lookup costs {LIVE_OSINT_COST} credits.\n"
                f"Your balance: {acc['credits']} credits.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Top Up", callback_data="topup")],
                    [InlineKeyboardButton("⬅ Back", callback_data="lookup")],
                ]),
            )
            return
        lookup_waiting[user.id] = "live"
        await q.edit_message_text(
            f"LIVE OSINT LOOKUP ({LIVE_OSINT_COST} credits)\n\n"
            "Send what you want to search:\n"
            "• Email — john@gmail.com\n"
            "• Phone — +14155552671 (+E.164 format)\n"
            "• Username — johnsmith\n"
            "• Full Name — John Smith\n\n"
            f"💳 Cost: {LIVE_OSINT_COST} credits per search\n"
            "⏳ May take up to a minute",
            reply_markup=back("lookup"),
        )

    elif q.data == "lookup_endato":
        if is_maintenance():
            await q.edit_message_text(
                "🛠 Bot is under maintenance.\n\nPaid lookups are temporarily unavailable.",
                reply_markup=back("lookup"),
            )
            return
        acc = get_account(user.id)
        if acc["credits"] < ENDATO_PERSON_COST:
            await q.edit_message_text(
                f"❌ NOT ENOUGH CREDITS\n\n"
                f"Enformion Person Search costs {ENDATO_PERSON_COST} credits.\n"
                f"Your balance: {acc['credits']} credits.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Top Up", callback_data="topup")],
                    [InlineKeyboardButton("⬅ Back", callback_data="lookup")],
                ]),
            )
            return
        lookup_waiting[user.id] = "endato"
        await q.edit_message_text(
            f"ENFORMION PERSON SEARCH ({ENDATO_PERSON_COST} credits)\n\n"
            "Send search info — more detail = better results:\n\n"
            "• Name + Location:\n"
            "  John Smith, Sacramento, CA\n\n"
            "• Name + DOB:\n"
            "  John Smith 1/1/1980\n\n"
            "• Email:\n"
            "  john@gmail.com\n\n"
            "• Phone:\n"
            "  916-555-1234",
            reply_markup=back("lookup"),
        )

    # ── Order Status ────────────────────────────────────────
    elif q.data == "status":
        status_waiting[user.id] = True
        await q.edit_message_text("📋 Enter your Order ID:", reply_markup=back())
        return

    elif q.data == "voucher":
        voucher_waiting[user.id] = True
        await q.edit_message_text(
            "🎟 Enter your voucher or referral code:",
            reply_markup=back("account"),
        )
        return

    # ── Info ────────────────────────────────────────────────
    elif q.data == "info":
        await q.edit_message_text(
            INFO_TEXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❓ FAQ", callback_data="faq")],
                [InlineKeyboardButton("⬅ Back", callback_data="back")],
            ]),
        )

    elif q.data == "faq":
        await q.edit_message_text(FAQ_TEXT, reply_markup=back("info"))

    elif q.data in ("lp", "ln"):
        state = lookup_pages.get(user.id)
        if not state:
            return
        state["page"] += -1 if q.data == "lp" else 1
        await q.edit_message_text(
            format_lookup_page(state, state["page"]),
            reply_markup=page_keyboard(user.id),
        )

    elif q.data == "lookup_dl":
        state = lookup_pages.get(user.id)
        if not state:
            await q.answer("No results to download.", show_alert=True)
            return
        download = build_lookup_download(state)
        if not download:
            await q.answer("Download unavailable.", show_alert=True)
            return
        filename, content, caption = download
        file = io.BytesIO(content.encode("utf-8"))
        file.name = filename
        await q.answer("Sending file...")
        await context.bot.send_document(
            chat_id=user.id,
            document=file,
            filename=filename,
            caption=caption,
        )

    # ── Buy a product ───────────────────────────────────────
    elif q.data.startswith("buy_"):
        if is_maintenance():
            await q.edit_message_text(
                "🛠 Bot is under maintenance.\n\nNew orders are temporarily unavailable.",
                reply_markup=back("buy"),
            )
            return
        key     = q.data[4:]
        product = get_product(key)
        if not product:
            return

        acc  = get_account(user.id)
        cost = product["credit_cost"]

        # Not enough credits
        if cost > 0 and acc["credits"] < cost:
            await q.edit_message_text(
                f"❌ NOT ENOUGH CREDITS\n\n"
                f"This item costs {cost} credits.\n"
                f"Your balance: {acc['credits']} credits.\n\n"
                f"Please top up first.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Top Up",  callback_data="topup")],
                    [InlineKeyboardButton("⬅ Back",     callback_data="buy")],
                ])
            )
            return

        # Start order — ask for search term first
        order_drafts[user.id] = {
            "step":        "note",
            "product":     product["name"],
            "credit_cost": cost,
        }

        await q.edit_message_text(
            f"🛒 ORDERING\n\n"
            f"📦 {product['name']}\n"
            f"💳 Cost: {cost} credits\n"
            f"💰 Your balance: {acc['credits']} credits\n\n"
            f"📝 Enter your search term:\n"
            f"(e.g. John Smith / test@mail.com)",
            reply_markup=back("buy")
        )

# =========================================================
# MESSAGE HANDLER
# Handles text messages during active flows
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    if is_banned(user.id):
        await update.message.reply_text("⛔ You are banned.")
        return
    if user.id in voucher_waiting:
        voucher_waiting.pop(user.id)
        code = text.strip().upper()

        ok, info = try_redeem_referral(user.id, code)
        if ok:
            referrer_id = info
            await update.message.reply_text(
                f"✅ Referral redeemed!\n\n"
                f"⚡ +{REFERRAL_BONUS} credit added\n"
                f"💰 New balance: {get_balance(user.id)} credits"
            )
            await context.bot.send_message(
                referrer_id,
                f"🔗 REFERRAL USED\n\n"
                f"Someone redeemed your code `{code}`!\n"
                f"⚡ +{REFERRAL_BONUS} credit added\n"
                f"💰 New balance: {get_balance(referrer_id)} credits",
                parse_mode="Markdown",
            )
            return
        if info == "own_code":
            await update.message.reply_text("❌ You can't use your own referral code.")
            return
        if info == "already_used":
            await update.message.reply_text("❌ You've already redeemed a referral code.")
            return
        if info == "referrer_limit":
            await update.message.reply_text(
                "❌ This referral code has reached its limit (15 referrals)."
            )
            return

        row = cursor.execute(
            "SELECT credits, used FROM vouchers WHERE code=?", (code,)
        ).fetchone()
        if not row:
            await update.message.reply_text("❌ Invalid code.")
            return
        if row[1] == 1:
            await update.message.reply_text("❌ That voucher has already been used.")
            return
        add_credits(user.id, row[0], f"Voucher redeemed ({code})")
        cursor.execute("UPDATE vouchers SET used=1 WHERE code=?", (code,))
        conn.commit()
        await update.message.reply_text(
            f"✅ Voucher redeemed!\n\n"
            f"⚡ +{row[0]} credits added\n"
            f"💰 New balance: {get_balance(user.id)} credits"
        )
        return

    if user.id in status_waiting:
        status_waiting.pop(user.id)

        order_id = text.strip().upper()

        row = cursor.execute(
            """
            SELECT order_id, product, status, created_at
            FROM orders
            WHERE order_id=? AND user_id=?
            """,
            (order_id, user.id)
        ).fetchone()

        if not row:
            await update.message.reply_text("❌ Order not found.")
            return

        created = time.strftime(
            "%d/%m/%Y %H:%M",
            time.localtime(row[3])
        )

        await update.message.reply_text(
            f"📋 ORDER STATUS\n\n"
            f"🆔 {row[0]}\n"
            f"📦 {row[1]}\n"
            f"📌 Status: {row[2]}\n"
            f"📅 Created: {created}"
        )
        return

    # ── Order Flow ──────────────────────────────────────────
    if user.id in order_drafts:
        draft = order_drafts[user.id]

        # Step 1: user sends their search term
        if draft["step"] == "note":
            draft["note"] = text
            draft["step"] = "serial"
            await update.message.reply_text(
                "🔑 Enter your Serial Code to confirm the order:\n"
                "(Find it in My Account)"
            )
            return

        # Step 2: user sends their serial code
        elif draft["step"] == "serial":
            acc    = get_account(user.id)
            serial = text.strip().upper()

            # Wrong serial code
            if serial != acc["serial_code"]:
                await update.message.reply_text(
                    "❌ Wrong serial code. Check My Account and try again."
                )
                order_drafts.pop(user.id)
                return

            cost = draft["credit_cost"]

            # Final balance check
            if cost > 0 and acc["credits"] < cost:
                await update.message.reply_text(
                    f"❌ Not enough credits. Need {cost}, you have {acc['credits']}."
                )
                order_drafts.pop(user.id)
                return

            # Deduct credits
            if cost > 0:
                deduct_credits(user.id, cost, f"Order: {draft['product']}")

            # Save order
            order_id = gen_order_id()
            cursor.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                (order_id, user.id, serial, draft["product"], draft["note"],
                 cost, "Awaiting Delivery", int(time.time()))
            )
            conn.commit()

            new_balance = get_balance(user.id)

            await update.message.reply_text(
                f"✅ ORDER PLACED\n\n"
                f"🆔 {order_id} - SAVE THIS ORDER ID ⚠️\n"
                f"📦 {draft['product']}\n"
                f"📝 {draft['note']}\n"
                f"💳 Credits used: {cost}\n"
                f"💰 New balance: {new_balance} credits",
                reply_markup=main_menu()
            )

            await context.bot.send_message(
                STAFF_CHAT_ID,
                f"🆕 NEW ORDER\n\n"
                f"🆔 {order_id}\n"
                f"👤 @{update.effective_user.username}\n"
                f"🔑 {serial}\n"
                f"📦 {draft['product']}\n"
                f"📝 {draft['note']}\n"
                f"💳 {cost} credits"
            )

            order_drafts.pop(user.id)
            return


    # ── OSINT Lookup ────────────────────────────────────────

    if user.id in lookup_waiting:
        mode = lookup_waiting.pop(user.id)
        query = text.strip()

        if mode == "breach_free":
            msg = await update.message.reply_text("Searching breach databases...")
            try:
                data = await asyncio.to_thread(leakosint_search, query)
                records = leakosint_records(data)
                if not records:
                    await msg.edit_text("❌ No results found.")
                    return
                save_last_search(user.id, "breach_free", query, len(records))
                lookup_pages[user.id] = {
                    "records": records, "page": 0, "kind": "breach_free", "query": query,
                }
                await msg.edit_text(
                    format_breach_free_page(records, 0),
                    reply_markup=page_keyboard(user.id),
                )
            except Exception as e:
                await msg.edit_text(f"❌ Failed: {e}")
            return

        if mode == "breach_full":
            acc = get_account(user.id)
            if acc["credits"] < BREACH_FULL_COST:
                await update.message.reply_text(
                    f"❌ Not enough credits. Full Breach costs {BREACH_FULL_COST} credit."
                )
                return

            msg = await update.message.reply_text("Running full breach search...")
            try:
                if not deduct_credits(user.id, BREACH_FULL_COST, "Full Breach Search"):
                    await msg.edit_text(
                        f"❌ Not enough credits. Need {BREACH_FULL_COST} credit."
                    )
                    return

                data = await asyncio.to_thread(leakosint_search, query)
                records = leakosint_records(data)
                if not records:
                    add_credits(user.id, BREACH_FULL_COST, "Refund: Full Breach (no results)")
                    await msg.edit_text("❌ No results found. Credit refunded.")
                    return

                save_last_search(user.id, "breach_full", query, len(records))
                lookup_pages[user.id] = {
                    "records": records, "page": 0, "kind": "breach_full", "query": query,
                }
                await msg.edit_text(
                    format_breach_full_page(records, 0),
                    reply_markup=page_keyboard(user.id),
                )
            except Exception as e:
                add_credits(user.id, BREACH_FULL_COST, "Refund: Full Breach (error)")
                await msg.edit_text(f"❌ Failed: {e}\nCredit refunded.")
            return

        if mode == "live":
            acc = get_account(user.id)
            if acc["credits"] < LIVE_OSINT_COST:
                await update.message.reply_text(
                    f"❌ Not enough credits. Live OSINT Lookup costs {LIVE_OSINT_COST} credits."
                )
                return

            module_type, identifier = detect_module_type(query)

            msg = await update.message.reply_text(
                f"Live OSINT Lookup scanning ({module_type})...\n"
                "This may take up to a minute."
            )
            try:
                if not deduct_credits(user.id, LIVE_OSINT_COST, "Live OSINT Lookup"):
                    await msg.edit_text(
                        f"❌ Not enough credits. Need {LIVE_OSINT_COST} credits."
                    )
                    return

                hits = await asyncio.to_thread(osint_sx_search, module_type, identifier)
                if not hits:
                    add_credits(user.id, LIVE_OSINT_COST, "Refund: Live OSINT Lookup (no hits)")
                    await msg.edit_text("❌ No hits found. Credits refunded.")
                    return

                save_last_search(user.id, "live", query, len(hits))
                lookup_pages[user.id] = {
                    "records": hits, "page": 0, "kind": "live",
                    "query": query, "module_type": module_type,
                }
                await msg.edit_text(
                    format_live_page(hits, 0),
                    reply_markup=page_keyboard(user.id),
                )
            except Exception as e:
                add_credits(user.id, LIVE_OSINT_COST, "Refund: Live OSINT Lookup (error)")
                await msg.edit_text(f"❌ Failed: {e}\nCredits refunded.")
            return

        if mode == "endato":
            acc = get_account(user.id)
            if acc["credits"] < ENDATO_PERSON_COST:
                await update.message.reply_text(
                    f"❌ Not enough credits. Enformion Person Search costs {ENDATO_PERSON_COST} credits."
                )
                return

            msg = await update.message.reply_text("Running Enformion person search...")
            try:
                if not deduct_credits(user.id, ENDATO_PERSON_COST, "Enformion Person Search"):
                    await msg.edit_text(
                        f"❌ Not enough credits. Need {ENDATO_PERSON_COST} credits."
                    )
                    return

                payload, search_type = parse_endato_query(query)
                persons = await asyncio.to_thread(
                    endato_person_search, payload, search_type
                )
                if not persons:
                    add_credits(user.id, ENDATO_PERSON_COST, "Refund: Enformion Person Search (no results)")
                    await msg.edit_text("❌ No results found. Credits refunded.")
                    return

                save_last_search(user.id, "endato", query, len(persons))
                lookup_pages[user.id] = {
                    "records": persons, "page": 0, "kind": "endato", "query": query,
                }
                await msg.edit_text(
                    format_endato_page(persons, 0),
                    reply_markup=page_keyboard(user.id),
                )
            except Exception as e:
                add_credits(user.id, ENDATO_PERSON_COST, "Refund: Enformion Person Search (error)")
                await msg.edit_text(f"❌ Failed: {e}\nCredits refunded.")
            return

# =========================================================
# ADMIN COMMANDS
# =========================================================

async def addcredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addcredits <topup_id> — approve a top-up and add credits to user"""
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1:
        return await update.message.reply_text("/addcredits <topup_id>")

    row = cursor.execute(
        "SELECT user_id, credits, status FROM topups WHERE topup_id=?", (context.args[0],)
    ).fetchone()

    if not row:
        return await update.message.reply_text("Top-up not found.")

    user_id, credits, status = row
    if status == "Completed":
        return await update.message.reply_text("Top-up already completed.")
    if status != "Awaiting Payment":
        return await update.message.reply_text(f"Top-up status is '{status}', cannot approve.")

    add_credits(user_id, credits, f"Admin top-up ({context.args[0]})")
    cursor.execute(
        "UPDATE topups SET status='Completed' WHERE topup_id=? AND status='Awaiting Payment'",
        (context.args[0],),
    )
    conn.commit()

    new_balance = get_balance(user_id)
    await context.bot.send_message(
        user_id,
        f"💳 CREDITS ADDED\n\n⚡ +{credits} credits\n💰 New balance: {new_balance} credits\n\nThank you!"
    )
    await update.message.reply_text(f"Done. Added {credits} credits. New balance: {new_balance}.")

async def deliver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deliver <order_id> <message>"""
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        return await update.message.reply_text("/deliver <order_id> <message>")

    order_id = context.args[0]
    delivery = " ".join(context.args[1:])
    cursor.execute("UPDATE orders SET status='Delivered' WHERE order_id=?", (order_id,))
    conn.commit()

    row = cursor.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not row:
        return await update.message.reply_text("Order not found.")

    await context.bot.send_message(
        row[0],
        f"📦 ORDER DELIVERED\n\n🆔 {order_id}\n\n{delivery}",
        disable_web_page_preview=False
    )
    await update.message.reply_text("Delivered.")

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/approve <order_id>"""
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1:
        return await update.message.reply_text("/approve <order_id>")

    order_id = context.args[0]
    cursor.execute("UPDATE orders SET status='Approved' WHERE order_id=?", (order_id,))
    conn.commit()

    row = cursor.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row:
        await context.bot.send_message(row[0], f"✅ ORDER APPROVED\n🆔 {order_id}\n\nYour order is being processed.")
    await update.message.reply_text("Approved.")

async def deny_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deny <order_id>"""
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1:
        return await update.message.reply_text("/deny <order_id>")

    order_id = context.args[0]
    cursor.execute("UPDATE orders SET status='Denied' WHERE order_id=?", (order_id,))
    conn.commit()

    row = cursor.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row:
        await context.bot.send_message(row[0], f"❌ ORDER DENIED\n🆔 {order_id}")
    await update.message.reply_text("Denied.")

async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/send <order_id> <message>"""
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        return await update.message.reply_text("/send <order_id> <message>")

    order_id = context.args[0]
    msg      = " ".join(context.args[1:])
    row      = cursor.execute("SELECT user_id FROM orders WHERE order_id=?", (order_id,)).fetchone()

    if not row:
        return await update.message.reply_text("Order not found.")

    await context.bot.send_message(row[0], f"📩 ORDER UPDATE\n🆔 {order_id}\n\n{msg}")
    await update.message.reply_text("Sent.")

async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/orders — list last 20 orders"""
    if not is_admin(update.effective_user.id): return
    rows = cursor.execute(
        "SELECT order_id, serial_code, product, status FROM orders ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    if not rows:
        return await update.message.reply_text("No orders yet.")
    msg = "📦 RECENT ORDERS\n\n"
    for r in rows:
        msg += f"🆔 {r[0]}\n🔑 {r[1]}\n📦 {r[2]}\n📌 {r[3]}\n\n"
    await update.message.reply_text(msg)

async def removecredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2:
        return await update.message.reply_text("/removecredits <user_id> <amount>")
    user_id = int(context.args[0])
    amount  = float(context.args[1])
    if get_balance(user_id) < amount:
        return await update.message.reply_text("Not enough credits to remove.")
    deduct_credits(user_id, amount, "Admin credit remove")
    await update.message.reply_text(f"Removed {amount} credits. New balance: {get_balance(user_id)}")

async def customaddcredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2:
        return await update.message.reply_text("/customaddcredits <user_id> <amount>")
    user_id = int(context.args[0])
    amount  = float(context.args[1])
    add_credits(user_id, amount, "Admin credit add")
    await update.message.reply_text(f"Added {amount} credits. New balance: {get_balance(user_id)}")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1:
        return await update.message.reply_text("/ban <user_id>")
    user_id = int(context.args[0])
    cursor.execute("UPDATE accounts SET credits = -999999 WHERE user_id=?", (user_id,))
    conn.commit()
    await context.bot.send_message(user_id, "⛔ You have been banned.")
    await update.message.reply_text(f"User {user_id} banned.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1:
        return await update.message.reply_text("/unban <user_id>")
    user_id = int(context.args[0])
    cursor.execute("UPDATE accounts SET credits = 0 WHERE user_id=?", (user_id,))
    conn.commit()
    await context.bot.send_message(user_id, "✅ You have been unbanned.")
    await update.message.reply_text(f"User {user_id} unbanned.")

async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rows = cursor.execute(
        "SELECT user_id, serial_code, credits, created_at FROM accounts ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        return await update.message.reply_text("No members yet.")
    msg = f"👥 MEMBERS ({len(rows)} total)\n\n"
    for r in rows:
        joined = time.strftime('%d/%m/%Y', time.localtime(r[3]))
        msg += f"🆔 {r[0]}\n🔑 {r[1]}\n💳 {r[2]} credits\n📅 {joined}\n\n"
    await update.message.reply_text(msg[:4000])

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    t = time.localtime()
    day_start = int(time.mktime(
        (t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, t.tm_wday, t.tm_yday, t.tm_isdst)
    ))

    total_members = cursor.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    total_credits = cursor.execute(
        "SELECT COALESCE(SUM(credits), 0) FROM accounts WHERE credits > 0"
    ).fetchone()[0]
    topup_stats = cursor.execute(
        "SELECT COUNT(*), COALESCE(SUM(credits), 0) FROM topups WHERE status='Completed'"
    ).fetchone()
    topups_pending = cursor.execute(
        "SELECT COUNT(*) FROM topups WHERE status='Awaiting Payment'"
    ).fetchone()[0]
    orders_pending = cursor.execute(
        "SELECT COUNT(*) FROM orders WHERE status='Awaiting Delivery'"
    ).fetchone()[0]
    orders_total = cursor.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    spent_today = cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM credit_log "
        "WHERE amount < 0 AND created_at >= ?",
        (day_start,),
    ).fetchone()[0]
    added_today = cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM credit_log "
        "WHERE amount > 0 AND created_at >= ?",
        (day_start,),
    ).fetchone()[0]
    maintenance = "ON 🛠" if is_maintenance() else "OFF ✅"

    await update.message.reply_text(
        f"📊 BOT STATS\n\n"
        f"👥 Members: {total_members}\n"
        f"💳 Credits in circulation: {total_credits:g}\n\n"
        f"💰 Top-ups completed: {topup_stats[0]} ({topup_stats[1]:g} credits)\n"
        f"⏳ Top-ups pending: {topups_pending}\n\n"
        f"📦 Orders total: {orders_total}\n"
        f"📦 Orders pending: {orders_pending}\n\n"
        f"📅 Today — added: +{added_today:g} credits\n"
        f"📅 Today — spent: {spent_today:g} credits\n\n"
        f"🛠 Maintenance: {maintenance}"
    )


async def user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        return await update.message.reply_text("/user <user_id>")

    try:
        user_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid user ID.")

    row = cursor.execute(
        "SELECT serial_code, credits, created_at, referral_code "
        "FROM accounts WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row:
        return await update.message.reply_text("User not found.")

    serial, credits, created_at, ref_code = row
    joined = time.strftime("%d/%m/%Y", time.localtime(created_at))
    status = "⛔ Banned" if is_banned(user_id) else "✅ Active"
    ref_count = get_referral_count(user_id)

    msg = (
        f"👤 USER {user_id}\n\n"
        f"🔑 Serial: {serial}\n"
        f"💳 Credits: {credits}\n"
        f"📅 Joined: {joined}\n"
        f"📌 Status: {status}\n"
        f"🔗 Referral code: {ref_code or '—'}\n"
        f"🔗 Referrals used: {ref_count}/{MAX_REFERRALS}\n\n"
        f"{format_last_search(user_id)}\n\n"
        f"{format_credit_log(user_id, limit=10)}"
    )

    orders = cursor.execute(
        "SELECT order_id, product, status FROM orders "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
        (user_id,),
    ).fetchall()
    if orders:
        msg += "\n\n📦 RECENT ORDERS\n\n"
        for oid, product, ostatus in orders:
            msg += f"🆔 {oid}\n📦 {product}\n📌 {ostatus}\n\n"

    await update.message.reply_text(msg[:4000])


async def maintenance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1 or context.args[0].lower() not in ("on", "off"):
        status = "ON 🛠" if is_maintenance() else "OFF ✅"
        return await update.message.reply_text(
            f"Maintenance is currently {status}.\n\n"
            f"/maintenance on\n/maintenance off\n\n"
            f"When ON: paid lookups and new orders are blocked."
        )
    enabled = context.args[0].lower() == "on"
    set_maintenance(enabled)
    await update.message.reply_text(
        f"🛠 Maintenance mode {'enabled' if enabled else 'disabled'}."
    )


async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("""
🔧 ADMIN COMMANDS

/approve <order_id>
/deny <order_id>
/deliver <order_id> <message>
/send <order_id> <message>
/addcredits <topup_id>
/customaddcredits <user_id> <amount>
/removecredits <user_id> <amount>
/createvoucher <code> <credits>
/ban <user_id>
/unban <user_id>
/orders
/members
/stats
/user <user_id>
/maintenance on|off
/commands
""")

async def createvoucher_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2:
        return await update.message.reply_text("/createvoucher <code> <credits>")
    code    = context.args[0].upper()
    credits = float(context.args[1])
    cursor.execute(
        "INSERT OR REPLACE INTO vouchers VALUES (?, ?, 0, ?)",
        (code, credits, int(time.time()))
    )
    conn.commit()
    await update.message.reply_text(f"✅ Voucher created: {code} = {credits} credits")

# =========================================================
# RUN
# =========================================================

async def post_init(application):
    """Resume pending top-ups after the polling loop is running."""
    async def _resume_when_ready():
        await asyncio.sleep(2)
        await resume_pending_topups(application)

    asyncio.create_task(_resume_when_ready())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .connect_timeout(30)
    .read_timeout(30)
    .write_timeout(30)
    .post_init(post_init)
    .build()
)

# User commands
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Admin commands
app.add_handler(CommandHandler("addcredits", addcredits_cmd))
app.add_handler(CommandHandler("deliver",    deliver_cmd))
app.add_handler(CommandHandler("approve",    approve_cmd))
app.add_handler(CommandHandler("deny",       deny_cmd))
app.add_handler(CommandHandler("send",       send_cmd))
app.add_handler(CommandHandler("orders",     orders_cmd))
app.add_handler(CommandHandler("removecredits",    removecredits_cmd))
app.add_handler(CommandHandler("customaddcredits", customaddcredits_cmd))
app.add_handler(CommandHandler("ban",              ban_cmd))
app.add_handler(CommandHandler("unban", unban_cmd))
app.add_handler(CommandHandler("members", members_cmd))
app.add_handler(CommandHandler("stats", stats_cmd))
app.add_handler(CommandHandler("user", user_cmd))
app.add_handler(CommandHandler("maintenance", maintenance_cmd))
app.add_handler(CommandHandler("commands", commands_cmd))
app.add_handler(CommandHandler("createvoucher", createvoucher_cmd))
app.add_handler(CommandHandler("menu",             menu_cmd))
app.add_handler(CommandHandler("balance",          balance_cmd))
app.add_handler(CommandHandler("order",            order_cmd))
app.add_handler(MessageHandler(filters.COMMAND,    unknown_cmd))
app.add_error_handler(error_handler)

if __name__ == "__main__":
    log.info("Bot starting...")
    app.run_polling()
