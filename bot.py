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

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
notion = NotionClient(auth=NOTION_TOKEN)

USERS = {
    "vladyslav.shapovalov.rs": "V",
}

CATEGORIES = [
    "produkty spożywcze", "zdrowie", "rozrywka", "transport",
    "dziecko", "dom i gospodarstwo", "odzież", "kawiarnie i restauracje",
    "apteka", "inne"
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

def parse_with_claude(text: str, username: str) -> dict:
    today = datetime.date.today().isoformat()
    current_year = datetime.date.today().year
    
    prompt = f"""Dzisiaj jest {today}. Rok: {current_year}.

Przeanalizuj wiadomość użytkownika i określ czy to:
1. WYDATEK - coś zostało kupione lub opłacone
2. DOCHÓD - otrzymano pieniądze
3. WYDARZENIE KALENDARZA - coś zaplanowanego na konkretną datę/godzinę
4. NIEZNANE - nic z powyższych

Wiadomość: "{text}"
Użytkownik: {username}

Odpowiedz TYLKO w JSON (bez markdown, bez komentarzy):

Dla wydatku/dochodu:
{{"type": "finance", "finance_type": "wydatek" lub "dochód", "item": "nazwa", "amount": liczba, "currency": "PLN", "category": "kategoria", "who": "V" lub "L", "date": "YYYY-MM-DD", "comment": ""}}

Dla wydarzenia kalendarza:
{{"type": "calendar", "title": "tytuł wydarzenia", "date": "YYYY-MM-DD", "time": "HH:MM" lub null, "duration_minutes": 60, "invite_wife": true lub false, "reminder_minutes": 30, "description": ""}}

Dla nieznanego:
{{"type": "unknown"}}

Kategorie finansowe: {", ".join(CATEGORIES)}
Żona = "L" (Lera). Jeśli wiadomość mówi "żona", "Lera", "ona" - who="L", inaczej who="{username}".
Jeśli brak godziny dla wydarzenia - time=null.
Jeśli napisano "jutro" - oblicz datę relative do {today}.
"""
    
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
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
        "Назва": {"title": [{"text": {"content": data["item"]}}]},
        "Сума": {"number": data["amount"]},
        "Валюта": {"select": {"name": data.get("currency", "PLN")}},
        "Категорія": {"select": {"name": data.get("category", "inne")}},
        "Дата": {"date": {"start": data["date"]}},
        "Хто": {"select": {"name": data.get("who", "V")}},
        "Тип": {"select": {"name": "Дохід" if data.get("finance_type") == "dochód" else "Витрата"}},
    }
    if data.get("comment"):
        props["Коментар"] = {"rich_text": [{"text": {"content": data["comment"]}}]}
    
    notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)

def add_to_calendar(data: dict, invite_wife_email: str = None):
    service = get_calendar_service()
    
    date_str = data["date"]
    time_str = data.get("time")
    duration = data.get("duration_minutes", 60)
    
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
        "description": data.get("description", ""),
        "start": start,
        "end": end,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": data.get("reminder_minutes", 30)},
            ],
        },
    }
    
    if data.get("invite_wife") and invite_wife_email:
        event["attendees"] = [{"email": invite_wife_email}]
    
    result = service.events().insert(calendarId="primary", body=event).execute()
    return result.get("htmlLink", "")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username_raw = user.username or user.first_name or "V"
    
    if "lera" in username_raw.lower() or "shapovalova" in username_raw.lower():
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
                f"{emoji} Zapisano!\n{sign}{data['amount']} {data.get('currency','PLN')} — {data['item']}\n📂 {data.get('category','inne')} | 👤 {data.get('who','?')} | 📅 {data['date']}"
            )
        
        elif data["type"] == "calendar":
            wife_email = WIFE_EMAIL
            link = add_to_calendar(data, wife_email if data.get("invite_wife") else None)
            
            time_info = f" o {data['time']}" if data.get("time") else ""
            invite_info = " + zaproszenie dla żony 👩" if data.get("invite_wife") else ""
            await update.message.reply_text(
                f"📅 Dodano do kalendarza!\n{data['title']}\n🗓 {data['date']}{time_info}{invite_info}\n⏰ Przypomnienie: {data.get('reminder_minutes', 30)} min wcześniej"
            )
        
        else:
            await update.message.reply_text(
                "Nie rozumiem 🤔 Powiedz mi o wydatku (np. 'kupiłem chleb 5 zł') lub wydarzeniu (np. 'wizyta u lekarza jutro o 10')."
            )
    
    except Exception as e:
        logger.error(f"Błąd: {e}")
        await update.message.reply_text("Coś poszło nie tak, spróbuj jeszcze raz.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        
        with open(tmp_path, "rb") as audio_file:
            import httpx
            response = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
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
            await update.message.reply_text("Błąd transkrypcji głosu.")
    
    except Exception as e:
        logger.error(f"Błąd głosu: {e}")
        await update.message.reply_text("Nie mogę przetworzyć wiadomości głosowej.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Cześć! 👋 Jestem twoim asystentem rodzinnym.\n\n"
        "💸 Powiedz mi o wydatku: 'kupiłem chleb 5 zł'\n"
        "💰 Lub o dochodzie: 'otrzymałem wynagrodzenie 5000 zł'\n"
        "📅 Lub o wydarzeniu: 'wizyta u lekarza w piątek o 10'\n\n"
        "Możesz pisać lub nagrywać głosówki!"
    )

async def raport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.datetime.now()
        month_start = datetime.date(now.year, now.month, 1).isoformat()
        
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Дата", "date": {"on_or_after": month_start}}
        )
        
        wydatki = sum(p["properties"]["Сума"]["number"] or 0 
                     for p in results["results"] 
                     if p["properties"].get("Тип", {}).get("select", {}).get("name") == "Витрата")
        dochody = sum(p["properties"]["Сума"]["number"] or 0 
                     for p in results["results"] 
                     if p["properties"].get("Тип", {}).get("select", {}).get("name") == "Дохід")
        
        await update.message.reply_text(
            f"📊 Raport za {now.strftime('%B %Y')}:\n"
            f"💰 Dochody: {dochody:.2f} PLN\n"
            f"💸 Wydatki: {wydatki:.2f} PLN\n"
            f"💵 Bilans: {dochody - wydatki:.2f} PLN"
        )
    except Exception as e:
        logger.error(f"Błąd raportu: {e}")
        await update.message.reply_text("Błąd pobierania raportu.")

async def dzisiaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = datetime.date.today().isoformat()
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Дата", "date": {"equals": today}}
        )
        
        if not results["results"]:
            await update.message.reply_text(f"📅 Dzisiaj ({today}) brak zapisów.")
            return
        
        lines = [f"📅 Dzisiaj ({today}):"]
        total = 0
        for p in results["results"]:
            name = p["properties"]["Назва"]["title"][0]["text"]["content"] if p["properties"]["Назва"]["title"] else "?"
            amount = p["properties"]["Сума"]["number"] or 0
            typ = p["properties"].get("Тип", {}).get("select", {}).get("name", "")
            sign = "-" if typ == "Витрата" else "+"
            lines.append(f"{sign}{amount} PLN — {name}")
            if typ == "Витрата":
                total -= amount
            else:
                total += amount
        
        lines.append(f"\nBilans: {total:.2f} PLN")
        await update.message.reply_text("\n".join(lines))
    
    except Exception as e:
        logger.error(f"Błąd dzisiaj: {e}")
        await update.message.reply_text("Błąd pobierania danych.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raport", raport))
    app.add_handler(CommandHandler("dzisiaj", dzisiaj))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot uruchomiony!")
    app.run_polling()

if __name__ == "__main__":
    main()
