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
import os
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
PAYSTACK_SECRET = os.environ["PAYSTACK_SECRET"]
ADMIN_ID        = 8162426062
CATALOG_FILE    = "catalog.json"
PAGE_SIZE       = 5   # listings shown per "page" in catalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── CONVERSATION STATES (admin upload flow) ───────────────────────────────────
(
    ASK_NAME, ASK_AGE, ASK_LOCATION, ASK_COLOR,
    ASK_HEIGHT, ASK_HOURS, ASK_PHONE, ASK_PRICE, ASK_BIO,
    CONFIRM_DELETE
) = range(10)

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
                headers={
                    "Authorization": f"Bearer {PAYSTACK_SECRET}",
                    "Content-Type": "application/json"
                },
                json={
                    "email": email,
                    "amount": int(amount_ghs * 100),
                    "currency": "GHS",
                    "metadata": {"listing": label},
                },
                timeout=15,
            )
            data = r.json()
            if data.get("status"):
                return {
                    "url": data["data"]["authorization_url"],
                    "reference": data["data"]["reference"],
                }
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
            data = r.json()
            return data.get("data", {}).get("status") == "success"
    except Exception as e:
        logging.error(f"Paystack verify error: {e}")
    return False

# ── CARD BUILDER ──────────────────────────────────────────────────────────────
def build_card_text(e: dict) -> str:
    lines = [
        f"👤 *{e['name']}, {e['age']}* — {e['location']}",
        "",
        f"🎨 Color: {e['color']}",
        f"📏 Height: {e['height']}",
        f"⏰ Hours: {e['hours']}",
        f"💬 {e['bio']}",
        "",
        f"💰 Price: *GHS {e['price']}*",
    ]
    return "\n".join(lines)

def build_card_keyboard(e: dict) -> InlineKeyboardMarkup:
    lid = e["id"]
    phone = e["phone"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛒 Order", callback_data=f"order|{lid}"),
            InlineKeyboardButton("📞 Call",  url=f"tel:{phone}"),
            InlineKeyboardButton("💬 Message", url=f"https://t.me/+{phone}"),
        ]
    ])

# ── CATALOG DISPLAY ───────────────────────────────────────────────────────────
async def send_catalog(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    catalog = load_catalog()
    if not catalog:
        await ctx.bot.send_message(
            chat_id,
            "😔 No listings yet. Check back soon!",
        )
        return

    start = page * PAGE_SIZE
    chunk = catalog[start: start + PAGE_SIZE]

    if page == 0:
        await ctx.bot.send_message(
            chat_id,
            "🛍️ *Welcome! Browse our listings below.*\n"
            "Tap *Order* to book, *Call* or *Message* for any girl you like 👇",
            parse_mode="Markdown",
        )

    for e in chunk:
        text = build_card_text(e)
        kb   = build_card_keyboard(e)
        try:
            await ctx.bot.send_photo(
                chat_id,
                photo=e["photo_id"],
                caption=text,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        except Exception as err:
            logging.error(f"Failed to send card {e['id']}: {err}")

    # Pagination
    total = len(catalog)
    nav_buttons = []
    if start + PAGE_SIZE < total:
        nav_buttons.append(
            InlineKeyboardButton("➡️ More listings", callback_data=f"page|{page+1}")
        )
    if page > 0:
        nav_buttons.insert(0,
            InlineKeyboardButton("⬅️ Back", callback_data=f"page|{page-1}")
        )

    if nav_buttons:
        await ctx.bot.send_message(
            chat_id,
            f"Showing {start+1}–{min(start+PAGE_SIZE, total)} of {total} listings",
            reply_markup=InlineKeyboardMarkup([nav_buttons]),
        )

# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Here's our catalog — scroll through and pick who you like 😍",
    )
    await send_catalog(update.effective_chat.id, ctx, page=0)

# ── /catalog ──────────────────────────────────────────────────────────────────
async def catalog_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_catalog(update.effective_chat.id, ctx, page=0)

# ── /delete ───────────────────────────────────────────────────────────────────
async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return ConversationHandler.END

    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: `/delete <listing_id>`\nUse /list to see IDs.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    lid = int(args[0])
    entry = get_listing(lid)
    if not entry:
        await update.message.reply_text(f"❌ No listing with ID {lid} found.")
        return ConversationHandler.END

    ctx.user_data["delete_id"] = lid
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirmdelete|{lid}"),
        InlineKeyboardButton("❌ Cancel", callback_data="canceldelete"),
    ]])
    await update.message.reply_text(
        f"⚠️ Are you sure you want to delete *{entry['name']}* (ID: {lid})?",
        parse_mode="Markdown",
        reply_markup=kb,
    )

# ── /list (admin) ─────────────────────────────────────────────────────────────
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

# ── ADMIN UPLOAD FLOW ──────────────────────────────────────────────────────────
async def handle_admin_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point: admin sends a photo."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "👋 I'm a private catalog bot. Use /start to browse listings."
        )
        return ConversationHandler.END

    # Save the highest-res photo file_id
    photo = update.message.photo[-1]
    ctx.user_data["photo_id"] = photo.file_id
    ctx.user_data["listing"] = {}

    await update.message.reply_text(
        "📸 Got the photo!\n\n"
        "Let's add the details step by step.\n\n"
        "➡️ *What is her name?*",
        parse_mode="Markdown"
    )
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
    await update.message.reply_text("➡️ *Color (e.g. skin tone / complexion)?*", parse_mode="Markdown")
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
    await update.message.reply_text("➡️ *Phone number? (include country code, e.g. 233241234567)*", parse_mode="Markdown")
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
        await update.message.reply_text("⚠️ Please enter a valid number for price.")
        return ASK_PRICE
    await update.message.reply_text("➡️ *Short description / bio?*", parse_mode="Markdown")
    return ASK_BIO

async def ask_bio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["listing"]["bio"] = update.message.text.strip()
    ctx.user_data["listing"]["photo_id"] = ctx.user_data["photo_id"]

    # Preview to admin
    e = ctx.user_data["listing"]
    e["id"] = 0  # temporary for preview
    preview = build_card_text(e)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save listing", callback_data="savelisting"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancellisting"),
    ]])
    await update.message.reply_photo(
        photo=e["photo_id"],
        caption=f"*Preview:*\n\n{preview}\n\nSave this listing?",
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

    # ── Save listing (admin) ──
    if data == "savelisting":
        if uid != ADMIN_ID:
            return
        entry = ctx.user_data.get("listing", {})
        if not entry:
            await query.message.reply_text("⚠️ No listing data found. Please re-upload.")
            return
        lid = add_listing(entry)
        await query.message.reply_text(
            f"✅ Listing *{entry['name']}* saved! ID: `{lid}`",
            parse_mode="Markdown"
        )
        ctx.user_data.clear()

    # ── Cancel listing (admin) ──
    elif data == "cancellisting":
        if uid != ADMIN_ID:
            return
        ctx.user_data.clear()
        await query.message.reply_text("❌ Listing cancelled.")

    # ── Confirm delete ──
    elif data.startswith("confirmdelete|"):
        if uid != ADMIN_ID:
            return
        lid = int(data.split("|")[1])
        entry = get_listing(lid)
        name = entry["name"] if entry else str(lid)
        if remove_listing(lid):
            await query.message.edit_text(f"🗑️ *{name}* (ID: {lid}) has been removed.", parse_mode="Markdown")
        else:
            await query.message.edit_text(f"⚠️ Listing ID {lid} not found.")

    # ── Cancel delete ──
    elif data == "canceldelete":
        await query.message.edit_text("👌 Delete cancelled.")

    # ── Pagination ──
    elif data.startswith("page|"):
        page = int(data.split("|")[1])
        await send_catalog(query.message.chat_id, ctx, page=page)

    # ── Order button ──
    elif data.startswith("order|"):
        lid = int(data.split("|")[1])
        entry = get_listing(lid)
        if not entry:
            await ctx.bot.send_message(uid, "⚠️ This listing is no longer available.")
            return

        await ctx.bot.send_message(uid, f"⏳ Setting up payment for *{entry['name']}*…", parse_mode="Markdown")

        txn = await init_paystack(entry["price"], entry["name"])
        if not txn:
            await ctx.bot.send_message(uid, "⚠️ Payment setup failed. Please try again later.")
            return

        # Store reference for verification
        if "pending" not in ctx.bot_data:
            ctx.bot_data["pending"] = {}
        ctx.bot_data["pending"][uid] = {"ref": txn["reference"], "listing": entry}

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=txn["url"])],
            [InlineKeyboardButton("✅ I've Paid — Verify", callback_data=f"verify|{txn['reference']}|{lid}")],
        ])
        await ctx.bot.send_message(
            uid,
            f"🛒 *Order: {entry['name']}*\n\n"
            f"💰 Amount: *GHS {entry['price']}*\n\n"
            f"Tap *Pay Now* to complete securely via Paystack.\n"
            f"Then tap *I've Paid — Verify* to confirm. 🎉",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ── Verify payment ──
    elif data.startswith("verify|"):
        parts     = data.split("|")
        reference = parts[1]
        lid       = int(parts[2])
        entry     = get_listing(lid)

        await ctx.bot.send_message(uid, "🔍 Verifying your payment…")
        success = await verify_paystack(reference)

        if success:
            name = entry["name"] if entry else "the listing"
            await ctx.bot.send_message(
                uid,
                f"🎉 *Payment confirmed!*\n\n"
                f"Thank you! Your booking for *{name}* has been received.\n"
                f"You'll be contacted shortly. ✅",
                parse_mode="Markdown",
            )
            # Notify admin
            u = query.from_user
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"💰 *New Order Paid!*\n\n"
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
                InlineKeyboardButton("🔄 Try Again", callback_data=f"verify|{reference}|{lid}")
            ]])
            await ctx.bot.send_message(
                uid,
                "❌ Payment not confirmed yet.\n\n"
                "If you've paid, wait a moment and tap *Try Again*.\n"
                "Haven't paid yet? Complete payment first. 👆",
                parse_mode="Markdown",
                reply_markup=kb,
            )

# ── FALLBACK: non-admin text messages ─────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text(
            "📸 Send a photo to add a new listing, or use:\n"
            "/list — see all listings\n"
            "/delete <id> — remove a listing\n"
            "/catalog — view catalog",
        )
    else:
        await update.message.reply_text(
            "👋 Use /start or /catalog to browse listings!"
        )

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Admin photo upload conversation
    upload_conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.PHOTO & filters.User(ADMIN_ID), handle_admin_photo
        )],
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
