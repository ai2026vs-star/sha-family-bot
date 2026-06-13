import os
import json
import logging
import tempfile
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from notion_client import Client as NotionClient
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)

CATEGORIES = [
    "Продукти харчування", "Охорона здоров'я", "Дозвілля та розваги", "Транспорт",
    "Дитина", "Дім та побут", "Одяг", "Кафе та ресторани",
    "Аптека", "Інше", "Зарплата"
]

WIFE_USERNAMES = ["valeriia_photo", "lera", "shapovalova"]

def get_who(username: str) -> str:
    username = username.lower()
    for w in WIFE_USERNAMES:
        if w in username:
            return "L"
    return "V"

def parse_with_claude(text: str, who: str) -> dict:
    today = datetime.date.today().isoformat()
    categories_str = ", ".join(CATEGORIES)
    
    system_prompt = "Ты помощник для учёта финансов. Отвечай ТОЛЬКО валидным JSON без пояснений."
    
    user_prompt = (
        f"Сегодня {today}.\n"
        f"Пользователь написал: {text}\n\n"
        f"Определи: это финансовая операция (расход или доход)?\n\n"
        f"Если ДА, ответь:\n"
        f'{{\"is_expense\": true, \"item\": \"название\", \"amount\": 10.0, \"category\": \"Транспорт\", \"who\": \"{who}\", \"date\": \"{today}\", \"finance_type\": \"wydatek\"}}\n\n'
        f"Если НЕТ, ответь:\n"
        f'{{\"is_expense\": false}}\n\n'
        f"Правила:\n"
        f"- finance_type = wydatek (расход/купил/потратил)\n"
        f"- finance_type = dochod (доход/зарплата/получил/заработал)\n"
        f"- category из списка: {categories_str}\n"
        f"- Зарплата/доходы -> category = Зарплата\n"
        f"- Если упоминается жена/Лера/дружина -> who = L, иначе who = {who}\n"
        f"- amount - только число"
    )
    
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    return json.loads(raw)

def add_to_notion(data: dict):
    props = {
        "Name": {"title": [{"text": {"content": data["item"]}}]},
        "Сума": {"number": data["amount"]},
        "Дата": {"date": {"start": data["date"]}},
        "Категорія": {"select": {"name": data.get("category", "Інше")}},
        "Хто": {"select": {"name": data.get("who", "V")}},
    }
    notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)

async def process_text(text: str, who: str, update: Update):
    try:
        data = parse_with_claude(text, who)
        
        if data.get("is_expense"):
            add_to_notion(data)
            ft = data.get("finance_type", "wydatek")
            emoji = "💰" if ft == "dochod" else "💸"
            sign = "+" if ft == "dochod" else "-"
            await update.message.reply_text(
                f"{emoji} Zapisano!\n"
                f"{sign}{data['amount']} PLN — {data['item']}\n"
                f"📂 {data.get('category', '?')} | 👤 {data.get('who', '?')} | 📅 {data['date']}"
            )
        else:
            await update.message.reply_text(
                "Не розумію 🤔\nНапишіть наприклад:\n"
                "• куплю хліб 5 зл\n"
                "• аптека 19 PLN\n"
                "• зарплата 5000 зл"
            )
    except Exception as e:
        logger.error(f"Błąd: {e}")
        await update.message.reply_text("Щось пішло не так, спробуй ще раз.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    who = get_who(username)
    await process_text(update.message.text, who, update)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    who = get_who(username)
    
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        
        with open(tmp_path, "rb") as audio_file:
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": ("voice.ogg", audio_file, "audio/ogg")},
                data={"model": "whisper-1"},
                timeout=30
            )
        
        os.unlink(tmp_path)
        
        if response.status_code == 200:
            text = response.json().get("text", "").strip()
            logger.info(f"Transkrypcja: {text}")
            if text:
                await process_text(text, who, update)
            else:
                await update.message.reply_text("Не вдалося розпізнати мову.")
        else:
            logger.error(f"Whisper error: {response.status_code}")
            await update.message.reply_text("Помилка транскрипції.")
    
    except Exception as e:
        logger.error(f"Błąd głosu: {e}")
        await update.message.reply_text("Не можу обробити голосове повідомлення.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! 👋\n\n"
        "💸 Витрата: купив хліб 5 зл\n"
        "💰 Дохід: зарплата 5000 зл\n\n"
        "Пиши або записуй голосові!"
    )

async def raport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.datetime.now()
        month_start = datetime.date(now.year, now.month, 1).isoformat()
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Дата", "date": {"on_or_after": month_start}}
        )
        total = sum(p["properties"]["Сума"]["number"] or 0 for p in results["results"])
        count = len(results["results"])
        await update.message.reply_text(f"📊 Цей місяць: {count} записів, {total:.2f} PLN")
    except Exception as e:
        logger.error(f"Błąd raportu: {e}")
        await update.message.reply_text("Помилка отримання звіту.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raport", raport))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot uruchomiony!")
    app.run_polling()

if __name__ == "__main__":
    main()
