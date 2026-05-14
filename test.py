# #!/usr/bin/env python3
# """
# Test script for the UNO MCP server.

# To run this test:
# 1. Make sure you have the MCP server dependencies installed
# 2. Run the test script:
#    python test.py

# This will start the MCP server as 2 subprocesses and simulate a two person
# game. Both players should:
# - Play cards and draw from the deck
# - Play several rounds
# - Display the game state for each of them.
# """
# import asyncio


# async def test_uno():
#     pass


# async def main():
#     """Main test function."""
#     await test_uno()


# if __name__ == "__main__":
#     asyncio.run(main())


import asyncio
import json
import os
import sys
import uuid


class MCPProcess:
    """
    Wraps a single main.py subprocess and provides simple
    async send/receive over its stdin/stdout.
    """

    def __init__(self, game_id: str, player: str):
        self.game_id = game_id
        self.player = player
        self.proc = None
        self._msg_id = 0

    async def start(self):
        cmd = [sys.executable, "main.py", f"--game={self.game_id}", f"--player={self.player}"]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,  # suppress server-side warnings
        )

    async def _send(self, obj: dict) -> None:
        """Send one JSON-RPC message to the process stdin."""
        line = json.dumps(obj) + "\n"
        self.proc.stdin.write(line.encode())
        await self.proc.stdin.drain()

    async def _recv(self) -> dict:
        """Read one JSON-RPC message from the process stdout."""
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise EOFError(f"Player {self.player} process closed unexpectedly")
            text = raw.decode().strip()
            if text:
                return json.loads(text)

    async def initialize(self):
        """Perform the MCP handshake (initialize → initialized notification)."""
        self._msg_id += 1
        await self._send({
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        })
        resp = await self._recv()  # consume initialize response
        # Send the required 'initialized' notification (no response expected)
        await self._send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
        return resp

    async def call_tool(self, name: str, arguments: dict = None) -> str:
        """Call an MCP tool and return the text result."""
        self._msg_id += 1
        await self._send({
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        resp = await self._recv()
        if "error" in resp:
            return f"[MCP ERROR] {resp['error']}"
        content = resp.get("result", {}).get("content", [])
        return content[0]["text"] if content else ""

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            await self.proc.wait()




def parse_status(status_text: str) -> dict:
    """
    Parse the human-readable Status output into a dict with keys:
        hand         – list of card strings
        top_card     – string
        current_color – string
        draw_pile    – int
        opponent_cards – int
        my_turn      – bool
        winner       – "me" | "opponent" | None
    """
    info = {
        "hand": [],
        "top_card": None,
        "current_color": None,
        "draw_pile": 0,
        "opponent_cards": 0,
        "my_turn": False,
        "winner": None,
    }
    in_hand = False
    for line in status_text.splitlines():
        line = line.strip()
        if line == "=== Your Hand ===":
            in_hand = True
            continue
        if line.startswith("==="):
            in_hand = False
            continue
        if in_hand and line and line[0].isdigit():
            # "1. Red 5"
            card = line.split(". ", 1)[1] if ". " in line else line
            info["hand"].append(card)
        if line.startswith("Top card:"):
            info["top_card"] = line.split(": ", 1)[1]
        if line.startswith("Current color:"):
            info["current_color"] = line.split(": ", 1)[1]
        if line.startswith("Draw pile:"):
            info["draw_pile"] = int(line.split(": ", 1)[1].split()[0])
        if line.startswith("Opponent has:"):
            info["opponent_cards"] = int(line.split(": ", 1)[1].split()[0])
        if line.startswith("Status: YOUR TURN"):
            info["my_turn"] = True
        if "YOU WIN" in line:
            info["winner"] = "me"
        if "OPPONENT WINS" in line:
            info["winner"] = "opponent"
    return info


def choose_card(hand: list, top_card: str, current_color: str) -> tuple:
    """
    Simple AI: pick the first playable card.
    Returns (1-based index, chosen_color or None).

    Priority: match-color > match-type > wild.
    """
    COLORS = ["Red", "Yellow", "Green", "Blue"]
    WILDS = ["Wild", "Wild Draw Four"]

    def card_color(card):
        for c in COLORS:
            if card.startswith(c):
                return c
        return None

    def card_type(card):
        for c in COLORS:
            if card.startswith(c + " "):
                return card[len(c) + 1:]
        return card

    def is_valid(card):
        if card in WILDS:
            return True
        cc = card_color(card)
        if cc == current_color:
            return True
        if card_type(card) == card_type(top_card):
            return True
        return False

    # First pass: non-wild playable cards
    for i, card in enumerate(hand, 1):
        if card not in WILDS and is_valid(card):
            return i, None

    # Second pass: wilds (choose the most frequent color in hand)
    for i, card in enumerate(hand, 1):
        if card in WILDS:
            color_counts = {c: sum(1 for h in hand if h.startswith(c)) for c in COLORS}
            best_color = max(color_counts, key=color_counts.get)
            return i, best_color

    return None, None  # must draw




SEPARATOR = "─" * 60

def print_section(title: str):
    print(f"\n{SEPARATOR}")
    print(f"  {title}")
    print(SEPARATOR)


async def test_uno():
    game_id = f"autotest_{uuid.uuid4().hex[:8]}"
    state_file = f"/tmp/uno_game_{game_id}.json"

    print_section(f"Starting UNO automated test  |  game_id = {game_id}")
    print("Spawning Player A and Player B server processes...")

    proc_a = MCPProcess(game_id, "A")
    proc_b = MCPProcess(game_id, "B")

    await proc_a.start()
    await proc_b.start()

    # ── Handshake ─────────────────────────────────────────────── #
    await proc_a.initialize()
    await proc_b.initialize()
    print(" Both players initialized\n")

    # ── Display initial state ─────────────────────────────────── #
    print_section("Initial Game State")
    status_a = await proc_a.call_tool("Status")
    print(f"[Player A]\n{status_a}")
    status_b = await proc_b.call_tool("Status")
    print(f"\n[Player B]\n{status_b}")

    # ── Game loop ─────────────────────────────────────────────── #
    procs = {"A": proc_a, "B": proc_b}
    turn_order = ["A", "B"]
    current_idx = 0
    round_num = 0
    winner = None
    max_rounds = 300  # safety valve

    while round_num < max_rounds:
        current_player = turn_order[current_idx]
        proc = procs[current_player]

        # Always read fresh status to know current state
        status_text = await proc.call_tool("Status")
        info = parse_status(status_text)

        # If it's not this player's turn, advance to the other player
        if not info["my_turn"] and not info["winner"]:
            current_idx = 1 - current_idx
            continue

        # Check for winner
        if info["winner"]:
            winner = current_player if info["winner"] == "me" else ("B" if current_player == "A" else "A")
            break

        round_num += 1
        print_section(f"Round {round_num} — Player {current_player}'s turn")
        print(status_text)

        # Decide action
        idx, chosen_color = choose_card(info["hand"], info["top_card"], info["current_color"])

        if idx is not None:
            # Play the chosen card
            args = {"card_index": idx}
            if chosen_color:
                args["chosen_color"] = chosen_color
            result = await proc.call_tool("Play", args)
            print(f"\n>>> Player {current_player} plays card #{idx}: {result}")
        else:
            # Must draw
            result = await proc.call_tool("Draw")
            print(f"\n>>> Player {current_player} draws: {result}")

        # Check for win in the play result
        if "UNO OUT" in result or "You win" in result:
            winner = current_player
            break

        # Advance turn (server handles skip/reverse/draw effects,
        # so we re-check status next iteration to see whose turn it truly is)
        current_idx = 1 - current_idx

    # ── Final state ───────────────────────────────────────────── #
    print_section("Final Game State")
    final_a = await proc_a.call_tool("Status")
    final_b = await proc_b.call_tool("Status")
    print(f"[Player A]\n{final_a}")
    print(f"\n[Player B]\n{final_b}")

    # ── Assertions ────────────────────────────────────────────── #
    print_section("Test Assertions")

    info_a = parse_status(final_a)
    info_b = parse_status(final_b)

    assert winner is not None, "FAIL: Game ended without a winner"
    print(f" Game completed — winner is Player {winner}")

    assert round_num < max_rounds, "FAIL: Game hit the round limit (possible infinite loop)"
    print(f" Completed in {round_num} rounds (well under the {max_rounds} limit)")

    if winner == "A":
        assert len(info_a["hand"]) == 0, f"FAIL: Winner A still has {len(info_a['hand'])} cards"
        assert info_a["winner"] == "me", "FAIL: A's status doesn't show a win"
        assert info_b["winner"] == "opponent", "FAIL: B's status doesn't show a loss"
    else:
        assert len(info_b["hand"]) == 0, f"FAIL: Winner B still has {len(info_b['hand'])} cards"
        assert info_b["winner"] == "me", "FAIL: B's status doesn't show a win"
        assert info_a["winner"] == "opponent", "FAIL: A's status doesn't show a loss"
    print(f" Winner Player {winner} has 0 cards in hand")
    print(" Both players' Status agrees on the outcome")
    print(f"\n{'═'*60}")
    print("  ALL TESTS PASSED ")
    print(f"{'═'*60}\n")

    # Cleanup
    await proc_a.stop()
    await proc_b.stop()
    if os.path.exists(state_file):
        os.remove(state_file)


async def main():
    """Main test entry point."""
    try:
        await test_uno()
    except AssertionError as e:
        print(f"\n TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
