"""
Games — new plugin, not present in any of the original four projects.

Betting commands (coinflip, slots) and rewards (trivia, guess) go
through bot.services.economy_service, the same module plugins/economy.py
uses, so balances stay consistent everywhere. Game state (trivia in
progress, an active tic-tac-toe board, a number-guessing target) is kept
in memory, scoped per chat — restarting the bot mid-game simply ends it,
which is the standard, acceptable trade-off for lightweight chat games.

All inline-keyboard callback_data is namespaced "game:" so it can never
collide with the font picker's "font:" callbacks or the main menu's
bare "page:"/"close" callbacks — see plugins/fonts.py for the full
explanation of why that matters.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from bot.config import settings
from bot.services.economy_service import add_coins, add_xp, try_spend
from bot.utils.helpers import escape_html, resolve_target_user
from bot.core.errors import safe_handler

TRIVIA_QUESTIONS = [
    ("general", "What is the largest planet in our solar system?", ["Jupiter", "Saturn", "Neptune", "Earth"], 0),
    ("general", "How many continents are there?", ["5", "6", "7", "8"], 2),
    ("general", "What is the smallest prime number?", ["0", "1", "2", "3"], 2),
    ("science", "What gas do plants absorb from the atmosphere?", ["Oxygen", "Nitrogen", "CO2", "Hydrogen"], 2),
    ("science", "What is the chemical symbol for gold?", ["Go", "Gd", "Au", "Ag"], 2),
    ("science", "How many bones are in the adult human body?", ["186", "206", "226", "246"], 1),
    ("geography", "What is the longest river in the world?", ["Amazon", "Nile", "Yangtze", "Mississippi"], 1),
    ("geography", "Which country has the most time zones?", ["Russia", "USA", "France", "China"], 2),
    ("geography", "What is the smallest country in the world?", ["Monaco", "Vatican City", "San Marino", "Malta"], 1),
    ("movies", "Who directed 'Jaws' and 'E.T.'?", ["George Lucas", "Steven Spielberg", "James Cameron", "Ridley Scott"], 1),
    ("movies", "What is the highest-grossing film of all time (unadjusted)?", ["Titanic", "Avatar", "Avengers: Endgame", "Star Wars"], 1),
    ("programming", "What does 'HTTP' stand for?", [
        "HyperText Transfer Protocol", "HighText Transmission Process",
        "HyperText Transmission Protocol", "HyperTransfer Text Protocol",
    ], 0),
    ("programming", "Which language is famous for significant whitespace?", ["Java", "C++", "Python", "Rust"], 2),
    ("programming", "What does 'SQL' stand for?", [
        "Structured Query Language", "Simple Query Logic",
        "Sequential Query Language", "Standard Query Language",
    ], 0),
    ("programming", "What year was Python first released?", ["1989", "1991", "1995", "2000"], 1),
    ("general", "What is the tallest mountain in the world?", ["K2", "Kangchenjunga", "Mount Everest", "Denali"], 2),
    ("science", "What planet is known as the Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1),
    ("geography", "What is the capital of Australia?", ["Sydney", "Melbourne", "Canberra", "Perth"], 2),
    ("movies", "Which studio produced 'Toy Story'?", ["DreamWorks", "Disney/Pixar", "Universal", "Warner Bros"], 1),
    ("programming", "What does 'CSS' stand for?", [
        "Computer Style Sheets", "Cascading Style Sheets",
        "Creative Style Syntax", "Colorful Style Sheets",
    ], 1),
]

RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
SLOT_PAYOUTS = {"7️⃣": 10, "💎": 6, "🔔": 4, "🍇": 3, "🍋": 2, "🍒": 2}


@dataclass
class TriviaState:
    question: str
    options: List[str]
    correct_index: int
    category: str
    solved: bool = False


@dataclass
class GuessState:
    target: int
    max_value: int
    attempts: int = 0


@dataclass
class TicTacToeState:
    board: List[str] = field(default_factory=lambda: [""] * 9)
    players: List[int] = field(default_factory=list)  # [X's id, O's id]
    names: List[str] = field(default_factory=lambda: ["", ""])
    turn: int = 0  # index into players/names

    @property
    def symbol(self) -> str:
        return "X" if self.turn == 0 else "O"


_trivia_by_key: Dict[str, TriviaState] = {}
_guess_state: Dict[int, GuessState] = {}       # keyed by chat_id
_ttt_state: Dict[int, TicTacToeState] = {}     # keyed by chat_id


def _clamp_bet(amount: int) -> Optional[int]:
    if amount < settings.games_min_bet or amount > settings.games_max_bet:
        return None
    return amount


# ==================== TRIVIA ====================

@safe_handler
async def trivia_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = context.args[0].lower() if context.args else None
    pool = [q for q in TRIVIA_QUESTIONS if not category or q[0] == category]
    if not pool:
        cats = sorted({q[0] for q in TRIVIA_QUESTIONS})
        await update.message.reply_text(f"❌ Unknown category. Try one of: {', '.join(cats)}")
        return

    cat, question, options, correct_idx = random.choice(pool)
    order = list(range(len(options)))
    random.shuffle(order)
    shuffled = [options[i] for i in order]
    new_correct_idx = order.index(correct_idx)

    msg = await update.message.reply_text(
        f"🧠 <b>Trivia</b> <i>({escape_html(cat)})</i>\n\n{escape_html(question)}",
        parse_mode="HTML",
    )
    key = f"{update.effective_chat.id}:{msg.message_id}"
    _trivia_by_key[key] = TriviaState(question, shuffled, new_correct_idx, cat)

    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"game:trivia:{key}:{i}")]
        for i, opt in enumerate(shuffled)
    ]
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_trivia_answer(query, key: str, choice: int):
    state = _trivia_by_key.get(key)
    if not state or state.solved:
        await query.answer("This question is already closed.", show_alert=False)
        return

    if choice != state.correct_index:
        await query.answer("❌ Wrong!", show_alert=False)
        return

    state.solved = True
    user = query.from_user
    chat_id = int(key.split(":")[0])
    new_level, leveled_up = await add_xp(chat_id, user.id, 20)
    await add_coins(chat_id, user.id, 30)

    await query.answer("✅ Correct!", show_alert=False)
    await query.edit_message_text(
        f"🧠 <b>Trivia</b> <i>({escape_html(state.category)})</i>\n\n{escape_html(state.question)}\n\n"
        f"✅ <b>{escape_html(user.first_name)}</b> got it! +20 XP, +30 🪙"
        + (f" · leveled up to {new_level}!" if leveled_up else ""),
        parse_mode="HTML",
    )
    _trivia_by_key.pop(key, None)


# ==================== ROCK PAPER SCISSORS ====================

@safe_handler
async def rps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = (context.args[0].lower() if context.args else "")
    if choice not in RPS_BEATS:
        await update.message.reply_text("Usage: /rps rock|paper|scissors")
        return
    bot_choice = random.choice(list(RPS_BEATS))

    if choice == bot_choice:
        result = "🤝 It's a tie!"
    elif RPS_BEATS[choice] == bot_choice:
        result = "🎉 You win!"
    else:
        result = "🤖 I win!"

    await update.message.reply_text(
        f"{RPS_EMOJI[choice]} vs {RPS_EMOJI[bot_choice]}\n{result}"
    )


# ==================== COINFLIP & SLOTS (betting) ====================

@safe_handler
async def coinflip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("🎲 Betting games are per-group — try this in a group chat.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /coinflip <amount {settings.games_min_bet}-{settings.games_max_bet}>")
        return
    amount = _clamp_bet(int(context.args[0]))
    if amount is None:
        await update.message.reply_text(f"❌ Bet must be between {settings.games_min_bet} and {settings.games_max_bet}.")
        return

    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    if not await try_spend(chat_id, user_id, amount):
        await update.message.reply_text("❌ You don't have enough coins.")
        return

    won = random.random() < 0.5
    if won:
        new_balance = await add_coins(chat_id, user_id, amount * 2)
        await update.message.reply_text(f"🪙 Heads! You won {amount:,} 🪙 (balance: {new_balance:,})")
    else:
        new_balance = await add_coins(chat_id, user_id, 0)
        await update.message.reply_text(f"🪙 Tails! You lost {amount:,} 🪙 (balance: {new_balance:,})")


@safe_handler
async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("🎰 Betting games are per-group — try this in a group chat.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /slots <amount {settings.games_min_bet}-{settings.games_max_bet}>")
        return
    amount = _clamp_bet(int(context.args[0]))
    if amount is None:
        await update.message.reply_text(f"❌ Bet must be between {settings.games_min_bet} and {settings.games_max_bet}.")
        return

    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    if not await try_spend(chat_id, user_id, amount):
        await update.message.reply_text("❌ You don't have enough coins.")
        return

    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    display = " | ".join(reels)

    if reels[0] == reels[1] == reels[2]:
        payout = amount * SLOT_PAYOUTS[reels[0]]
        new_balance = await add_coins(chat_id, user_id, payout)
        await update.message.reply_text(f"🎰 {display}\n🎉 JACKPOT! +{payout:,} 🪙 (balance: {new_balance:,})")
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        payout = int(amount * 1.5)
        new_balance = await add_coins(chat_id, user_id, payout)
        await update.message.reply_text(f"🎰 {display}\n✨ Two of a kind! +{payout:,} 🪙 (balance: {new_balance:,})")
    else:
        new_balance = await add_coins(chat_id, user_id, 0)
        await update.message.reply_text(f"🎰 {display}\n💨 No match. Lost {amount:,} 🪙 (balance: {new_balance:,})")


# ==================== NUMBER GUESSING ====================

@safe_handler
async def guess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if context.args and context.args[0].lower() == "start":
        max_value = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 100
        _guess_state[chat_id] = GuessState(target=random.randint(1, max_value), max_value=max_value)
        await update.message.reply_text(f"🔢 I'm thinking of a number between 1 and {max_value}. /guess <number> to guess!")
        return

    state = _guess_state.get(chat_id)
    if not state:
        await update.message.reply_text("No game running. Start one with /guess start [max]")
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /guess <number>  (or /guess start [max] for a new game)")
        return

    guess = int(context.args[0])
    state.attempts += 1

    if guess == state.target:
        reward = max(10, 100 - state.attempts * 5)
        new_balance = await add_coins(chat_id, update.effective_user.id, reward)
        await add_xp(chat_id, update.effective_user.id, 10)
        await update.message.reply_text(
            f"🎉 Correct! It was {state.target}. Solved in {state.attempts} guesses. "
            f"+{reward:,} 🪙 (balance: {new_balance:,})"
        )
        del _guess_state[chat_id]
    elif guess < state.target:
        await update.message.reply_text("📈 Higher!")
    else:
        await update.message.reply_text("📉 Lower!")


# ==================== TIC-TAC-TOE ====================

def _ttt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = _ttt_state[chat_id]
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            label = state.board[i] or "·"
            row.append(InlineKeyboardButton(label, callback_data=f"game:ttt:{chat_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _ttt_winner(board: List[str]) -> Optional[str]:
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


@safe_handler
async def tictactoe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ Tic-tac-toe needs two players — try this in a group chat.")
        return

    opponent_id, opponent_name = await resolve_target_user(update, context)
    if not opponent_id:
        await update.message.reply_text("Usage: reply to someone with /tictactoe, or /tictactoe @username")
        return
    if opponent_id == update.effective_user.id:
        await update.message.reply_text("❌ You can't play against yourself.")
        return

    chat_id = update.effective_chat.id
    state = TicTacToeState(players=[update.effective_user.id, opponent_id], names=[update.effective_user.first_name, opponent_name])
    _ttt_state[chat_id] = state

    await update.message.reply_text(
        f"⭕❌ <b>{escape_html(state.names[0])}</b> (X) vs <b>{escape_html(state.names[1])}</b> (O)\n"
        f"Turn: <b>{escape_html(state.names[0])}</b>",
        parse_mode="HTML",
        reply_markup=_ttt_keyboard(chat_id),
    )


async def _handle_ttt_move(query, chat_id: int, cell: int):
    state = _ttt_state.get(chat_id)
    if not state:
        await query.answer("No game running here.", show_alert=False)
        return

    player = query.from_user
    if player.id != state.players[state.turn]:
        await query.answer("It's not your turn.", show_alert=True)
        return
    if state.board[cell]:
        await query.answer("That cell is taken.", show_alert=False)
        return

    state.board[cell] = state.symbol
    winner = _ttt_winner(state.board)
    draw = not winner and all(state.board)

    await query.answer()

    if winner or draw:
        del _ttt_state[chat_id]
        if winner:
            winner_idx = 0 if winner == "X" else 1
            winner_id = state.players[winner_idx]
            await add_coins(chat_id, winner_id, 50)
            await add_xp(chat_id, winner_id, 15)
            text = f"🏆 <b>{escape_html(state.names[winner_idx])}</b> wins! (+50 🪙, +15 XP)"
        else:
            text = "🤝 It's a draw!"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_ttt_keyboard_static(state))
        return

    state.turn = 1 - state.turn
    await query.edit_message_text(
        f"⭕❌ <b>{escape_html(state.names[0])}</b> (X) vs <b>{escape_html(state.names[1])}</b> (O)\n"
        f"Turn: <b>{escape_html(state.names[state.turn])}</b>",
        parse_mode="HTML",
        reply_markup=_ttt_keyboard(chat_id),
    )


def _ttt_keyboard_static(state: TicTacToeState) -> InlineKeyboardMarkup:
    """Render the final board with disabled (no-op) buttons once a game ends."""
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            row.append(InlineKeyboardButton(state.board[i] or "·", callback_data="game:ttt:over"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ==================== CALLBACK ROUTER ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data[len("game:"):]

    if data.startswith("trivia:"):
        _, chat_id, msg_id, choice = data.split(":")
        await _handle_trivia_answer(query, f"{chat_id}:{msg_id}", int(choice))
        return

    if data.startswith("ttt:"):
        parts = data.split(":")
        if parts[1] == "over":
            await query.answer("This game has ended.", show_alert=False)
            return
        chat_id, cell = int(parts[1]), int(parts[2])
        await _handle_ttt_move(query, chat_id, cell)
        return

    await query.answer()


def register(app):
    if not settings.enable_games:
        return
    app.add_handler(CommandHandler("trivia", trivia_cmd))
    app.add_handler(CommandHandler("rps", rps_cmd))
    app.add_handler(CommandHandler("coinflip", coinflip_cmd))
    app.add_handler(CommandHandler("slots", slots_cmd))
    app.add_handler(CommandHandler("guess", guess_cmd))
    app.add_handler(CommandHandler("tictactoe", tictactoe_cmd))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^game:"))
