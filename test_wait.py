#!/usr/bin/env python3
"""
test_wait.py — Part 3: Wait-tool based automated test.

Same as test.py but both players use the Wait tool to block
until it's their turn, instead of polling Status manually.

Run with:
    bash run_wait_test.sh
or:
    python test_wait.py
"""

import asyncio
import json
import os
import sys
import uuid

# ──────────────────────────────────────────────────────────── #
#  MCPProcess (same helper as test.py)                         #
# ──────────────────────────────────────────────────────────── #

class MCPProcess:
    def __init__(self, game_id: str, player: str):
        self.game_id = game_id
        self.player = player
        self.proc = None
        self._msg_id = 0

    async def start(self):
        cmd = [sys.executable, "main.py",
               f"--game={self.game_id}", f"--player={self.player}"]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _send(self, obj: dict):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _recv(self) -> dict:
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise EOFError(f"Player {self.player} process closed unexpectedly")
            text = raw.decode().strip()
            if text:
                return json.loads(text)

    async def initialize(self):
        self._msg_id += 1
        await self._send({
            "jsonrpc": "2.0", "id": self._msg_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "wait-test", "version": "1"},
            },
        })
        await self._recv()  # consume initialize response
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    async def call_tool(self, name: str, arguments: dict = None) -> str:
        self._msg_id += 1
        await self._send({
            "jsonrpc": "2.0", "id": self._msg_id,
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


# ──────────────────────────────────────────────────────────── #
#  Helpers                                                     #
# ──────────────────────────────────────────────────────────── #

COLORS = ["Red", "Yellow", "Green", "Blue"]
WILDS  = ["Wild", "Wild Draw Four"]

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

def is_valid(card, current_color, top_card):
    if card in WILDS:
        return True
    if card_color(card) == current_color:
        return True
    if card_type(card) == card_type(top_card):
        return True
    return False

def choose_card(hand, top_card, current_color):
    for i, card in enumerate(hand, 1):
        if card not in WILDS and is_valid(card, current_color, top_card):
            return i, None
    for i, card in enumerate(hand, 1):
        if card in WILDS:
            counts = {c: sum(1 for h in hand if h.startswith(c)) for c in COLORS}
            return i, max(counts, key=counts.get)
    return None, None

def parse_status(text):
    info = {"hand": [], "top_card": None, "current_color": None,
            "my_turn": False, "winner": None}
    in_hand = False
    for line in text.splitlines():
        line = line.strip()
        if line == "=== Your Hand ===":
            in_hand = True; continue
        if line.startswith("==="):
            in_hand = False; continue
        if in_hand and line and line[0].isdigit():
            info["hand"].append(line.split(". ", 1)[1] if ". " in line else line)
        if line.startswith("Top card:"):
            info["top_card"] = line.split(": ", 1)[1]
        if line.startswith("Current color:"):
            info["current_color"] = line.split(": ", 1)[1]
        if "YOUR TURN" in line:
            info["my_turn"] = True
        if "YOU WIN" in line:
            info["winner"] = "me"
        if "OPPONENT WINS" in line:
            info["winner"] = "opponent"
    return info

SEP = "─" * 60

def section(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")


# ──────────────────────────────────────────────────────────── #
#  Per-player coroutine — each player runs independently       #
# ──────────────────────────────────────────────────────────── #

async def play_as(proc: MCPProcess, result_box: dict, round_counts: dict):
    """
    Coroutine for one player.  Uses Wait to block until it's their turn,
    then picks the best card (or draws) and repeats until the game ends.
    """
    player = proc.player
    round_counts[player] = 0

    while True:
        # ── Wait for our turn (may return immediately if already ours) ──
        wait_result = await proc.call_tool("Wait", {"timeout": 120})

        # Parse the embedded status from the Wait response
        info = parse_status(wait_result)

        if info["winner"]:
            # Game is over from this player's perspective
            result_box[player] = info["winner"]  # "me" or "opponent"
            return

        # ── It's our turn — pick an action ──
        round_counts[player] += 1
        rn = round_counts[player]

        section(f"[Player {player}] Round {rn} — after Wait")
        # Print just the last-move line (first line of wait_result) + status
        print(wait_result)

        idx, chosen_color = choose_card(info["hand"], info["top_card"], info["current_color"])

        if idx is not None:
            args = {"card_index": idx}
            if chosen_color:
                args["chosen_color"] = chosen_color
            result = await proc.call_tool("Play", args)
            print(f"\n>>> Player {player} plays card #{idx}: {result}")
        else:
            result = await proc.call_tool("Draw")
            print(f"\n>>> Player {player} draws: {result}")

        if "UNO OUT" in result or "You win" in result:
            result_box[player] = "me"
            return


# ──────────────────────────────────────────────────────────── #
#  Main                                                        #
# ──────────────────────────────────────────────────────────── #

async def test_wait():
    game_id   = f"waitest_{uuid.uuid4().hex[:8]}"
    state_file = f"/tmp/uno_game_{game_id}.json"

    section(f"UNO Wait-Tool Test  |  game_id = {game_id}")
    print("Spawning Player A and Player B processes...")

    proc_a = MCPProcess(game_id, "A")
    proc_b = MCPProcess(game_id, "B")

    await proc_a.start()
    await proc_b.start()
    await proc_a.initialize()
    await proc_b.initialize()
    print("Both players initialized and connected.")

    # Show initial state via Player A's Status (creates the game)
    section("Initial Game State (Player A view)")
    init_status = await proc_a.call_tool("Status")
    print(init_status)

    # Run both players concurrently — each blocks on Wait internally
    result_box   = {}
    round_counts = {}
    await asyncio.gather(
        play_as(proc_a, result_box, round_counts),
        play_as(proc_b, result_box, round_counts),
    )

    # ── Final state ──
    section("Final Game State")
    final_a = await proc_a.call_tool("Status")
    final_b = await proc_b.call_tool("Status")
    print(f"[Player A]\n{final_a}")
    print(f"\n[Player B]\n{final_b}")

    # ── Assertions ──
    section("Test Assertions")

    # Determine winner from result_box
    winner = None
    for p, outcome in result_box.items():
        if outcome == "me":
            winner = p

    assert winner is not None, "FAIL: No winner determined"
    print(f"Winner: Player {winner}")

    total_rounds = sum(round_counts.values())
    print(f"Total turns played: {total_rounds} (A={round_counts.get('A',0)}, B={round_counts.get('B',0)})")
    assert total_rounds < 600, "FAIL: Too many turns — possible infinite loop"
    print(f"Completed under turn limit")

    info_a = parse_status(final_a)
    info_b = parse_status(final_b)

    if winner == "A":
        assert len(info_a["hand"]) == 0, f"FAIL: Winner A still has cards"
        assert info_a["winner"] == "me"
        assert info_b["winner"] == "opponent"
    else:
        assert len(info_b["hand"]) == 0, f"FAIL: Winner B still has cards"
        assert info_b["winner"] == "me"
        assert info_a["winner"] == "opponent"
    print(f"Winner Player {winner} has 0 cards in hand")
    print("Both players' Status agrees on the outcome")

    print(f"\n{'='*60}")
    print("  ALL WAIT-TOOL TESTS PASSED")
    print(f"{'='*60}\n")

    await proc_a.stop()
    await proc_b.stop()
    if os.path.exists(state_file):
        os.remove(state_file)


async def main():
    try:
        await test_wait()
    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
