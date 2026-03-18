import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ChatAction

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = "7637715655:AAHJUlqtUjUVC67xZqsVxNRh2cmkGJZVmms"  # Replace with your bot token from @BotFather

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! 👋\n\n"
        "I'm a simple Telegram bot. Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    help_text = """
📋 Available Commands:

/start - Start the bot
/help - Show this help message
/ping - Check if bot is alive
/echo <text> - Echo your message back
/weather <city> - Get weather info (if API available)

Just send me any message and I'll respond!
    """
    await update.message.reply_text(help_text)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond with pong when /ping is issued."""
    await update.message.reply_text("🏓 Pong!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user's message."""
    if not context.args:
        await update.message.reply_text("Please provide some text to echo.\nUsage: /echo <your text>")
        return
    
    echo_text = " ".join(context.args)
    await update.message.reply_text(f"🔊 Echo: {echo_text}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular messages."""
    user_message = update.message.text
    
    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)
    
    # Simple responses
    responses = {
        "hello": "Hello! 👋 How can I help?",
        "hi": "Hi there! 😊",
        "how are you": "I'm doing great! Thanks for asking! 🤖",
        "thanks": "You're welcome! 😊",
        "bye": "Goodbye! See you later! 👋",
    }
    
    # Check for keyword matches (case-insensitive)
    for keyword, response in responses.items():
        if keyword.lower() in user_message.lower():
            await update.message.reply_text(response)
            return
    
    # Default response
    await update.message.reply_text(f"You said: {user_message}\n\nUse /help to see what I can do!")

def main() -> None:
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ping", ping))
    application.add_handler(CommandHandler("echo", echo))

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot
    print("🤖 Bot is running... Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
