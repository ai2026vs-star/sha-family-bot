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

def parse_with_claude(text: str, who: str) -> dict:
    today = datetime.date.today().isoformat()
    prompt = f"""Dzisiaj jest {today}.

Użytkownik napisał: "{text}"

Czy w tej wiadomości jest jakaś kwota pieniędzy (wydatek LUB dochód/przychód/wynagrodzenie/zarobek)?
Odpowiedz TYLKO w JSON bez żadnego tekstu:

Jeśli TAK (jest kwota):
{{"is_expense": true, "item": "nazwa/opis", "amount": 10.0, "category": "Транспорт", "who": "{who}", "date": "{today}", "finance_type": "wydatek"}}

Jeśli NIE (brak kwoty):
{{"is_expense": false}}

Zasady:
- finance_type = "wydatek" gdy kupuje/płaci/wydaje/kosztuje
- finance_type = "dochód" gdy otrzymuje/zarabia/wynagrodzenie/wpłata/przychód/зарплата/отримав
- category wybierz z: {", ".join(CATEGORIES)}
- Kategoria "Зарплата" dla wynagrodzeń i dochodów
- Jeśli mówi "żona"/"Lera"/"она"/"дружина" to who="L", inaczej who="{who}"
- amount to liczba (samo bez waluty)
- item to krótka nazwa np. "Зарплата", "Аптека", "Їжа", "Парковка""""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
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
            emoji = "💸" if data.get("finance_type") == "wydatek" else "💰"
            sign = "-" if data.get("finance_type") == "wydatek" else "+"
            await update.message.reply_text(
                f"{emoji} Zapisano!\n{sign}{data['amount']} PLN — {data['item']}\n"
                f"📂 {data.get('category', '?')} | 👤 {data.get('who', '?')} | 📅 {data['date']}"
            )
        else:
            await update.message.reply_text(
                "Nie rozumiem 🤔\nNapisz np.:\n• 'kupiłem chleb 5 zł'\n• 'apteka 19 PLN'"
            )
    except Exception as e:
        logger.error(f"Błąd: {e}")
        await update.message.reply_text("Coś poszło nie tak, spróbuj jeszcze raz.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or "").lower()
    who = "L" if ("lera" in username or "shapovalova" in username) else "V"
    await process_text(update.message.text, who, update)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or "").lower()
    who = "L" if ("lera" in username or "shapovalova" in username) else "V"
    
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
                await update.message.reply_text("Nie udało się rozpoznać mowy.")
        else:
            logger.error(f"Whisper error: {response.status_code} {response.text}")
            await update.message.reply_text("Błąd transkrypcji głosu.")
    
    except Exception as e:
        logger.error(f"Błąd głosu: {e}")
        await update.message.reply_text("Nie mogę przetworzyć wiadomości głosowej.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cześć! 👋\n\n"
        "💸 Wydatek: 'kupiłem chleb 5 zł'\n"
        "💰 Dochód: 'otrzymałem wynagrodzenie 5000 zł'\n\n"
        "Pisz lub nagrywaj głosówki!"
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
        await update.message.reply_text(f"📊 Ten miesiąc: {count} transakcji, {total:.2f} PLN")
    except Exception as e:
        logger.error(f"Błąd raportu: {e}")
        await update.message.reply_text("Błąd pobierania raportu.")

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
