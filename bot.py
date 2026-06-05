import os
import json
import base64
import logging
import anthropic
import gspread
import fitz
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
SHEET_ID       = os.environ["SHEET_ID"]
GOOGLE_CREDS   = json.loads(os.environ["GOOGLE_CREDS_JSON"])
# اسم الموديل من Environment Variable أو القيمة الافتراضية
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.info(f"Using model: {MODEL}")

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("فواتير ليزو")
    except:
        ws = sh.add_worksheet(title="فواتير ليزو", rows=1000, cols=10)
        ws.append_row(["#","التاريخ","النوع","التصنيف","الجهة","الوصف","المبلغ","الاتجاه","أضافه","ملاحظات"])
    return ws

def save_to_sheet(data: dict, user: str):
    ws   = get_sheet()
    vals = ws.col_values(1)
    nums = [int(v) for v in vals[1:] if str(v).isdigit()]
    num  = max(nums) + 1 if nums else 1
    ws.append_row([num, data.get("date",""), data.get("type",""), data.get("category",""),
                   data.get("party",""), data.get("description",""), data.get("amount",""),
                   data.get("direction",""), user, data.get("notes","")])

SYSTEM = """أنت محاسب ذكي لمتجر Lizo السعودي. حلل الفاتورة وأرجع JSON فقط:
{
  "date": "DD/MM/YYYY",
  "type": "فاتورة شراء أو مبيعة أو تحويل صادر أو تحويل وارد",
  "category": "مواد خام أو تغليف أو طباعة أو اشتراكات رقمية أو معدات أو رسوم تشغيلية أو أخرى",
  "party": "اسم المورد أو العميل",
  "description": "وصف مختصر",
  "amount": 0.00,
  "direction": "مصروف أو وارد",
  "notes": "ملاحظة"
}"""

def ask_claude_image(b64, mt, caption=""):
    c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    note = f"\nملاحظة: {caption}" if caption else ""
    r = c.messages.create(model=MODEL, max_tokens=800, system=SYSTEM,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mt,"data":b64}},
            {"type":"text","text":f"حلل وأرجع JSON فقط.{note}"}]}])
    raw = r.content[0].text.strip().strip("```").strip()
    if raw.startswith("json"): raw = raw[4:].strip()
    return json.loads(raw)

def ask_claude_text(text):
    c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    r = c.messages.create(model=MODEL, max_tokens=800, system=SYSTEM,
        messages=[{"role":"user","content":f"حلل وأرجع JSON فقط:\n{text}"}])
    raw = r.content[0].text.strip().strip("```").strip()
    if raw.startswith("json"): raw = raw[4:].strip()
    return json.loads(raw)

def pdf_to_b64(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(2,2))
    return base64.standard_b64encode(pix.tobytes("jpeg")).decode()

EMOJI = {"فاتورة شراء":"🔴","مبيعة":"🟢","تحويل صادر":"🔵","تحويل وارد":"🟡"}

def build_reply(data):
    e = EMOJI.get(data.get("type",""),"📄")
    t = (f"{e} *تم التسجيل!*\n\n"
         f"📅 {data.get('date','—')}\n"
         f"🏷️ {data.get('type','—')} | {data.get('category','—')}\n"
         f"🏪 {data.get('party','—')}\n"
         f"📦 {data.get('description','—')}\n"
         f"💰 {data.get('amount','—')} ريال | {data.get('direction','—')}\n")
    if data.get("notes"): t += f"📝 {data['notes']}\n"
    return t

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user.first_name or "مجهول"
    await msg.reply_text("⏳ جاري التحليل...")
    try:
        f = await msg.photo[-1].get_file()
        raw = await f.download_as_bytearray()
        b64 = base64.standard_b64encode(bytes(raw)).decode()
        data = ask_claude_image(b64, "image/jpeg", msg.caption or "")
        save_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await msg.reply_text(f"❌ خطأ: {e}")

async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    user = msg.from_user.first_name or "مجهول"
    await msg.reply_text("⏳ جاري التحليل...")
    try:
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
        if doc.mime_type == "application/pdf":
            b64, mt = pdf_to_b64(bytes(raw)), "image/jpeg"
        elif "image" in (doc.mime_type or ""):
            b64, mt = base64.standard_b64encode(bytes(raw)).decode(), doc.mime_type
        else:
            await msg.reply_text("⚠️ أرسل صورة أو PDF فقط.")
            return
        data = ask_claude_image(b64, mt, msg.caption or "")
        save_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await msg.reply_text(f"❌ خطأ: {e}")

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or ""
    user = msg.from_user.first_name or "مجهول"
    if not any(k in text for k in ["ريال","فاتورة","تحويل","SAR","دفعت","اشتريت"]):
        return
    await msg.reply_text("⏳ جاري التحليل...")
    try:
        data = ask_claude_text(text)
        save_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(e)
        await msg.reply_text(f"❌ خطأ: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logger.info(f"🤖 Lizo Bot running with model: {MODEL}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
