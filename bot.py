import os
import time
import requests
import secrets
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# =========================================================
# CONFIG — change these values
# =========================================================

BOT_TOKEN      = os.environ["BOT_TOKEN"]
STAFF_CHAT_ID  = -1003941910641
GROUP_LINK     = "https://t.me/cornballsv2"
LTC_ADDRESS    = "ltc1qwzqh92kggfelh59f8jzud2qkxr8xemfu29mcrw"
ADMIN_IDS      = {8910478622}

LEAKOSINT_KEY  = os.environ["LEAKOSINT_KEY"]
LEAKOSINT_URL  = "https://leakosintapi.com/"

ORDER_TIMEOUT  = 10800  # 3 hours in seconds

# =========================================================
# DATABASE
# Three tables:
#   accounts — one row per user, holds serial code + credits
#   orders   — one row per order placed
#   topups   — one row per credit top-up request
# =========================================================

conn   = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS accounts (
    user_id     INTEGER PRIMARY KEY,
    serial_code TEXT UNIQUE,
    credits     REAL DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    user_id     INTEGER,
    serial_code TEXT,
    product     TEXT,
    note        TEXT,
    credit_cost INTEGER,
    status      TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS topups (
    topup_id   TEXT PRIMARY KEY,
    user_id    INTEGER,
    serial_code TEXT,
    credits    REAL,
    ltc_amount TEXT,
    status     TEXT,
    created_at INTEGER
);
""")
conn.commit()

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
            "leak":   {"name": "LeakOSINT",             "credit_cost": 0},
            "intelx": {"name": "IntelX Lookup",         "credit_cost": 1},
            "db":     {"name": "Data Breach Report",    "credit_cost": 20},
            "db":     {"name": "Stealer Log Scan",      "credit_cost": 20},
            "logs":   {"name": "Website ULP Logs",      "credit_cost": 15},
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
            "osintsx":  {"name": "Osint.xs Report",          "credit_cost": 4},
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
lookup_waiting = {} # user_id -> True while waiting for lookup query
lookup_pages = {}

# =========================================================
# ACCOUNT HELPERS
# =========================================================

def create_account(user_id):
    """Create a new account with a random serial code."""
    serial = secrets.token_hex(6).upper()
    cursor.execute(
        "INSERT OR IGNORE INTO accounts VALUES (?, ?, 0, ?)",
        (user_id, serial, int(time.time()))
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

def deduct_credits(user_id, amount):
    """Remove credits. Returns True if successful, False if not enough."""
    if get_balance(user_id) < amount:
        return False
    cursor.execute(
        "UPDATE accounts SET credits = credits - ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()
    return True

def add_credits(user_id, amount):
    """Add credits to account."""
    cursor.execute(
        "UPDATE accounts SET credits = credits + ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()

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

def leakosint_search(query):
    payload = {"token": LEAKOSINT_KEY, "request": query, "limit": 100, "lang": "en"}
    response = requests.post(LEAKOSINT_URL, json=payload, timeout=60)
    return response.json()

def format_page(records, page):
    total = len(records)
    pages = max(1, -(-total // 10))
    text  = f"🔎 Page {page+1}/{pages} — {total} results\n\n"
    for db_name, record in records[page*10 : page*10+10]:
        text += f"📂 {db_name}\n"
        for key, val in record.items():
            if key in SAFE_FIELDS and val:
                text += f"• {key}: {val}\n"
        text += "━━━━━━━━━━\n"
    return text[:4000]

def page_keyboard(user_id):
    page  = lookup_pages[user_id]["page"]
    total = len(lookup_pages[user_id]["records"])
    row   = []
    if page > 0:            row.append(InlineKeyboardButton("◀", callback_data="lp"))
    if (page+1)*10 < total: row.append(InlineKeyboardButton("▶", callback_data="ln"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("⬅ Back", callback_data="back")]])

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
3. Top up the ammount of credits you want. You need to top-up at least $5 worth of credits.
4. After you have sent the LTC, wait shorty. Once we review payment we will send your lookup.

NOTE: 1 credit is valued at 0.50 cents USD.


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


If you don't understand what to do or want to learn more about a specific lookup, feel free to DM us.
I can't get too specific as we don't want to get this bot banned.
⚠️ No fake serial codes. Abuse = restricted access.

💬 Support: @SlowlyFallingDown
💬 Channel: @cornballsv2
💬 Chat: @cornballschat
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

# =========================================================
# BUTTON HANDLER
# Handles every inline button tap
# =========================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = q.from_user
    await q.answer()

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
            f"🔑 Serial Code:  {acc['serial_code']}\n"
            f"💳 Credits:      {acc['credits']}\n\n"
            f"Use your serial code when placing orders.\n"
            f"Top up below to add credits.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Top Up Credits", callback_data="topup")],
                [InlineKeyboardButton("📋 Order Status",   callback_data="status")],
                [InlineKeyboardButton("⬅ Back",            callback_data="back")],
            ])
        )
    # ── Top Up: show packages ──────────────────────────────
    elif q.data == "topup":
        await q.edit_message_text("💳 Choose a Credit Package:", reply_markup=topup_menu())

    # ── Top Up: package selected ───────────────────────────
    elif q.data.startswith("topup_"):
        key = q.data[6:]
        pkg = CREDIT_PACKAGES.get(key)
        if not pkg:
            return

        acc      = get_account(user.id)
        topup_id = gen_topup_id()
        expires  = int(time.time()) + ORDER_TIMEOUT

        cursor.execute(
            "INSERT INTO topups VALUES (?,?,?,?,?,?,?)",
            (topup_id, user.id, acc["serial_code"], pkg["credits"], pkg["price"], "Awaiting Payment", int(time.time()))
        )
        conn.commit()

        await q.edit_message_text(
            f"💳 TOP UP ORDER CREATED\n\n"
            f"🆔 Top Up ID:  {topup_id}\n"
            f"⚡ Credits:    {pkg['credits']}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"💸 Send LTC to:\n{LTC_ADDRESS}\n\n"
            f"💰 Exact amount:\n{pkg['price']}\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"⌛ Expires: {time.strftime('%H:%M:%S', time.localtime(expires))}\n\n"
            f"Credits will be added once payment is confirmed.",
            reply_markup=back("account")
        )

        await context.bot.send_message(
            STAFF_CHAT_ID,
            f"💳 NEW TOP UP\n\n"
            f"🆔 {topup_id}\n"
            f"👤 @{user.username}\n"
            f"🔑 {acc['serial_code']}\n"
            f"⚡ {pkg['credits']} credits\n"
            f"💰 {pkg['price']}"
        )

    # ── OSINT Lookup ────────────────────────────────────────
    elif q.data == "lookup":
        lookup_waiting[user.id] = True
        await q.edit_message_text(
            "🔎 OSINT LOOKUP\n\n"
            "Send what you want to search:\n"
            "• Email\n• Phone\n• Username\n• Full Name\n• IP Address\n\n"
            "Example: john@gmail.com",
            reply_markup=back()
        )

    # ── Order Status ────────────────────────────────────────
    elif q.data == "status":
        status_waiting[user.id] = True
        await q.edit_message_text("📋 Enter your Order ID:", reply_markup=back())

    # ── Info ────────────────────────────────────────────────
    elif q.data == "info":
        await q.edit_message_text(INFO_TEXT, reply_markup=back())

    elif q.data in ("lp", "ln"):
        state = lookup_pages.get(user.id)
        if not state:
            return
        state["page"] += -1 if q.data == "lp" else 1
        await q.edit_message_text(format_page(state["records"], state["page"]), reply_markup=page_keyboard(user.id))

    # ── Buy a product ───────────────────────────────────────
    elif q.data.startswith("buy_"):
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
                deduct_credits(user.id, cost)

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
                f"🆔 {order_id}\n"
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
        lookup_waiting.pop(user.id)
        msg = await update.message.reply_text("🔎 Searching...")
        try:
            data    = leakosint_search(text)
            records = [(db, rec) for db, d in data.get("List", {}).items() for rec in d.get("Data", [])]
            lookup_pages[user.id] = {"records": records, "page": 0}
            await msg.edit_text(format_page(records, 0), reply_markup=page_keyboard(user.id))
        except Exception as e:
            await msg.edit_text(f"❌ Failed: {e}")
        return

    # ── Order Status ────────────────────────────────────────
    if user.id in status_waiting:
        status_waiting.pop(user.id)
        row = cursor.execute(
            "SELECT product, status FROM orders WHERE order_id=? AND user_id=?",
            (text.strip(), user.id)
        ).fetchone()
        if not row:
            await update.message.reply_text("❌ Order Error - Please check your order and try again.")
        else:
            await update.message.reply_text(
                f"📋 ORDER STATUS\n\n🆔 {text.strip()}\n📦 {row[0]}\n📌 {row[1]}",
                reply_markup=main_menu()
            )
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
        "SELECT user_id, credits FROM topups WHERE topup_id=?", (context.args[0],)
    ).fetchone()

    if not row:
        return await update.message.reply_text("Top-up not found.")

    user_id, credits = row
    add_credits(user_id, credits)
    cursor.execute("UPDATE topups SET status='Completed' WHERE topup_id=?", (context.args[0],))
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
    deduct_credits(user_id, amount)
    await update.message.reply_text(f"Removed {amount} credits. New balance: {get_balance(user_id)}")

async def customaddcredits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2:
        return await update.message.reply_text("/customaddcredits <user_id> <amount>")
    user_id = int(context.args[0])
    amount  = float(context.args[1])
    add_credits(user_id, amount)
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

# =========================================================
# RUN
# =========================================================

app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .connect_timeout(30)
    .read_timeout(30)
    .write_timeout(30)
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
app.add_handler(CommandHandler("members", members_cmd))

if __name__ == "__main__":
    print("🔥 v2 running...")
    app.run_polling()
