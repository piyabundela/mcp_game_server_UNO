"""
UNO MCP Server - Parts 1 & 3
Implements a stdio MCP server that manages a two-player UNO card game.

Usage:
    python main.py --game=<game_id> --player=<A|B>

MCP Tools:
    - Status: show current game state from this player's perspective
    - Play:   play a card from the player's hand
    - Draw:   draw a card from the draw pile
    - Wait:   block until it is this player's turn (Part 3)
"""

import asyncio
import argparse
import json
import os
import random
import time
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
)

# ─────────────────────────────────────────────────────────── #
#  Try to import Redis; fall back to file-based persistence   #
# ─────────────────────────────────────────────────────────── #
try:
    import redis as _redis_lib
    _redis_available = True
except ImportError:
    _redis_available = False

# ════════════════════════════════════════════════════════════ #
#  UNO Deck helpers                                           #
# ════════════════════════════════════════════════════════════ #
COLORS = ["Red", "Yellow", "Green", "Blue"]
NUMBERS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
ACTIONS = ["Skip", "Reverse", "Draw Two"]
WILDS = ["Wild", "Wild Draw Four"]


def build_deck() -> list:
    """Build and return a standard 108-card UNO deck."""
    deck = []
    for color in COLORS:
        deck.append(f"{color} 0")
        for num in NUMBERS[1:]:
            deck.append(f"{color} {num}")
            deck.append(f"{color} {num}")
        for action in ACTIONS:
            deck.append(f"{color} {action}")
            deck.append(f"{color} {action}")
    for wild in WILDS:
        for _ in range(4):
            deck.append(wild)
    return deck


def card_color(card: str) -> Optional[str]:
    """Return the color of a card, or None for wilds."""
    for c in COLORS:
        if card.startswith(c):
            return c
    return None


def card_type(card: str) -> str:
    """Return the type/value portion of a card string."""
    for c in COLORS:
        if card.startswith(c + " "):
            return card[len(c) + 1:]
    return card  # Wild or Wild Draw Four


def is_valid_play(card: str, top_card: str, current_color: str) -> bool:
    """Determine whether `card` can legally be played on `top_card`."""
    if card in WILDS:
        return True
    cc = card_color(card)
    if cc == current_color:
        return True
    if card_type(card) == card_type(top_card):
        return True
    return False


# ════════════════════════════════════════════════════════════ #
#  Persistence layer (Redis first, file fallback)             #
# ════════════════════════════════════════════════════════════ #
class StateStore:
    """Abstracts saving/loading game state to Redis or a local JSON file."""

    def __init__(self, game_id: str):
        self.game_id = game_id
        self._redis = None
        self._file_path = f"/tmp/uno_game_{game_id}.json"

        if _redis_available:
            try:
                r = _redis_lib.Redis(host="localhost", port=6379, decode_responses=True)
                r.ping()
                self._redis = r
            except Exception:
                self._redis = None

    def save(self, state: dict) -> None:
        payload = json.dumps(state)
        if self._redis:
            self._redis.set(f"uno:{self.game_id}", payload)
            # Publish change notification for Wait tool
            self._redis.publish(f"uno:events:{self.game_id}", payload)
        else:
            # Atomic write: write to .tmp then rename to avoid partial reads
            tmp = self._file_path + ".tmp"
            with open(tmp, "w") as f:
                f.write(payload)
            os.replace(tmp, self._file_path)

    def load(self) -> Optional[dict]:
        if self._redis:
            data = self._redis.get(f"uno:{self.game_id}")
            return json.loads(data) if data else None
        else:
            if os.path.exists(self._file_path):
                with open(self._file_path) as f:
                    return json.loads(f.read())
            return None

    def exists(self) -> bool:
        if self._redis:
            return bool(self._redis.exists(f"uno:{self.game_id}"))
        return os.path.exists(self._file_path)


# ════════════════════════════════════════════════════════════ #
#  UnoGame                                                    #
# ════════════════════════════════════════════════════════════ #
class UnoGame:
    """
    Represents a two-player UNO game. All state is persisted via the
    StateStore so that both player processes share the same game.
    """

    def __init__(self, game_id: str, player: str, store: StateStore):
        self.game_id = game_id
        self.player = player        # "A" or "B"
        self.store = store
        self.opponent = "B" if player == "A" else "A"

    # ─────────────────────────── internal helpers ──────────── #

    def _new_state(self) -> dict:
        """Create and return the initial state for a fresh game."""
        deck = build_deck()
        random.shuffle(deck)

        hand_a = [deck.pop() for _ in range(7)]
        hand_b = [deck.pop() for _ in range(7)]

        # Flip top card — skip wilds per standard rules
        while True:
            top = deck.pop()
            if top not in WILDS:
                break
            deck.insert(0, top)  # put wild back at bottom

        return {
            "deck": deck,
            "discard": [top],
            "hands": {"A": hand_a, "B": hand_b},
            "current_player": "A",
            "current_color": card_color(top),
            "last_move": None,
            "winner": None,
        }

    def _load_or_create(self) -> dict:
        """
        Load existing game state, or create a new one if none exists.
        Uses atomic file write + re-read to reduce the race window when
        two processes start simultaneously.
        """
        state = self.store.load()
        if state is None:
            state = self._new_state()
            self.store.save(state)
            # Re-read what's actually on disk in case another process
            # wrote first in a tight race
            reloaded = self.store.load()
            if reloaded:
                return reloaded
        return state

    def _replenish_deck(self, state: dict) -> None:
        """If the draw pile is empty, shuffle the discard pile back in."""
        if len(state["deck"]) == 0:
            top = state["discard"][-1]
            state["deck"] = state["discard"][:-1]
            random.shuffle(state["deck"])
            state["discard"] = [top]

    # ─────────────────────────── public tools ──────────────── #

    def status(self) -> str:
        """Return a human-readable status string from this player's POV."""
        state = self._load_or_create()

        my_hand = state["hands"][self.player]
        opp_hand = state["hands"][self.opponent]
        top_card = state["discard"][-1]
        current_color = state["current_color"]
        draw_pile_size = len(state["deck"])
        current_player = state["current_player"]
        winner = state["winner"]

        lines = ["=== Your Hand ==="]
        if my_hand:
            for i, card in enumerate(my_hand, 1):
                lines.append(f" {i}. {card}")
        else:
            lines.append(" (empty)")

        lines += [
            "",
            "=== Table ===",
            f"Top card: {top_card}",
            f"Current color: {current_color}",
            f"Draw pile: {draw_pile_size} cards",
            f"Opponent has: {len(opp_hand)} cards",
            "",
        ]

        if winner:
            lines.append("Status: YOU WIN!" if winner == self.player else "Status: OPPONENT WINS")
        elif current_player == self.player:
            lines.append("Status: YOUR TURN")
        else:
            lines.append("Status: OPPONENT'S TURN")

        return "\n".join(lines)

    def play(self, card_index: int, chosen_color: Optional[str] = None) -> str:
        """
        Play a card from this player's hand.

        Args:
            card_index:    1-based index into the player's hand.
            chosen_color:  Required when playing a Wild or Wild Draw Four.

        Returns:
            A message describing the outcome.
        """
        state = self._load_or_create()

        if state["winner"]:
            return f"Game is already over. Player {state['winner']} won!"

        if state["current_player"] != self.player:
            return f"ERROR: It is not your turn. Waiting for Player {state['current_player']}."

        my_hand = state["hands"][self.player]
        if card_index < 1 or card_index > len(my_hand):
            return (
                f"ERROR: Invalid card index {card_index}. "
                f"You have {len(my_hand)} cards (index 1-{len(my_hand)})."
            )

        card = my_hand[card_index - 1]
        top_card = state["discard"][-1]

        if not is_valid_play(card, top_card, state["current_color"]):
            return (
                f"ERROR: Cannot play '{card}' on '{top_card}' "
                f"(current color: {state['current_color']}). "
                "Card must match color, type, or be a Wild."
            )

        # Wild color must be specified
        if card in WILDS:
            if chosen_color not in COLORS:
                return (
                    "ERROR: You must specify a color when playing a Wild. "
                    f"Choose from: {', '.join(COLORS)}"
                )
            new_color = chosen_color
        else:
            new_color = card_color(card)

        # Remove card from hand and place on discard
        my_hand.pop(card_index - 1)
        state["discard"].append(card)
        state["current_color"] = new_color
        state["last_move"] = {
            "player": self.player,
            "action": "play",
            "card": card,
            "color": new_color,
        }

        result_parts = [f"You played: {card}"]
        if card in WILDS:
            result_parts.append(f"Color changed to: {new_color}")

        # Check win condition BEFORE applying effects
        if len(my_hand) == 0:
            state["winner"] = self.player
            state["hands"][self.player] = my_hand
            self.store.save(state)
            return "\n".join(result_parts) + "\nUNO OUT! You win!"

        # Apply card effects
        ctype = card_type(card)
        next_player = self.opponent  # default: turn passes to opponent

        if ctype == "Skip":
            next_player = self.player
            result_parts.append(f"Player {self.opponent} is skipped. Your turn again!")

        elif ctype == "Reverse":
            # In 2-player: Reverse acts like Skip
            next_player = self.player
            result_parts.append(f"Reversed (acts as Skip in 2-player). Your turn again!")

        elif ctype == "Draw Two":
            self._replenish_deck(state)
            drawn = [state["deck"].pop() for _ in range(min(2, len(state["deck"])))]
            state["hands"][self.opponent].extend(drawn)
            next_player = self.player
            result_parts.append(
                f"Player {self.opponent} draws 2 cards and loses their turn. Your turn again!"
            )

        elif ctype == "Wild Draw Four":
            self._replenish_deck(state)
            drawn = [state["deck"].pop() for _ in range(min(4, len(state["deck"])))]
            state["hands"][self.opponent].extend(drawn)
            next_player = self.player
            result_parts.append(
                f"Player {self.opponent} draws 4 cards and loses their turn. Your turn again!"
            )

        state["current_player"] = next_player
        state["hands"][self.player] = my_hand
        self.store.save(state)

        return "\n".join(result_parts)

    def draw(self) -> str:
        """Draw one card from the draw pile. Advances turn to opponent."""
        state = self._load_or_create()

        if state["winner"]:
            return f"Game is already over. Player {state['winner']} won!"

        if state["current_player"] != self.player:
            return f"ERROR: It is not your turn. Waiting for Player {state['current_player']}."

        self._replenish_deck(state)

        if not state["deck"]:
            return "ERROR: Draw pile is empty and discard cannot be reshuffled."

        card = state["deck"].pop()
        state["hands"][self.player].append(card)
        state["last_move"] = {
            "player": self.player,
            "action": "draw",
            "card": card,
        }

        state["current_player"] = self.opponent
        self.store.save(state)

        return f"You drew: {card}\nTurn passes to Player {self.opponent}."

    async def wait(self, timeout: float = 60) -> str:
        """
        Block until it is this player's turn (or the game ends).
        Returns immediately if it's already this player's turn.

        Polls every 0.5s for the file backend.
        For Redis, the same poll works well since saves happen quickly.
        (True pub/sub support is a future enhancement.)

        Returns:
            Opponent's last move description + current status.
        """
        deadline = time.time() + timeout

        while True:
            state = self.store.load()
            if state is None:
                state = self._load_or_create()

            if state["current_player"] == self.player or state["winner"]:
                last = state.get("last_move")
                if last is None:
                    move_str = "Game just started — you go first!"
                elif last["action"] == "play":
                    move_str = f"Opponent played: {last['card']}"
                    if last["card"] in WILDS:
                        move_str += f" (chose {last['color']})"
                else:
                    move_str = "Opponent drew a card."
                return f"{move_str}\n\n{self.status()}"

            if time.time() >= deadline:
                return "Timeout: still waiting for opponent's move."

            await asyncio.sleep(0.5)


# ════════════════════════════════════════════════════════════ #
#  MCP Server setup                                           #
# ════════════════════════════════════════════════════════════ #
server = Server("uno")

# Set in main() before the server starts
game: Optional[UnoGame] = None


@server.list_tools()
async def list_game_commands():
    return [
        Tool(
            name="Status",
            description=(
                "Display the current state of the UNO game from this player's perspective. "
                "Shows your hand, the top card, draw pile size, opponent card count, and whose turn it is."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="Play",
            description=(
                "Play a card from your hand. "
                "Provide the 1-based card_index of the card to play. "
                "For Wild cards also provide 'chosen_color' (Red, Yellow, Green, or Blue)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "card_index": {
                        "type": "integer",
                        "description": "1-based index of the card to play from your hand.",
                    },
                    "chosen_color": {
                        "type": "string",
                        "enum": ["Red", "Yellow", "Green", "Blue"],
                        "description": "Required color choice when playing a Wild or Wild Draw Four.",
                    },
                },
                "required": ["card_index"],
            },
        ),
        Tool(
            name="Draw",
            description=(
                "Draw one card from the draw pile into your hand. "
                "This ends your turn and passes play to the opponent."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="Wait",
            description=(
                "Block until it is your turn to play. "
                "Returns immediately if it is already your turn. "
                "Also returns the opponent's most recent move."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "number",
                        "description": "Maximum seconds to wait before timing out (default: 60).",
                    }
                },
                "required": [],
            },
        ),
    ]


@server.call_tool()
async def handle_command(tool_name: str, arguments: dict):
    global game
    args_dict = arguments or {}

    if game is None:
        result = "ERROR: Game not initialized."
    elif tool_name == "Status":
        result = game.status()
    elif tool_name == "Play":
        card_index = args_dict.get("card_index")
        if card_index is None:
            result = "ERROR: 'card_index' is required."
        else:
            chosen_color = args_dict.get("chosen_color")
            result = game.play(int(card_index), chosen_color)
    elif tool_name == "Draw":
        result = game.draw()
    elif tool_name == "Wait":
        timeout = float(args_dict.get("timeout", 60))
        result = await game.wait(timeout)
    else:
        result = f"ERROR: Unknown tool '{tool_name}'."

    return [TextContent(type="text", text=result)]


# ════════════════════════════════════════════════════════════ #
#  Entry point                                                #
# ════════════════════════════════════════════════════════════ #
def parse_args():
    parser = argparse.ArgumentParser(description="UNO MCP Server")
    parser.add_argument("--game", required=True, help="Unique game ID string")
    parser.add_argument(
        "--player",
        required=True,
        choices=["A", "B"],
        help="Which player this server represents (A or B)",
    )
    args, _ = parser.parse_known_args()
    return args


async def main():
    """Parse args, initialise game, then run the MCP stdio server."""
    global game
    cli = parse_args()
    store = StateStore(cli.game)
    game = UnoGame(game_id=cli.game, player=cli.player, store=store)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())