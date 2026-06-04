import os
import json
import base64
import logging
import anthropic
import gspread
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.service_account import Credentials

# ── الـ Keys تأتي من Environment Variables (Railway) ──
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
SHEET_ID       = os.environ["SHEET_ID"]

# Google Credentials من Environment Variable
GOOGLE_CREDS = json.loads(os.environ["GOOGLE_CREDS_JSON"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Google Sheets ──────────────────────────────────────
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(GOOGLE_CREDS, scopes=scopes)
    client = gspread.authorize(creds)
    sh     = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("فواتير ليزو")
    except:
        ws = sh.add_worksheet(title="فواتير ليزو", rows=1000, cols=10)
        ws.append_row(["#","التاريخ","النوع","التصنيف","الجهة / المورد","الوصف","المبلغ (ريال)","الاتجاه","أضافه","ملاحظات"])
    return ws

def next_row_num(ws):
    vals = ws.col_values(1)
    nums = [int(v) for v in vals[1:] if str(v).isdigit()]
    return max(nums) + 1 if nums else 1

def append_to_sheet(data: dict, added_by: str):
    ws  = get_sheet()
    num = next_row_num(ws)
    ws.append_row([
        num,
        data.get("date", ""),
        data.get("type", ""),
        data.get("category", ""),
        data.get("party", ""),
        data.get("description", ""),
        data.get("amount", ""),
        data.get("direction", ""),
        added_by,
        data.get("notes", "")
    ])

# ── Claude يحلل الفاتورة ───────────────────────────────
SYSTEM_PROMPT = """أنت محاسب ذكي لمتجر Lizo السعودي المتخصص في نحت الأسماء والشعارات على الهدايا.
مهمتك: تحليل الفواتير وإيصالات التحويل البنكي وإرجاع JSON فقط بدون أي نص إضافي.

قواعد التصنيف:
- إذا ليزو هو المشتري → النوع: "فاتورة شراء"
- إذا ليزو هو البائع أو وصل طلب من عميل → النوع: "مبيعة"
- إذا إيصال تحويل خارج من حساب ليزو → النوع: "تحويل صادر"
- إذا إيصال تحويل داخل لحساب ليزو → النوع: "تحويل وارد"
- اشتراكات (كانفا، أدوبي) → النوع: "فاتورة شراء" + التصنيف: "اشتراكات رقمية"

تصنيفات المصروفات:
مواد خام | تغليف | طباعة | اشتراكات رقمية | معدات | رسوم تشغيلية | أخرى

أرجع JSON بهذا الشكل بالضبط:
{
  "date": "DD/MM/YYYY",
  "type": "نوع العملية",
  "category": "التصنيف",
  "party": "اسم المورد أو العميل أو المستفيد",
  "description": "وصف مختصر للمنتج أو الخدمة",
  "amount": 0.00,
  "direction": "مصروف أو وارد",
  "notes": "أي ملاحظة مفيدة"
}"""

def analyze_with_claude(image_b64: str, media_type: str, caption: str = "") -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": f"حلل هذه الفاتورة/الإيصال وأرجع JSON فقط.\nملاحظة من المرسل: {caption}" if caption else "حلل هذه الفاتورة/الإيصال وأرجع JSON فقط."}
        ]
    }]
    response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=SYSTEM_PROMPT, messages=messages)
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())

def analyze_text_with_claude(text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"حلل هذا النص وأرجع JSON فقط:\n{text}"}])
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    return json.loads(raw.strip())

# ── معالجة الرسائل ─────────────────────────────────────
TYPE_EMOJI = {"فاتورة شراء": "🔴", "مبيعة": "🟢", "تحويل صادر": "🔵", "تحويل وارد": "🟡"}

def build_reply(data: dict) -> str:
    emoji = TYPE_EMOJI.get(data.get("type", ""), "📄")
    reply = (
        f"{emoji} *تم التسجيل في الشيت!*\n\n"
        f"📅 التاريخ: {data.get('date','—')}\n"
        f"🏷️ النوع: {data.get('type','—')}\n"
        f"📂 التصنيف: {data.get('category','—')}\n"
        f"🏪 الجهة: {data.get('party','—')}\n"
        f"📦 الوصف: {data.get('description','—')}\n"
        f"💰 المبلغ: {data.get('amount','—')} ريال\n"
        f"↕️ الاتجاه: {data.get('direction','—')}\n"
    )
    if data.get("notes"):
        reply += f"📝 ملاحظة: {data.get('notes')}\n"
    return reply

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user.first_name or "مجهول"
    caption = msg.caption or ""
    await msg.reply_text("⏳ جاري تحليل الفاتورة...")
    try:
        photo  = msg.photo[-1]
        file   = await photo.get_file()
        b_data = await file.download_as_bytearray()
        b64    = base64.standard_b64encode(bytes(b_data)).decode()
        data   = analyze_with_claude(b64, "image/jpeg", caption)
        append_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.reply_text(f"❌ حدث خطأ: {str(e)}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    doc  = msg.document
    user = msg.from_user.first_name or "مجهول"
    caption = msg.caption or ""
    if doc.mime_type not in ["image/jpeg","image/png","image/jpg"]:
        await msg.reply_text("⚠️ الرجاء إرسال صورة. الملفات النصية غير مدعومة حالياً.")
        return
    await msg.reply_text("⏳ جاري تحليل الفاتورة...")
    try:
        file   = await doc.get_file()
        b_data = await file.download_as_bytearray()
        b64    = base64.standard_b64encode(bytes(b_data)).decode()
        data   = analyze_with_claude(b64, doc.mime_type, caption)
        append_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.reply_text(f"❌ حدث خطأ: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    text = msg.text or ""
    user = msg.from_user.first_name or "مجهول"
    keywords = ["ريال","فاتورة","تحويل","مبلغ","SAR","مصروف","دفعت","اشتريت"]
    if not any(k in text for k in keywords):
        return
    await msg.reply_text("⏳ جاري تحليل الرسالة...")
    try:
        data = analyze_text_with_claude(text)
        append_to_sheet(data, user)
        await msg.reply_text(build_reply(data), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.reply_text(f"❌ حدث خطأ: {str(e)}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🤖 Lizo Bot يعمل...")
    app.run_polling()

if __name__ == "__main__":
    main()
