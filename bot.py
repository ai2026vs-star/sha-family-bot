import os
import json
import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CATEGORIES = [
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

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def parse_expense_with_claude(text: str) -> dict:
    categories_str = "\n".join(f"- {c}" for c in CATEGORIES)
    prompt = f"""Ты помощник для учёта расходов семьи. Пользователь написал: "{text}"

Извлеки информацию о трате и верни JSON:
{{"amount": <число>, "currency": "PLN", "description": "<название>", "category": "<категория>", "is_expense": true/false}}

Категории:
{categories_str}

Правила:
- Если не про трату — верни {{"is_expense": false}}
- currency всегда PLN если не указано другое
- Верни ТОЛЬКО JSON, без пояснений"""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    
    block = response.content[0]
    result_text = block.text.strip()
    result_text = result_text.replace("```json", "").replace("```", "").strip()
    return json.loads(result_text)


def add_to_notion(description: str, amount: float, currency: str, category: str, user_name: str) -> bool:
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
            "Хто": {"select": {"name": user_name}}
        }
    }
    response = httpx.post(url, headers=headers, json=data)
    return response.status_code == 200


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "друг"
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Просто напиши что потратил:\n"
        "• потратил 45 злотых на продукты\n"
        "• парковка 5 PLN\n"
        "• кофе 12 злотых\n\n"
        "Я запишу в Notion 📊"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name or "Неизвестно"
    text = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        parsed = parse_expense_with_claude(text)
        if not parsed.get("is_expense"):
            await update.message.reply_text("Не понял как трату 🤔 Напиши например: потратил 50 злотых на продукты")
            return
        success = add_to_notion(parsed["description"], parsed["amount"], parsed.get("currency", "PLN"), parsed["category"], user_name)
        if success:
            await update.message.reply_text(
                f"✅ Записано!\n\n"
                f"📝 {parsed['description']}\n"
                f"💰 {parsed['amount']} {parsed.get('currency', 'PLN')}\n"
                f"🏷 {parsed['category']}\n"
                f"👤 {user_name}"
            )
        else:
            await update.message.reply_text("❌ Ошибка записи в Notion.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("❌ Что-то пошло не так. Попробуй ещё раз.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
