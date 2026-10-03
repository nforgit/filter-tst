import logging
import sys
import traceback
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------
# Logging Setup (Filter out HTTP noise)
# ----------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress verbose HTTP logs from httpx / telegram
logging.getLogger("httpx").setLevel(logging.WARNING)

# ----------------------------------------------------
# Configuration & In-Memory State
# ----------------------------------------------------
BOT_TOKEN = "8788641970:AAF9gFcFaB-LzPV6BghnS8vYJTHsS5LYkxI"

# Per-User Quiz Builder Dictionaries: user_id -> builder_dict
user_builders = {}

# Quizzes database indexed by quiz_id
# Structure: { quiz_id: {"owner_id": int, "questions": list, "ratio": int/None, "success_link": str, "fail_message": str, "block_on_fail": bool} }
quizzes = {}
quiz_counter = 1000

# Active User Sessions: user_id -> dict with session state
user_sessions = {}

# Tracks active user editing inputs: user_id -> {"quiz_id": str, "field": str}
editing_states = {}

# ----------------------------------------------------
# Helper Functions
# ----------------------------------------------------
def log_action(action_type: str, user, details: str):
    """Clean shell logger for owner and user actions."""
    username = f"@{user.username}" if user.username else f"ID:{user.id}"
    print(f"[ACTION LOG] [{action_type}] User: {username} ({user.first_name}) | {details}")


def get_user_builder(user_id: int) -> dict:
    """Retrieves or initializes an isolated builder instance for a user."""
    if user_id not in user_builders:
        user_builders[user_id] = {
            "questions": [],  # List of dicts: {"q": text, "answers": [{"text": str, "is_correct": bool}]}
            "ratio": None,  # None means all questions must be answered correctly by default
            "success_link": None,
            "fail_message": "Sorry, you did not pass the quiz.",
            "block_on_fail": False,
        }
    return user_builders[user_id]


def build_quiz_settings_menu(quiz_id: str, quiz_data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Builds textual overview and inline keyboard for editing a specific quiz."""
    q_count = len(quiz_data["questions"])
    ratio_val = quiz_data["ratio"] if quiz_data["ratio"] is not None else f"All ({q_count})"
    block_val = "🟢 ON" if quiz_data["block_on_fail"] else "🔴 OFF"

    text = (
        f"⚙️ **Editing Panel for Quiz ID:** `{quiz_id}`\n\n"
        f"❓ **Total Questions:** {q_count}\n"
        f"🎯 **Passing Threshold:** {ratio_val} correct answers\n"
        f"🔗 **Success Link:** {quiz_data['success_link']}\n"
        f"💔 **Fail Message:** \"{quiz_data['fail_message']}\"\n"
        f"🚫 **Block on Failure:** {block_val}\n\n"
        f"👇 _Select an option below to modify this quiz:_"
    )

    buttons = [
        [
            InlineKeyboardButton("🔗 Edit Success Link", callback_data=f"editfield_link_{quiz_id}"),
            InlineKeyboardButton("🎯 Edit Ratio", callback_data=f"editfield_ratio_{quiz_id}"),
        ],
        [
            InlineKeyboardButton("💔 Edit Fail Message", callback_data=f"editfield_fail_{quiz_id}"),
            InlineKeyboardButton("🚫 Toggle Block Status", callback_data=f"editfield_block_{quiz_id}"),
        ],
        [
            InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"editfield_delete_{quiz_id}"),
            InlineKeyboardButton("🔙 Back to Quiz List", callback_data="edit_back_list"),
        ]
    ]

    return text, InlineKeyboardMarkup(buttons)


# ----------------------------------------------------
# Owner Commands
# ----------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_action("COMMAND", user, "/start executed")

    # Check if user clicked a deep-link quiz parameter: /start quiz_1000
    if context.args and context.args[0].startswith("quiz_"):
        quiz_id = context.args[0]
        if quiz_id in quizzes:
            # Start Quiz Session for User2
            user_sessions[user.id] = {
                "quiz_id": quiz_id,
                "current_q_index": 0,
                "answers_given": {},  # q_index -> chosen_ans_index
                "last_msg_id": None,
            }
            log_action("QUIZ_START", user, f"Started {quiz_id}")
            await send_quiz_question(update, context, user.id)
            return
        else:
            await update.message.reply_text("❌ Quiz not found or expired.")
            return

    # Default /start message for Owner / General instructions
    builder = get_user_builder(user.id)
    block_state = "🟢 ON" if builder["block_on_fail"] else "🔴 OFF"
    ratio_str = f"{builder['ratio']}" if builder['ratio'] is not None else "All Questions (Default)"

    help_text = (
        "🤖 **Quiz Bot Control Panel & Instructions**\n\n"
        "Welcome! Here are all the available commands:\n\n"
        "📝 **Quiz Creation Commands**\n"
        "/q <question text> - Set a new question\n"
        "/a ans1, ans2 +, ans3 - Add multiple answers separated by commas (Add '+' for correct)\n"
        f"/ratio <number> - Set required correct answers for success [Current: {ratio_str}]\n"
        "/link <url> - Set success link revealed upon passing\n"
        "/fail <message> - Set custom failure response message\n"
        f"/block - Toggle blocking user upon quiz failure [Current: {block_state}]\n"
        "/finish - Save quiz and generate special shareable link\n\n"
        "🛠️ **Quiz Management**\n"
        "/edit - Open interactive panel to edit your existing quizzes\n\n"
        "ℹ️ **General**\n"
        "/start - Display this control panel and command list\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log_action("COMMAND", user, "/edit executed")

    # Clear any active text input prompt for user
    editing_states.pop(user.id, None)

    # Find all quizzes created by this user
    user_quiz_ids = [q_id for q_id, q_data in quizzes.items() if q_data.get("owner_id") == user.id]

    if not user_quiz_ids:
        await update.message.reply_text("⚠️ You haven't created any quizzes yet! Use `/q` to start creating one.", parse_mode="Markdown")
        return

    buttons = []
    for q_id in user_quiz_ids:
        q_data = quizzes[q_id]
        q_count = len(q_data["questions"])
        buttons.append([InlineKeyboardButton(f"📝 {q_id} ({q_count} Questions)", callback_data=f"editselect_{q_id}")])

    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        "🛠️️ **Your Created Quizzes**\n\nSelect a quiz below to manage or edit its settings:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def set_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("⚠️ Usage: `/q What is 2 + 2?`", parse_mode="Markdown")
        return

    builder = get_user_builder(user.id)
    builder["questions"].append({"q": text, "answers": []})
    log_action("BUILDER", user, f"Added Question #{len(builder['questions'])}: '{text}'")
    await update.message.reply_text(
        f"❓ **Question #{len(builder['questions'])} Set!**\n\n\"{text}\"\n\nNow use `/a` to add choice options separated by commas.",
        parse_mode="Markdown",
    )


async def set_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    builder = get_user_builder(user.id)

    if not builder["questions"]:
        await update.message.reply_text("⚠️ Please set a question first using `/q <question>`.", parse_mode="Markdown")
        return

    full_text = " ".join(context.args)
    if not full_text:
        await update.message.reply_text("⚠️ Usage: `/a answer1, answer2, answer3 +, answer4+`", parse_mode="Markdown")
        return

    # Split answers by comma
    raw_answers = full_text.split(",")
    current_q = builder["questions"][-1]
    added_summary = []

    for raw in raw_answers:
        raw_trimmed = raw.strip()
        if not raw_trimmed:
            continue

        is_correct = raw_trimmed.endswith("+")
        ans_text = raw_trimmed[:-1].strip() if is_correct else raw_trimmed.strip()

        current_q["answers"].append({"text": ans_text, "is_correct": is_correct})
        status = "✅ Correct" if is_correct else "❌ Incorrect"
        added_summary.append(f"• \"{ans_text}\" ({status})")

    log_action("BUILDER", user, f"Added {len(added_summary)} answers to Q#{len(builder['questions'])}")

    summary_str = "\n".join(added_summary)
    await update.message.reply_text(
        f"🔘 **Answers Added to Question #{len(builder['questions'])}**\n\n{summary_str}",
        parse_mode="Markdown",
    )


async def set_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/ratio 3` (Provide a valid number)", parse_mode="Markdown")
        return

    val = int(context.args[0])
    builder = get_user_builder(user.id)
    builder["ratio"] = val
    log_action("BUILDER", user, f"Set passing ratio to {val}")
    await update.message.reply_text(f"🎯 **Passing Threshold Set!**\nUsers need at least **{val}** correct answers to pass.", parse_mode="Markdown")


async def set_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/link https://t.me/yourlink`", parse_mode="Markdown")
        return

    link = context.args[0]
    builder = get_user_builder(user.id)
    builder["success_link"] = link
    log_action("BUILDER", user, f"Set success link: {link}")
    await update.message.reply_text(f"🔗 **Success Link Set!**\nLink: `{link}`", parse_mode="Markdown")


async def set_fail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("⚠️ Usage: `/fail Sorry, you failed the quiz.`", parse_mode="Markdown")
        return

    builder = get_user_builder(user.id)
    builder["fail_message"] = msg
    log_action("BUILDER", user, f"Set fail message: {msg}")
    await update.message.reply_text(f"💔 **Failure Response Message Set!**\nMessage: \"{msg}\"", parse_mode="Markdown")


async def toggle_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    builder = get_user_builder(user.id)
    builder["block_on_fail"] = not builder["block_on_fail"]
    state_str = "🟢 ON (Users will be blocked on failure)" if builder["block_on_fail"] else "🔴 OFF (No blocking)"
    
    log_action("BUILDER", user, f"Toggled block-on-fail to {builder['block_on_fail']}")
    await update.message.reply_text(f"🚫 **Block On Failure Toggled!**\nCurrent Status: **{state_str}**", parse_mode="Markdown")


async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global quiz_counter
    user = update.effective_user
    builder = get_user_builder(user.id)

    if not builder["questions"]:
        await update.message.reply_text("⚠️ Cannot finish quiz: No questions created yet!")
        return
    if not builder["success_link"]:
        await update.message.reply_text("⚠️ Cannot finish quiz: Please set a success link using `/link <url>` first!")
        return

    quiz_id = f"quiz_{quiz_counter}"
    quiz_counter += 1

    # Save permanent snapshot of the quiz with owner_id attached
    quizzes[quiz_id] = {
        "owner_id": user.id,
        "questions": list(builder["questions"]),
        "ratio": builder["ratio"],
        "success_link": builder["success_link"],
        "fail_message": builder["fail_message"],
        "block_on_fail": builder["block_on_fail"],
    }

    bot_info = await context.bot.get_me()
    share_link = f"https://t.me/{bot_info.username}?start={quiz_id}"

    log_action("BUILDER", user, f"Finished & Generated {quiz_id}")

    # Reset user's builder draft so they can create another quiz cleanly
    del user_builders[user.id]

    await update.message.reply_text(
        f"🎉 **Quiz Created Successfully!**\n\nShare this special link with participants:\n`{share_link}`\n\nUse `/edit` anytime to update quiz details.",
        parse_mode="Markdown",
    )


# ----------------------------------------------------
# General Message Handler for Input Interception (Editing State)
# ----------------------------------------------------
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in editing_states:
        state = editing_states.pop(user.id)
        quiz_id = state["quiz_id"]
        field = state["field"]

        if quiz_id not in quizzes or quizzes[quiz_id].get("owner_id") != user.id:
            await update.message.reply_text("❌ Quiz no longer exists or permissions changed.")
            return

        input_text = update.message.text.strip()
        quiz = quizzes[quiz_id]

        if field == "link":
            quiz["success_link"] = input_text
            await update.message.reply_text(f"✅ Success link updated to: `{input_text}` for `{quiz_id}`", parse_mode="Markdown")
        elif field == "ratio":
            if input_text.isdigit():
                quiz["ratio"] = int(input_text)
                await update.message.reply_text(f"✅ Required passing ratio updated to **{input_text}** for `{quiz_id}`", parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Invalid number provided. Editing canceled.")
        elif field == "fail":
            quiz["fail_message"] = input_text
            await update.message.reply_text(f"✅ Fail message updated for `{quiz_id}`", parse_mode="Markdown")

        # Show updated settings panel
        text, markup = build_quiz_settings_menu(quiz_id, quiz)
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


# ----------------------------------------------------
# Quiz Execution Mechanics (User2)
# ----------------------------------------------------
def build_question_keyboard(q_idx: int, question_obj: dict, selected_a_idx: int = None):
    buttons = []
    for a_idx, ans in enumerate(question_obj["answers"]):
        prefix = "🔘 "
        if selected_a_idx is not None and a_idx == selected_a_idx:
            prefix = "✅ "
        buttons.append([InlineKeyboardButton(f"{prefix}{ans['text']}", callback_data=f"ans_{q_idx}_{a_idx}")])
    return InlineKeyboardMarkup(buttons)


async def send_quiz_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions.get(user_id)
    if not session:
        return

    quiz_data = quizzes[session["quiz_id"]]
    q_idx = session["current_q_index"]
    question_obj = quiz_data["questions"][q_idx]

    selected_a_idx = session["answers_given"].get(q_idx)
    reply_markup = build_question_keyboard(q_idx, question_obj, selected_a_idx)

    selected_str = f"\n\nSelected: **{question_obj['answers'][selected_a_idx]['text']}**" if selected_a_idx is not None else ""
    text = (
        f"❓ **Question {q_idx + 1} of {len(quiz_data['questions'])}**\n\n"
        f"{question_obj['q']}{selected_str}\n\n"
        f"📌 _Click /next to lock in your final choice and proceed to the next question._"
    )

    sent_msg = await context.bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )
    session["last_msg_id"] = sent_msg.message_id


async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session = user_sessions.get(user.id)

    if not session:
        await update.message.reply_text("⚠️ You do not have an active quiz session.")
        return

    q_idx = session["current_q_index"]
    if q_idx not in session["answers_given"]:
        await update.message.reply_text("⚠️ Please select an answer option above before clicking `/next`!", parse_mode="Markdown")
        return

    quiz_data = quizzes[session["quiz_id"]]

    # Lock in previous message: remove buttons
    if session.get("last_msg_id"):
        try:
            chosen_idx = session["answers_given"][q_idx]
            chosen_ans = quiz_data["questions"][q_idx]["answers"][chosen_idx]["text"]
            await context.bot.edit_message_text(
                chat_id=user.id,
                message_id=session["last_msg_id"],
                text=f"❓ **Question {q_idx + 1} of {len(quiz_data['questions'])}**\n\n{quiz_data['questions'][q_idx]['q']}\n\n🔒 **Final Answer:** {chosen_ans}",
                parse_mode="Markdown",
                reply_markup=None,
            )
        except Exception:
            pass

    session["current_q_index"] += 1

    if session["current_q_index"] < len(quiz_data["questions"]):
        await send_quiz_question(update, context, user.id)
    else:
        # Quiz complete - evaluate results
        score = 0
        total_questions = len(quiz_data["questions"])
        for q_i, chosen_a_i in session["answers_given"].items():
            if quiz_data["questions"][q_i]["answers"][chosen_a_i]["is_correct"]:
                score += 1

        # Dynamic ratio fallback: If User1 didn't set ratio, require ALL questions (100%)
        required_score = quiz_data["ratio"] if quiz_data["ratio"] is not None else total_questions

        log_action("QUIZ_FINISH", user, f"Finished {session['quiz_id']} | Final Score: {score}/{total_questions} (Req: {required_score})")

        if score >= required_score:
            await update.message.reply_text(
                f"🎉 **Congratulations! You passed the quiz!**\n\nHere is your success link:\n{quiz_data['success_link']}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(f"❌ {quiz_data['fail_message']}")
            if quiz_data["block_on_fail"]:
                log_action("QUIZ_BLOCK", user, "Blocked and session deleted due to quiz failure.")
                try:
                    await context.bot.ban_chat_member(chat_id=user.id, user_id=user.id)
                except Exception as ex:
                    log_action("ERROR", user, f"Could not block user in DM: {ex}")

        del user_sessions[user.id]


# ----------------------------------------------------
# Callback Query Handler (Inline Buttons & Edit Panel)
# ----------------------------------------------------
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # Always acknowledge clicks promptly

    data = query.data
    user = update.effective_user

    # Handle returning back to the general edit menu list
    if data == "edit_back_list":
        user_quiz_ids = [q_id for q_id, q_data in quizzes.items() if q_data.get("owner_id") == user.id]
        if not user_quiz_ids:
            await query.edit_message_text("⚠️ You haven't created any quizzes yet.")
            return

        buttons = [[InlineKeyboardButton(f"📝 {q_id} ({len(quizzes[q_id]['questions'])} Questions)", callback_data=f"editselect_{q_id}")] for q_id in user_quiz_ids]
        await query.edit_message_text(
            "🛠️ **Your Created Quizzes**\n\nSelect a quiz below to manage or edit its settings:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )
        return

    # Handle selecting a specific quiz to edit
    if data.startswith("editselect_"):
        quiz_id = data.split("_", 1)[1]
        if quiz_id in quizzes and quizzes[quiz_id].get("owner_id") == user.id:
            text, markup = build_quiz_settings_menu(quiz_id, quizzes[quiz_id])
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Quiz not found or permission denied.")
        return

    # Handle field-specific editing actions
    if data.startswith("editfield_"):
        parts = data.split("_")
        action = parts[1]
        quiz_id = f"{parts[2]}_{parts[3]}"

        if quiz_id not in quizzes or quizzes[quiz_id].get("owner_id") != user.id:
            await query.edit_message_text("❌ Quiz not found or permission denied.")
            return

        quiz = quizzes[quiz_id]

        if action == "block":
            quiz["block_on_fail"] = not quiz["block_on_fail"]
            text, markup = build_quiz_settings_menu(quiz_id, quiz)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        elif action == "delete":
            del quizzes[quiz_id]
            await query.edit_message_text(f"🗑️ Quiz `{quiz_id}` has been permanently deleted.", parse_mode="Markdown")
        elif action in ["link", "ratio", "fail"]:
            editing_states[user.id] = {"quiz_id": quiz_id, "field": action}
            prompts = {
                "link": "🔗 Please send the new **Success Link** in your next message:",
                "ratio": f"🎯 Please send the new **Passing Ratio** number (Total questions: {len(quiz['questions'])}):",
                "fail": "💔 Please send the new custom **Fail Message** in your next message:",
            }
            await query.message.reply_text(prompts[action], parse_mode="Markdown")
        return

    # Handle quiz answer button selection (User2 execution)
    if data.startswith("ans_"):
        parts = data.split("_")
        q_idx, a_idx = int(parts[1]), int(parts[2])

        session = user_sessions.get(user.id)
        if not session:
            await query.message.reply_text("⚠️ Active session expired or not found.")
            return

        quiz_data = quizzes[session["quiz_id"]]
        question_obj = quiz_data["questions"][q_idx]

        # Update user's current selection for this question
        session["answers_given"][q_idx] = a_idx
        ans_obj = question_obj["answers"][a_idx]
        log_action("QUIZ_SELECTION", user, f"Q#{q_idx+1} selected: '{ans_obj['text']}'")

        # Re-render keyboard with the newly selected item highlighted
        reply_markup = build_question_keyboard(q_idx, question_obj, a_idx)
        text = (
            f"❓ **Question {q_idx + 1} of {len(quiz_data['questions'])}**\n\n"
            f"{question_obj['q']}\n\nSelected: **{ans_obj['text']}**\n\n"
            f"📌 _Click /next to lock in your final choice and proceed to the next question._"
        )

        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )
        except Exception:
            pass


# ----------------------------------------------------
# Main Execution Entrypoint
# ----------------------------------------------------
def main():
    try:
        app = Application.builder().token(BOT_TOKEN).build()

        # Command handlers
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("edit", edit_command))
        app.add_handler(CommandHandler("q", set_question))
        app.add_handler(CommandHandler("a", set_answer))
        app.add_handler(CommandHandler("ratio", set_ratio))
        app.add_handler(CommandHandler("link", set_link))
        app.add_handler(CommandHandler("fail", set_fail))
        app.add_handler(CommandHandler("block", toggle_block))
        app.add_handler(CommandHandler("finish", finish_quiz))
        app.add_handler(CommandHandler("next", next_command))

        # Inline button handler
        app.add_handler(CallbackQueryHandler(handle_callback_query))

        # Text input handler for edit prompts
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

        print("🤖 Quiz Telegram Bot is up and running...")

        # Explicitly configure allowed_updates for inline queries and messages
        app.run_polling(allowed_updates=["message", "callback_query"])

    except Exception as e:
        print("\n❌ CRITICAL EXCEPTION ENCOUNTERED IN MAIN EXECUTION:\n")
        traceback.print_exc()
    finally:
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()