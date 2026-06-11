import os
import json
import logging
import tempfile
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import httpx
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

EXPENSE_CATEGORIES = [
    "Продукти харчування",
    "Охорона здоров'я",
    "Дозвілля та розваги",
    "Транспорт",
    "Дитина",
    "Дім та побут",
    "Одяг",
    "Кафе та ресторани",
    "Аптека",
    "Інше"
]

INCOME_CATEGORIES = [
    "Зарплата",
    "Фріланс",
    "Бонус",
    "Подарунок",
    "Інший дохід"
]


async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f
        )
    return transcript.text


def parse_with_claude(text: str) -> dict:
    expense_cats = "\n".join(f"- {c}" for c in EXPENSE_CATEGORIES)
    income_cats = "\n".join(f"- {c}" for c in INCOME_CATEGORIES)

    prompt = f"""Ты помощник для учёта семейного бюджета. Пользователь написал: "{text}"

Определи тип транзакции и верни JSON:
{{
  "amount": <число>,
  "currency": "PLN",
  "description": "<название>",
  "category": "<категория>",
  "type": "expense" или "income",
  "is_transaction": true/false
}}

Категории РАСХОДОВ:
{expense_cats}

Категории ДОХОДОВ:
{income_cats}

Правила:
- Если это трата/расход — type: "expense"
- Если это доход/зарплата/получил — type: "income"
- Если не про деньги — верни {{"is_transaction": false}}
- currency всегда PLN если не указано другое
- Верни ТОЛЬКО JSON, без пояснений"""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    result_text = response.content[0].text.strip()
    result_text = result_text.replace("```json", "").replace("```", "").strip()
    return json.loads(result_text)


def add_to_notion(description: str, amount: float, currency: str, category: str, user_name: str, tx_type: str) -> bool:
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    today = datetime.now().strftime("%Y-%m-%d")
    data = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": description}}]},
            "Сума": {"number": amount},
            "Валюта": {"select": {"name": currency}},
            "Категорія": {"select": {"name": category}},
            "Дата": {"date": {"start": today}},
            "Хто": {"select": {"name": user_name}},
            "Тип": {"select": {"name": "Дохід" if tx_type == "income" else "Витрата"}}
        }
    }
    response = httpx.post(url, headers=headers, json=data)
    return response.status_code == 200


def get_monthly_report() -> dict:
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    now = datetime.now()
    start = f"{now.year}-{now.month:02d}-01"
    payload = {
        "filter": {
            "property": "Дата",
            "date": {"on_or_after": start}
        }
    }
    response = httpx.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        return None

    results = response.json().get("results", [])
    income = 0
    expenses = 0
    by_category = {}

    for r in results:
        props = r["properties"]
        amount = props.get("Сума", {}).get("number") or 0
        tx_type = props.get("Тип", {}).get("select", {})
        tx_type_name = tx_type.get("name", "") if tx_type else ""
        category = props.get("Категорія", {}).get("select", {})
        cat_name = category.get("name", "Інше") if category else "Інше"

        if tx_type_name == "Дохід":
            income += amount
        else:
            expenses += amount
            by_category[cat_name] = by_category.get(cat_name, 0) + amount

    return {
        "income": income,
        "expenses": expenses,
        "balance": income - expenses,
        "by_category": by_category
    }


def get_today_expenses() -> list:
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    today = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "filter": {
            "property": "Дата",
            "date": {"equals": today}
        }
    }
    response = httpx.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        return []

    results = response.json().get("results", [])
    items = []
    for r in results:
        props = r["properties"]
        name = props.get("Name", {}).get("title", [{}])
        name = name[0].get("text", {}).get("content", "?") if name else "?"
        amount = props.get("Сума", {}).get("number") or 0
        tx_type = props.get("Тип", {}).get("select", {})
        tx_type_name = tx_type.get("name", "Витрата") if tx_type else "Витрата"
        items.append({"name": name, "amount": amount, "type": tx_type_name})
    return items


async def process_text(text: str, user_name: str, update: Update):
    try:
        parsed = parse_with_claude(text)
        if not parsed.get("is_transaction"):
            await update.message.reply_text("Не понял 🤔 Напиши например:\n• потратил 50 злотых на продукты\n• получил зарплату 3000 PLN")
            return

        tx_type = parsed.get("type", "expense")
        success = add_to_notion(
            parsed["description"],
            parsed["amount"],
            parsed.get("currency", "PLN"),
            parsed["category"],
            user_name,
            tx_type
        )

        if success:
            emoji = "💰" if tx_type == "income" else "💸"
            type_label = "Дохід записано!" if tx_type == "income" else "Витрату записано!"
            await update.message.reply_text(
                f"✅ {type_label}\n\n"
                f"📝 {parsed['description']}\n"
                f"{emoji} {parsed['amount']} {parsed.get('currency', 'PLN')}\n"
                f"🏷 {parsed['category']}\n"
                f"👤 {user_name}"
            )
        else:
            await update.message.reply_text("❌ Помилка запису в Notion.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Щось пішло не так. Спробуй ще раз.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Напиши или надиктуй трату или доход:\n\n"
        "💸 Расходы:\n"
        "• потратил 45 злотых на продукты\n"
        "• парковка 5 PLN\n\n"
        "💰 Доходы:\n"
        "• получил зарплату 3000 PLN\n"
        "• фриланс 500 злотых\n\n"
        "📊 Команды:\n"
        "/raport — отчёт за месяц\n"
        "/dzisiaj — траты сегодня"
    )


async def raport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    data = get_monthly_report()
    if not data:
        await update.message.reply_text("❌ Не удалось получить данные.")
        return

    now = datetime.now()
    month_name = now.strftime("%B %Y")

    cats = ""
    for cat, amount in sorted(data["by_category"].items(), key=lambda x: -x[1]):
        cats += f"  • {cat}: {amount:.0f} PLN\n"

    balance_emoji = "📈" if data["balance"] >= 0 else "📉"

    await update.message.reply_text(
        f"📊 Отчёт за {month_name}:\n\n"
        f"💰 Доходы: {data['income']:.0f} PLN\n"
        f"💸 Расходы: {data['expenses']:.0f} PLN\n"
        f"{balance_emoji} Баланс: {data['balance']:.0f} PLN\n\n"
        f"📂 По категориям:\n{cats}"
    )


async def dzisiaj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    items = get_today_expenses()
    if not items:
        await update.message.reply_text("Сегодня ещё ничего не записано 🙂")
        return

    total_exp = sum(i["amount"] for i in items if i["type"] == "Витрата")
    total_inc = sum(i["amount"] for i in items if i["type"] == "Дохід")

    lines = ""
    for i in items:
        emoji = "💰" if i["type"] == "Дохід" else "💸"
        lines += f"{emoji} {i['name']}: {i['amount']:.0f} PLN\n"

    msg = f"📅 Сегодня:\n\n{lines}"
    if total_exp > 0:
        msg += f"\n💸 Итого расходов: {total_exp:.0f} PLN"
    if total_inc > 0:
        msg += f"\n💰 Итого доходов: {total_inc:.0f} PLN"

    await update.message.reply_text(msg)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Невідомо"
    text = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await process_text(text, user_name, update)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Невідомо"
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        text = await transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        logger.info(f"Transcribed: {text}")
        await process_text(text, user_name, update)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text("❌ Не смог распознать голос. Попробуй ещё раз.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raport", raport))
    app.add_handler(CommandHandler("dzisiaj", dzisiaj))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
