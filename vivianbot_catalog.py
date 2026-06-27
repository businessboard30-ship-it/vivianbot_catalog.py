import logging
import json
import os
import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
PAYSTACK_SECRET = os.environ["PAYSTACK_SECRET"]
ADMIN_ID        = 8162426062
CATALOG_FILE    = "catalog.json"
GRID_SIZE       = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── CONVERSATION STATES ───────────────────────────────────────────────────────
(
    ASK_NAME, ASK_AGE, ASK_LOCATION, ASK_COLOR,
    ASK_HEIGHT, ASK_HOURS, ASK_PHONE, ASK_PRICE, ASK_BIO,
) = range(9)

# ── CATALOG PERSISTENCE ───────────────────────────────────────────────────────
def load_catalog() -> list:
    if os.path.exists(CATALOG_FILE):
        with open(CATALOG_FILE, "r") as f:
            return json.load(f)
    return []

def save_catalog(data: list):
    with open(CATALOG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def add_listing(entry: dict) -> int:
    catalog = load_catalog()
    entry["id"] = max((e["id"] for e in catalog), default=0) + 1
    catalog.append(entry)
    save_catalog(catalog)
    return entry["id"]

def remove_listing(listing_id: int) -> bool:
    catalog = load_catalog()
    new = [e for e in catalog if e["id"] != listing_id]
    if len(new) == len(catalog):
        return False
    save_catalog(new)
    return True

def get_listing(listing_id: int) -> dict | None:
    return next((e for e in load_catalog() if e["id"] == listing_id), None)

# ── PAYSTACK ──────────────────────────────────────────────────────────────────
async def init_paystack(amount_ghs: float, label: str, email: str = "customer@vivianbot.com") -> dict | None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.paystack.co/transaction/initialize",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
                json={"email": email, "amount": int(amount_ghs * 100), "currency": "GHS", "metadata": {"listing": label}},
                timeout=15,
            )
            data = r.json()
            if data.get("status"):
                return {"url": data["data"]["authorization_url"], "reference": data["data"]["reference"]}
    except Exception as e:
        logging.error(f"Paystack init error: {e}")
    return None

async def verify_paystack(reference: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.paystack.co/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"},
                timeout=15,
            )
            return r.json().get("data", {}).get("status") == "success"
    except Exception as e:
        logging.error(f"Paystack verify error: {e}")
    return False

# ── CARD TEXT ─────────────────────────────────────────────────────────────────
def build_card_text(e: dict) -> str:
    return "\n".join([
        f"╔══════════════════════╗",
        f"  ✦ *{e['name'].upper()}*  •  {e['age']} yrs",
        f"╚══════════════════════╝",
        f"",
        f"📍 *Location* ›  {e['location']}",
        f"🎨 *Color* ›  {e['color']}",
        f"📏 *Height* ›  {e['height']}",
        f"⏱ *Available* ›  {e['hours']}",
        f"",
        f"💭 _{e['bio']}_",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"💎  *RATE:  GHS {e['price']}*",
        f"━━━━━━━━━━━━━━━━━━━━━━",
    ])

# ── CARD KEYBOARD (futuristic) ────────────────────────────────────────────────
def build_card_keyboard(e: dict) -> InlineKeyboardMarkup:
    lid   = e["id"]
    phone = e["phone"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ BOOK NOW",      callback_data=f"order|{lid}"),
        ],
        [
            InlineKeyboardButton("📲 CALL",           url=f"tel:{phone}"),
            InlineKeyboardButton("💬 MESSAGE",         url=f"https://t.me/+{phone}"),
        ],
        [
            InlineKeyboardButton("◀  BACK TO CATALOG", callback_data="back|0"),
        ],
    ])

# ── WELCOME KEYBOARD ──────────────────────────────────────────────────────────
def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 BROWSE CATALOG",    callback_data="page|0")],
        [
            InlineKeyboardButton("📞 CONTACT US",     url="https://t.me/+233000000000"),  # update with your number
            InlineKeyboardButton("ℹ️ HOW IT WORKS",   callback_data="howto"),
        ],
    ])

# ── GRID DISPLAY ─────────────────────────────────────────────────────────────
async def send_grid(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    catalog = load_catalog()
    if not catalog:
        await ctx.bot.send_message(chat_id, "😔 No listings yet. Check back soon!")
        return

    start = page * GRID_SIZE
    chunk = catalog[start: start + GRID_SIZE]
    total = len(catalog)

    # Album of thumbnails
    media = []
    for i, e in enumerate(chunk):
        cap = f"✦ {e['name']}, {e['age']}  |  {e['location']}" if i == 0 else f"✦ {e['name']}, {e['age']}  |  {e['location']}"
        media.append(InputMediaPhoto(media=e["photo_id"], caption=cap))

    try:
        await ctx.bot.send_media_group(chat_id, media=media)
    except Exception as err:
        logging.error(f"Media group error: {err}")

    # Name selector buttons — 2 per row, futuristic style
    name_buttons = []
    row = []
    for e in chunk:
        row.append(InlineKeyboardButton(
            f"✦ {e['name'].upper()}  {e['age']}",
            callback_data=f"profile|{e['id']}"
        ))
        if len(row) == 2:
            name_buttons.append(row)
            row = []
    if row:
        name_buttons.append(row)

    # Nav row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀  PREV",   callback_data=f"page|{page-1}"))
    if start + GRID_SIZE < total:
        nav.append(InlineKeyboardButton("NEXT  ▶",   callback_data=f"page|{page+1}"))
    if nav:
        name_buttons.append(nav)

    await ctx.bot.send_message(
        chat_id,
        f"◈  *{start+1} – {min(start+GRID_SIZE, total)} of {total} AVAILABLE*  ◈\n"
        f"_Tap a name to view full profile_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(name_buttons),
    )

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    first = user.first_name or "there"
    await update.message.reply_text(
        f"🌟 *Welcome, {first}!*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"You've entered an *exclusive catalog*.\n"
        f"Browse, choose, and book in seconds.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_What would you like to do?_",
        parse_mode="Markdown",
        reply_markup=welcome_keyboard(),
    )

async def catalog_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_grid(update.effective_chat.id, ctx, page=0)

# ── /list & /delete (admin) ───────────────────────────────────────────────────
async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    catalog = load_catalog()
    if not catalog:
        await update.message.reply_text("No listings yet.")
        return
    lines = [f"📋 *All Listings ({len(catalog)})*\n"]
    for e in catalog:
        lines.append(f"• ID `{e['id']}` — *{e['name']}*, {e['age']}, {e['location']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/delete <listing_id>`\nUse /list to see IDs.", parse_mode="Markdown")
        return
    lid   = int(args[0])
    entry = get_listing(lid)
    if not entry:
        await update.message.reply_text(f"❌ No listing with ID {lid} found.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirmdelete|{lid}"),
        InlineKeyboardButton("❌ Cancel",       callback_data="canceldelete"),
    ]])
    await update.message.reply_text(
        f"⚠️ Delete *{entry['name']}* (ID: {lid})?",
        parse_mode="Markdown", reply_markup=kb,
    )

# ── ADMIN UPLOAD FLOW ─────────────────────────────────────────────────────────
async def handle_admin_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("👋 Use /start to browse.")
        return ConversationHandler.END
    ctx.user_data["photo_id"] = update.message.photo[-1].file_id
    ctx.user_data["listing"]  = {}
    await update.message.reply_text("📸 Photo received!\n\n➡️ *Her name?*", parse_mode="Markdown")
    return ASK_NAME

async def ask_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["name"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Age?*", parse_mode="Markdown")
    return ASK_AGE

async def ask_age(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["age"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Location?*", parse_mode="Markdown")
    return ASK_LOCATION

async def ask_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["location"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Color / complexion?*", parse_mode="Markdown")
    return ASK_COLOR

async def ask_color(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["color"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Height?*", parse_mode="Markdown")
    return ASK_HEIGHT

async def ask_height(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["height"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Hours available? (e.g. 9am–10pm)*", parse_mode="Markdown")
    return ASK_HOURS

async def ask_hours(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["hours"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Phone number? (with country code e.g. 233241234567)*", parse_mode="Markdown")
    return ASK_PHONE

async def ask_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["phone"] = update.message.text.strip()
    await update.message.reply_text("➡️ *Price? (GHS — numbers only)*", parse_mode="Markdown")
    return ASK_PRICE

async def ask_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", "")
    try:
        ctx.user_data["listing"]["price"] = float(raw)
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number.")
        return ASK_PRICE
    await update.message.reply_text("➡️ *Short bio / description?*", parse_mode="Markdown")
    return ASK_BIO

async def ask_bio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["bio"]      = update.message.text.strip()
    ctx.user_data["listing"]["photo_id"] = ctx.user_data["photo_id"]
    e       = ctx.user_data["listing"]
    e["id"] = 0
    preview = build_card_text(e)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ SAVE",   callback_data="savelisting"),
        InlineKeyboardButton("❌ CANCEL", callback_data="cancellisting"),
    ]])
    await update.message.reply_photo(
        photo=e["photo_id"],
        caption=f"*── PREVIEW ──*\n\n{preview}\n\nSave this listing?",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return ConversationHandler.END

async def cancel_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Upload cancelled.")
    ctx.user_data.clear()
    return ConversationHandler.END

# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    uid   = query.from_user.id

    if data == "savelisting":
        if uid != ADMIN_ID:
            return
        entry = ctx.user_data.get("listing", {})
        if not entry:
            await query.message.reply_text("⚠️ No listing data. Please re-upload.")
            return
        lid = add_listing(entry)
        await query.message.reply_text(f"✅ *{entry['name']}* saved! ID: `{lid}`", parse_mode="Markdown")
        ctx.user_data.clear()

    elif data == "cancellisting":
        if uid != ADMIN_ID:
            return
        ctx.user_data.clear()
        await query.message.reply_text("❌ Listing cancelled.")

    elif data.startswith("confirmdelete|"):
        if uid != ADMIN_ID:
            return
        lid   = int(data.split("|")[1])
        entry = get_listing(lid)
        name  = entry["name"] if entry else str(lid)
        if remove_listing(lid):
            await query.message.edit_text(f"🗑️ *{name}* (ID: {lid}) removed.", parse_mode="Markdown")
        else:
            await query.message.edit_text(f"⚠️ Listing {lid} not found.")

    elif data == "canceldelete":
        await query.message.edit_text("👌 Delete cancelled.")

    elif data == "howto":
        await ctx.bot.send_message(
            uid,
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ *HOW IT WORKS*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣  Browse the catalog\n"
            "2️⃣  Tap a name to view her full profile\n"
            "3️⃣  Tap *⚡ BOOK NOW* to pay securely\n"
            "4️⃣  After payment, you'll be contacted\n\n"
            "📞 Questions? Tap *CONTACT US* on the main menu.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 BROWSE CATALOG", callback_data="page|0")
            ]])
        )

    elif data.startswith("page|"):
        page = int(data.split("|")[1])
        await send_grid(query.message.chat_id, ctx, page=page)

    elif data.startswith("back|"):
        page = int(data.split("|")[1])
        await send_grid(query.message.chat_id, ctx, page=page)

    elif data.startswith("profile|"):
        lid   = int(data.split("|")[1])
        entry = get_listing(lid)
        if not entry:
            await ctx.bot.send_message(uid, "⚠️ This listing is no longer available.")
            return
        await ctx.bot.send_photo(
            uid,
            photo=entry["photo_id"],
            caption=build_card_text(entry),
            parse_mode="Markdown",
            reply_markup=build_card_keyboard(entry),
        )

    elif data.startswith("order|"):
        lid   = int(data.split("|")[1])
        entry = get_listing(lid)
        if not entry:
            await ctx.bot.send_message(uid, "⚠️ This listing is no longer available.")
            return
        await ctx.bot.send_message(uid, f"⚡ _Generating your payment link for *{entry['name']}*…_", parse_mode="Markdown")
        txn = await init_paystack(entry["price"], entry["name"])
        if not txn:
            await ctx.bot.send_message(uid, "⚠️ Payment setup failed. Try again later.")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳  PAY SECURELY NOW", url=txn["url"])],
            [InlineKeyboardButton("✅  I'VE PAID — VERIFY", callback_data=f"verify|{txn['reference']}|{lid}")],
            [InlineKeyboardButton("◀  BACK TO PROFILE",   callback_data=f"profile|{lid}")],
        ])
        await ctx.bot.send_message(
            uid,
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *BOOKING:  {entry['name'].upper()}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"💎  Amount:  *GHS {entry['price']}*\n\n"
            f"Tap *PAY SECURELY NOW* to complete via Paystack.\n"
            f"Then tap *I'VE PAID — VERIFY* to confirm. 🎉",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif data.startswith("verify|"):
        parts     = data.split("|")
        reference = parts[1]
        lid       = int(parts[2])
        entry     = get_listing(lid)
        await ctx.bot.send_message(uid, "🔍 _Verifying your payment…_", parse_mode="Markdown")
        success = await verify_paystack(reference)
        if success:
            name = entry["name"] if entry else "the listing"
            await ctx.bot.send_message(
                uid,
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 *PAYMENT CONFIRMED!*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Your booking for *{name}* is locked in.\n"
                f"You will be contacted shortly. ✅",
                parse_mode="Markdown",
            )
            u = query.from_user
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"💰 *NEW ORDER PAID!*\n\n"
                    f"👤 Customer: @{u.username or u.first_name} (ID: `{u.id}`)\n"
                    f"🧾 Listing: *{name}* (ID: {lid})\n"
                    f"💵 Amount: GHS {entry['price'] if entry else 'N/A'}\n"
                    f"🔖 Reference: `{reference}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 TRY AGAIN", callback_data=f"verify|{reference}|{lid}")
            ]])
            await ctx.bot.send_message(
                uid,
                "❌ *Payment not confirmed yet.*\n\n"
                "If you've paid, wait a moment and tap *TRY AGAIN*.\n"
                "Haven't paid yet? Complete payment first. 👆",
                parse_mode="Markdown",
                reply_markup=kb,
            )

# ── FALLBACK TEXT ─────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "📸 Send a photo to add a listing, or:\n"
            "/list — see all listings with IDs\n"
            "/delete <id> — remove a listing\n"
            "/catalog — view the catalog"
        )
    else:
        await update.message.reply_text(
            "👋 Use /start to open the main menu!",
            reply_markup=welcome_keyboard(),
        )

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    upload_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO & filters.User(ADMIN_ID), handle_admin_photo)],
        states={
            ASK_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_AGE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_age)],
            ASK_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location)],
            ASK_COLOR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_color)],
            ASK_HEIGHT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_height)],
            ASK_HOURS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_hours)],
            ASK_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_price)],
            ASK_BIO:      [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_bio)],
        },
        fallbacks=[CommandHandler("cancel", cancel_upload)],
        allow_reentry=True,
    )

    app.add_handler(upload_conv)
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("catalog", catalog_cmd))
    app.add_handler(CommandHandler("list",    list_cmd))
    app.add_handler(CommandHandler("delete",  delete_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Catalog bot is running…")
    app.run_polling()
