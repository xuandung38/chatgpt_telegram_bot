import os
import logging
import traceback
import html
import json
import tempfile
import pydub
from pathlib import Path
from datetime import datetime

import telegram
from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)
from telegram.constants import ParseMode, ChatAction

import config
import database
import openai_utils

# setup
db = database.Database()
logger = logging.getLogger(__name__)

HELP_MESSAGE = """Commands:
⚪ /retry – Tạo lại câu trả lời cuối cùng
⚪ /new – Bắt đầu hộp thoại mới
⚪ /mode – Chọn chế độ trò chuyện
⚪ /ask – Hỏi một câu hỏi mới
⚪ /help – Hiển thị trợ giúp
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    reply_text = f"Chào {update.message.from_user.name} ! Em là <b>Layla</b> Thư ký ảo của FoxSolutions 🤖\n\n"
    reply_text += HELP_MESSAGE

    reply_text += f"\nHãy hỏi em bất kỳ điều gì {update.message.from_user.name} muốn ạ!"

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("Không có tin nhắn nào để thử lại 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # new dialog timeout
    if use_new_dialog_timeout:
        if (datetime.now() - db.get_user_attribute(user_id,
                                                   "last_interaction")).seconds > config.new_dialog_timeout and len(
                db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)
            await update.message.reply_text(
                f"Bắt đầu hộp thoại mới do hết thời gian chờ (<b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> mode) ✅",
                parse_mode=ParseMode.HTML)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # send typing action
    await update.message.chat.send_action(action="typing")

    try:
        message = message or update.message.text

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

        chatgpt_instance = openai_utils.ChatGPT(use_chatgpt_api=config.use_chatgpt_api)
        answer, n_used_tokens, n_first_dialog_messages_removed = await chatgpt_instance.send_message(
            message,
            dialog_messages=dialog_messages,
            chat_mode=chat_mode
        )

        # update user data
        new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}
        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))

    except Exception as e:
        error_text = f"Đã xảy ra sự cố trong quá trình hoàn thành. Lý do: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

    # send message if some messages were removed from the context
    if n_first_dialog_messages_removed > 0:
        if n_first_dialog_messages_removed == 1:
            text = "✍️ <i>Lưu ý:</i> Hộp thoại hiện tại của bạn quá dài nên <b>tin nhắn đầu tiên</b> của bạn đã bị xóa khỏi ngữ cảnh.\n Gửi lệnh /new để bắt đầu hộp thoại mới"
        else:
            text = f"✍️ <i>Lưu ý:</i> Hộp thoại hiện tại của bạn quá dài nên <b>{n_first_dialog_messages_removed} tin nhắn đầu tiên</b> đã bị xóa khỏi ngữ cảnh.\n Gửi lệnh /new để bắt đầu hộp thoại mới"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # split answer into multiple messages due to 4096 character limit
    for answer_chunk in split_text_into_chunks(answer, 4000):
        try:
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[openai_utils.CHAT_MODES[chat_mode]["parse_mode"]]

            await update.message.reply_text(answer_chunk, parse_mode=parse_mode)
        except telegram.error.BadRequest:
            # answer has invalid characters, so we send it without parse_mode
            await update.message.reply_text(answer_chunk)


async def voice_message_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    voice = update.message.voice
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        voice_ogg_path = tmp_dir / "voice.ogg"

        # download
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(voice_ogg_path)

        # convert to mp3
        voice_mp3_path = tmp_dir / "voice.mp3"
        pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

        # transcribe
        with open(voice_mp3_path, "rb") as f:
            transcribed_text = await openai_utils.transcribe_audio(f)

    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    await message_handle(update, context, message=transcribed_text)

    # calculate spent dollars
    n_spent_dollars = voice.duration * (config.whisper_price_per_1_min / 60)

    # normalize dollars to tokens (it's very convenient to measure everything in a single unit)
    price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
    n_used_tokens = int(n_spent_dollars / (price_per_1000_tokens / 1000))
    db.set_user_attribute(user_id, "n_used_tokens", n_used_tokens + db.get_user_attribute(user_id, "n_used_tokens"))


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    db.start_new_dialog(user_id)
    await update.message.reply_text("Khởi tạo hội thoại mới")

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    await update.message.reply_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}",
                                    parse_mode=ParseMode.HTML)


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    keyboard = []
    for chat_mode, chat_mode_dict in openai_utils.CHAT_MODES.items():
        keyboard.append([InlineKeyboardButton(chat_mode_dict["name"], callback_data=f"set_chat_mode|{chat_mode}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Chọn chế độ trò chuyện: ", reply_markup=reply_markup)


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await query.edit_message_text(
        f"Chế độ trò chuyện <b>{openai_utils.CHAT_MODES[chat_mode]['name']}</b> được thiết lập",
        parse_mode=ParseMode.HTML
    )

    await query.edit_message_text(f"{openai_utils.CHAT_MODES[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)


async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    n_used_tokens = db.get_user_attribute(user_id, "n_used_tokens")

    price_per_1000_tokens = config.chatgpt_price_per_1000_tokens if config.use_chatgpt_api else config.gpt_price_per_1000_tokens
    n_spent_dollars = n_used_tokens * (price_per_1000_tokens / 1000)

    text = f"Bạn đã chi <b>{n_spent_dollars:.03f}$</b>\n"
    text += f"Bạn đã dùng <b>{n_used_tokens}</b> tokens\n\n"

    text += "🏷️ Giá\n"
    text += f"<i>- ChatGPT: {price_per_1000_tokens}$ mỗi 1000 tokens\n"
    text += f"- Whisper (nhận diện giọng nói): {config.whisper_price_per_1_min}$ mỗi 1 phút</i>"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def edited_message_handle(update: Update, context: CallbackContext):
    text = "🥲 Rất tiếc, không hỗ trợ <b>chỉnh sửa</b> tin nhắn"
    await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # split text into multiple messages due to 4096 character limit
        for message_chunk in split_text_into_chunks(message, 4000):
            try:
                await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
            except telegram.error.BadRequest:
                # answer has invalid characters, so we send it without parse_mode
                await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")


async def ask_bot_handle(update: Update, context: CallbackContext) -> None:
    try:
        ask_message = update.message.text.replace('/ask', '')
        print(ask_message)

        if len(ask_message) < 0:
            await update.message.reply_text(f"Xin lỗi, em không thể hểu câu hỏi của {update.message.from_user.name} ạ!")
            return

        await message_handle(update, context, message=ask_message)
    except Exception as e:
        error_text = f"Đã xảy ra sự cố. Lý do: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .build()
    )

    # add handlers
    if len(config.allowed_telegram_usernames) == 0:
        user_filter = filters.ALL
    else:
        user_filter = filters.User(username=config.allowed_telegram_usernames)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))

    application.add_handler(CommandHandler("ask", ask_bot_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


if __name__ == "__main__":
    run_bot()
