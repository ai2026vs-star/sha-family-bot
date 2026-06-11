import os
import json
import logging
import tempfile
import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GOOGLE_CALENDAR_TOKEN = os.environ["GOOGLE_CALENDAR_TOKEN"]
WIFE_EMAIL = os.environ.get("WIFE_EMAIL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)

CATEGORIES = [
    "Продукти харчування", "Охорона здоров'я", "Дозвілля та розваги", "Транспорт",
    "Дитина", "Дім та побут", "Одяг", "Кафе та ресторани",
    "Аптека", "Інше", "Зарплата"
]

def get_calendar_service():
    token_data = json.loads(GOOGLE_CALENDAR_TOKEN)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)

def parse_with_claude(text: str, who: str) -> dict:
    today = datetime.date.today().isoformat()
    prompt = f"""Dzisiaj jest {today}.

Przeanalizuj wiadomość i określ czy to:
1. WYDATEK lub DOCHÓD (transakcja finansowa)
2. WYDARZENIE (coś zaplanowanego na datę/godzinę)
3. NIEZNANE

Wiadomość: "{text}"

Odpowiedz TYLKO w JSON bez żadnego tekstu przed ani po:

Dla wydatku/dochodu:
{{"type": "finance", "finance_type": "wydatek", "item": "nazwa", "amount": 10.0, "category": "Інше", "who": "{who}", "date": "{today}"}}

Dla wydarzenia:
{{"type": "calendar", "title": "tytuł", "date": "YYYY-MM-DD", "time": "HH:MM", "invite_wife": false, "reminder_minutes": 30}}

Dla nieznanego:
{{"type": "unknown"}}

Ważne:
- finance_type to "wydatek" lub "dochód"
- category musi być jedną z: {", ".join(CATEGORIES)}
- Jeśli "żona"/"Lera"/"ona" to who="L", inaczej who="{who}"
- Jeśli brak godziny to time=null
- Jutro = dzień po {today}"""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
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
    # Kolumny w Notion: Name, Сума, Валюта, Дата, Категорія, Хто
    props = {
        "Name": {"title": [{"text": {"content": data["item"]}}]},
        "Сума": {"number": data["amount"]},
        "Дата": {"date": {"start": data["date"]}},
        "Категорія": {"select": {"name": data.get("category", "Інше")}},
        "Хто": {"select": {"name": data.get("who", "V")}},
    }
    notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)

def add_to_calendar(data: dict):
    service = get_calendar_service()
    date_str = data["date"]
    time_str = data.get("time")
    duration = data.get("reminder_minutes", 60)

    if time_str:
        start_dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + datetime.timedelta(minutes=duration)
        start = {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Warsaw"}
        end = {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Warsaw"}
    else:
        start = {"date": date_str}
        end = {"date": date_str}

    event = {
        "summary": data["title"],
        "start": start,
        "end": end,
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": data.get("reminder_minutes", 30)}],
        },
    }

    if data.get("invite_wife") and WIFE_EMAIL:
        event["attendees"] = [{"email": WIFE_EMAIL}]

    service.events().insert(calendarId="primary", body=event).execute()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    if "lera" in username.lower() or "shapovalova" in username.lower():
        who = "L"
    else:
        who = "V"

    text = update.message.text

    try:
        data = parse_with_claude(text, who)

        if data["type"] == "finance":
            add_to_notion(data)
            emoji = "💸" if data["finance_type"] == "wydatek" else "💰"
            sign = "-" if data["finance_type"] == "wydatek" else "+"
            await update.message.reply_text(
                f"{emoji} Zapisano!\n{sign}{data['amount']} PLN — {data['item']}\n📂 {data.get('category', '?')} | 👤 {data.get('who', '?')} | 📅 {data['date']}"
            )

        elif data["type"] == "calendar":
            add_to_calendar(data)
            time_info = f" o {data['time']}" if data.get("time") else ""
            invite_info = " + zaproszenie dla żony 👩" if data.get("invite_wife") else ""
            await update.message.reply_text(
                f"📅 Dodano do kalendarza!\n{data['title']}\n🗓 {data['date']}{time_info}{invite_info}"
            )

        else:
            await update.message.reply_text(
                "Nie rozumiem 🤔\nNapisz np.:\n• 'kupiłem chleb 5 zł'\n• 'wizyta u lekarza w piątek o 10'"
            )

    except Exception as e:
        logger.error(f"Błąd: {e}")
        await update.message.reply_text("Coś poszło nie tak, spróbuj jeszcze raz.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        import httpx
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
                data={"model": "whisper-1", "language": "pl"},
                timeout=30
            )

        os.unlink(tmp_path)

        if response.status_code == 200:
            text = response.json().get("text", "")
            if text:
                update.message.text = text
                await handle_message(update, context)
            else:
                await update.message.reply_text("Nie udało się rozpoznać mowy.")
        else:
            await update.message.reply_text("Błąd transkrypcji.")

    except Exception as e:
        logger.error(f"Błąd głosu: {e}")
        await update.message.reply_text("Nie mogę przetworzyć wiadomości głosowej.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cześć! 👋\n\n"
        "💸 Wydatek: 'kupiłem chleb 5 zł'\n"
        "💰 Dochód: 'otrzymałem wynagrodzenie 5000 zł'\n"
        "📅 Wydarzenie: 'wizyta u lekarza w piątek o 10'\n\n"
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
        await update.message.reply_text(f"📊 Ten miesiąc: {total:.2f} PLN wydatków")
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
