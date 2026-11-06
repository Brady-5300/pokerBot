"""Connects the strategy to Open Poker (https://docs.openpoker.ai).

Usage:
    Put your key in ~/.openpoker_key, or set OPEN_POKER_API_KEY.
    python openpoker_client.py --hands 50

Session is pure message handling, so it can be tested without a network.
run() owns the socket, reconnects, and delayed sends.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import websockets

import evaluator as ev
from ranges import Act
from strategy import View, action_order, safe_decide

URL = "wss://openpoker.ai/ws"
# Lives in the home folder so the key stays out of this OneDrive-synced project.
KEY_FILE = Path.home() / ".openpoker_key"
BUY_IN = 5000
MIN_BUY_IN = 1000
BUST_REJOIN_DELAY = 5
REBUY_RETRY_DELAY = 60
MAX_BACKOFF = 30
IDLE_SECONDS = 300
# Equity pricing is off until it clears the full simulator test. Use 2000 to turn it on.
SAMPLES = 0
# table_state makes up most of the traffic and nothing replays it, so don't log it.
UNLOGGED_TYPES = {"table_state"}

log = logging.getLogger("openpoker")


def added_chips(msg):
    """Chips a player_action put in. All-ins report 0 added, so use the whole stack."""
    added = msg.get("contribution_delta") or 0
    if msg.get("action") == "all_in" and not added:
        added = msg.get("player_stack_before") or msg.get("stack_before") or 0
    return added


@dataclass
class Hand:
    hand_id: str = ""
    hole: list = field(default_factory=list)
    dealer_seat: int = 0
    big_blind: int = 20
    folded: set = field(default_factory=set)
    raises: int = 0
    i_was_last_raiser: bool = False
    my_bet: int = 0
    street_cards: int = 0
    bet_streets: set = field(default_factory=set)
    history: list = field(default_factory=list)


@dataclass
class Later:
    """A message to send after a delay. A newer Later replaces a pending one."""

    delay: float
    message: dict


class Session:
    def __init__(self, rng=None, buy_in=BUY_IN):
        self.rng = rng or random.Random()
        self.target_buy_in = buy_in
        self.buy_in = buy_in
        self.rebuying = False
        self.seat = None
        self.table_id = None
        self.last_table_seq = 0
        self.hand = Hand()
        self.pending_turn = None
        self.hands_played = 0
        self.last_stack = None
        self.auto_rebuy_sent = False
        self.decision_ms_max = 0.0

    def handle(self, msg):
        """Process one server message and return the messages to send."""
        if "table_seq" in msg:
            self.last_table_seq = max(self.last_table_seq, msg["table_seq"] or 0)
        if msg.get("table_id"):
            self.table_id = msg["table_id"]

        handler = getattr(self, "on_" + str(msg.get("type")), None)
        if handler is None:
            log.debug("ignored message type %s", msg.get("type"))
            return []
        return handler(msg) or []

    def on_connected(self, msg):
        log.info("connected as %s", msg.get("name"))
        if self.table_id:
            return [self.resync()]
        return [self.join_lobby()]

    def on_lobby_joined(self, msg):
        log.info("in lobby at position %s", msg.get("position"))
        if not self.auto_rebuy_sent:
            self.auto_rebuy_sent = True
            return [{"type": "set_auto_rebuy", "enabled": True}]
        return []

    def on_table_joined(self, msg):
        self.seat = msg.get("seat")
        self.buy_in = self.target_buy_in
        log.info("seated at table %s, seat %s", msg.get("table_id"), self.seat)

    def on_hand_start(self, msg):
        hand_id = msg.get("hand_id", "")
        if hand_id != self.hand.hand_id:
            self.hand = Hand(hand_id=hand_id)
        if msg.get("seat") is not None:
            self.seat = msg["seat"]
        self.hand.dealer_seat = msg.get("dealer_seat", 0)
        blinds = msg.get("blinds") or {}
        self.hand.big_blind = blinds.get("big_blind", self.hand.big_blind)

    def on_hole_cards(self, msg):
        self.hand.hole = [ev.parse_card(card) for card in msg["cards"]]

    def on_community_cards(self, msg):
        self.hand.raises = 0
        self.hand.my_bet = 0
        self.hand.street_cards = len(msg.get("cards") or [])

    def on_player_action(self, msg):
        self.track_action(msg)

    def track_action(self, msg):
        hand = self.hand
        action = msg.get("action")
        seat = msg.get("seat")
        if action == "fold":
            hand.folded.add(seat)
        if seat == self.seat:
            hand.my_bet += added_chips(msg)
        hand.history.append(self.to_act(msg))
        amount = msg.get("amount") or added_chips(msg)
        if action in ("raise", "all_in") and amount > hand.big_blind:
            hand.raises += 1
            hand.i_was_last_raiser = seat == self.seat
            if hand.street_cards and seat != self.seat:
                hand.bet_streets.add(hand.street_cards)

    def to_act(self, msg):
        action = msg.get("action")
        added = added_chips(msg)
        facing = msg.get("to_call_before") or 0
        pot = (msg.get("pot_before") or 0) + facing
        size = (added - facing) / pot if pot else 0.0
        kind = action
        if action in ("raise", "all_in"):
            kind = "call" if action == "all_in" and added <= facing else "raise" if facing else "bet"
        return Act(msg.get("seat"), self.hand.street_cards, kind, max(size, 0.0))

    def on_player_left(self, msg):
        self.hand.folded.add(msg.get("seat"))

    def on_your_turn(self, msg):
        return [self.act(msg)]

    def posted_blind(self, seats):
        """Blinds don't appear as player_action, so work out what we posted."""
        hand = self.hand
        order = action_order(seats, hand.dealer_seat, preflop=False)
        if len(order) < 2:
            return 0
        small, big = (order[1], order[0]) if len(order) == 2 else (order[0], order[1])
        if self.seat == small:
            return hand.big_blind // 2
        return hand.big_blind if self.seat == big else 0

    @staticmethod
    def raises_faced(hand, board, to_call):
        """Counted raises, but never 0 when the price shows someone raised."""
        if board:
            return max(hand.raises, 1 if to_call > 0 else 0)
        return max(hand.raises, 1 if to_call > hand.big_blind else 0)

    def build_view(self, msg):
        hand = self.hand
        offered = {entry["action"]: entry for entry in msg["valid_actions"]}
        players = msg.get("players") or []
        stacks = {player["seat"]: player.get("stack", 0) for player in players}
        live = [seat for seat in stacks if seat not in hand.folded]
        board = [ev.parse_card(card) for card in msg.get("community_cards") or []]

        order = action_order(stacks, hand.dealer_seat, preflop=not board)
        live_order = [seat for seat in order if seat in live]
        behind = 0
        if self.seat in live_order:
            behind = len(live_order) - 1 - live_order.index(self.seat)

        to_call = (offered.get("call") or {}).get("amount", 0)
        if "call" not in offered and "check" not in offered and "all_in" in offered:
            to_call = offered["all_in"].get("amount", 0)
        opponent_stacks = [stacks[seat] for seat in live if seat != self.seat]
        raise_entry = offered.get("raise") or {}
        stack = stacks.get(self.seat) or raise_entry.get("max", 0) or to_call

        return View(
            hole=hand.hole,
            board=board,
            pot=msg.get("pot", 0),
            to_call=to_call,
            stack=stack,
            big_blind=hand.big_blind,
            min_raise=raise_entry.get("min", 0),
            max_raise=raise_entry.get("max", 0),
            my_bet=hand.my_bet + (0 if board else self.posted_blind(stacks)),
            opponents=max(len(opponent_stacks), 1),
            players_behind=behind,
            raises_this_street=self.raises_faced(hand, board, to_call),
            effective_stack=min(stack, max(opponent_stacks, default=stack)) or stack,
            was_aggressor=hand.i_was_last_raiser,
            opponent_bet_streets=len(hand.bet_streets),
            history=tuple(hand.history),
            opponent_seats=tuple(seat for seat in live if seat != self.seat),
            samples=SAMPLES,
            rng=self.rng,
        )

    def act(self, turn):
        self.pending_turn = turn
        started = time.perf_counter()
        try:
            view = self.build_view(turn)
            action, amount = safe_decide(view, turn["valid_actions"])
        except Exception:
            log.exception("could not build a view; playing check/fold")
            action, amount = self.fallback(turn)
        elapsed = (time.perf_counter() - started) * 1000
        self.decision_ms_max = max(self.decision_ms_max, elapsed)
        log.debug("decided %s %s in %.3f ms", action, amount, elapsed)
        return self.action_message(turn, action, amount)

    @staticmethod
    def fallback(turn):
        offered = {entry.get("action") for entry in turn.get("valid_actions", [])}
        return ("check", None) if "check" in offered else ("fold", None)

    @staticmethod
    def action_message(turn, action, amount):
        message = {
            "type": "action",
            "hand_id": turn["hand_id"],
            "action": action,
            "client_action_id": uuid.uuid4().hex,
            "turn_token": turn["turn_token"],
        }
        if action == "raise":
            message["amount"] = amount
        return message

    def on_action_rejected(self, msg):
        code = msg.get("code")
        log.warning("action rejected: %s (%s)", code, msg.get("reason"))
        turn, self.pending_turn = self.pending_turn, None
        # Only one retry, so a bad action can't trigger the flood kick.
        if code == "invalid_action" and turn is not None:
            action, amount = self.fallback(turn)
            return [self.action_message(turn, action, amount)]
        return []

    def on_hand_result(self, msg):
        self.pending_turn = None
        if not self.hand.hole:
            return  # A hand already running when we sat down.
        self.hands_played += 1
        stacks = msg.get("final_stacks") or {}
        stack = stacks.get(str(self.seat))
        change = None if stack is None or self.last_stack is None else stack - self.last_stack
        log.info("hand %d done: stack %s (%s)", self.hands_played, stack, change)
        if stack is not None:
            self.last_stack = stack

    def on_busted(self, msg):
        log.info("busted")
        self.clear_table()
        return [Later(BUST_REJOIN_DELAY, self.join_lobby())]

    def on_auto_rebuy_scheduled(self, msg):
        cooldown = msg.get("cooldown_seconds") or 0
        log.info("auto-rebuy in %s seconds", cooldown)
        return [Later(cooldown + 1, self.join_lobby())]

    def on_rebuy_confirmed(self, msg):
        log.info("rebuy confirmed, balance %s", msg.get("chip_balance"))
        self.rebuying = False
        return [self.join_lobby()]

    def on_table_closed(self, msg):
        log.info("table closed: %s", msg.get("reason"))
        self.clear_table()
        return [self.join_lobby()]

    def on_season_ended(self, msg):
        log.info("season %s ended", msg.get("season_number"))
        self.clear_table()
        # The auto-rebuy preference is saved per season, so set it again.
        self.auto_rebuy_sent = False
        return [self.join_lobby()]

    def on_resync_response(self, msg):
        for event in msg.get("replayed_events") or []:
            payload = event.get("payload", event) if isinstance(event, dict) else {}
            if payload.get("type") == "your_turn":
                continue  # Only the snapshot can tell us whether we still act.
            handler = getattr(self, "on_" + str(payload.get("type")), None)
            if handler is not None and payload.get("type") in REPLAYABLE:
                handler(payload)

        snapshot = msg.get("snapshot") or {}
        hero = snapshot.get("hero") or {}
        if hero.get("hole_cards"):
            self.hand.hole = [ev.parse_card(card) for card in hero["hole_cards"]]
        if hero.get("seat") is not None:
            self.seat = hero["seat"]
        if hero.get("valid_actions") and hero.get("turn_token"):
            turn = {
                "hand_id": snapshot.get("hand_id") or msg.get("hand_id") or self.hand.hand_id,
                "turn_token": hero["turn_token"],
                "valid_actions": hero["valid_actions"],
                "pot": snapshot.get("pot", 0),
                "community_cards": snapshot.get("board") or [],
                "players": snapshot.get("seats") or [],
            }
            self.hand.folded |= {
                seat["seat"] for seat in turn["players"] if seat.get("folded")
            }
            return [self.act(turn)]
        return []

    def on_error(self, msg):
        code = msg.get("code")
        log.warning("server error: %s (%s)", code, msg.get("message"))
        if code == "insufficient_funds" and self.buy_in > MIN_BUY_IN:
            # A rebuy needs a balance under 1,000, so step down to use what we have.
            self.buy_in = max(self.buy_in // 2, MIN_BUY_IN)
            return [self.join_lobby()]
        if code in ("insufficient_season_chips", "insufficient_funds"):
            self.rebuying = True
            return [{"type": "rebuy", "amount": 0}]
        if code == "already_seated":
            self.table_id = msg.get("table_id", self.table_id)
            self.seat = msg.get("seat", self.seat)
            return [self.resync()]
        if code in ("table_not_found", "not_at_table"):
            self.clear_table()
            return [Later(BUST_REJOIN_DELAY, self.join_lobby())]
        if self.rebuying or code in ("rate_limited", "not_registered_for_season"):
            # The docs list no cooldown error code, so retry on any failed rebuy.
            self.rebuying = False
            return [Later(REBUY_RETRY_DELAY, self.join_lobby())]
        return []

    def clear_table(self):
        self.table_id = None
        self.last_table_seq = 0
        self.hand = Hand()
        self.pending_turn = None
        self.last_stack = None

    def join_lobby(self):
        return {"type": "join_lobby", "buy_in": self.buy_in}

    def resync(self):
        return {
            "type": "resync_request",
            "table_id": self.table_id,
            "last_table_seq": self.last_table_seq,
        }


REPLAYABLE = {"hand_start", "hole_cards", "player_action", "community_cards"}


async def run(api_key, url=URL, max_hands=None, log_path=None, buy_in=BUY_IN):
    session = Session(buy_in=buy_in)
    backoff = 1
    timer = None
    raw_log = open(log_path, "a", encoding="utf-8") if log_path else None

    async def send_later(socket, later):
        await asyncio.sleep(later.delay)
        await socket.send(json.dumps(later.message))

    try:
        while True:
            try:
                headers = {"Authorization": f"Bearer {api_key}"}
                async with websockets.connect(url, additional_headers=headers) as socket:
                    backoff = 1
                    while True:
                        try:
                            raw = await asyncio.wait_for(socket.recv(), IDLE_SECONDS)
                        except TimeoutError:
                            # Nudge the lobby in case a rebuy or rejoin was missed.
                            await socket.send(json.dumps(session.join_lobby()))
                            continue
                        msg = json.loads(raw)
                        if raw_log and msg.get("type") not in UNLOGGED_TYPES:
                            raw_log.write(raw + "\n")
                            raw_log.flush()
                        try:
                            outgoing = session.handle(msg)
                        except Exception:
                            log.exception("failed on %s message", msg.get("type"))
                            outgoing = []
                        for out in outgoing:
                            if isinstance(out, Later):
                                if timer:
                                    timer.cancel()
                                timer = asyncio.create_task(send_later(socket, out))
                            else:
                                await socket.send(json.dumps(out))
                        if max_hands and session.hands_played >= max_hands:
                            await socket.send(json.dumps({"type": "leave_table"}))
                            log.info("played %d hands; slowest decision %.3f ms",
                                     session.hands_played, session.decision_ms_max)
                            return session
            except websockets.ConnectionClosed as closed:
                if closed.rcvd and closed.rcvd.code == 4001:
                    raise SystemExit("Open Poker rejected the API key (close code 4001).")
                log.warning("connection closed: %s", closed)
            except (OSError, websockets.InvalidHandshake) as error:
                log.warning("connection failed: %s", error)
            if timer:
                timer.cancel()
                timer = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        if raw_log:
            raw_log.close()


def load_api_key(key_file=KEY_FILE):
    key = os.environ.get("OPEN_POKER_API_KEY")
    if key:
        return key.strip()
    if key_file.exists():
        # utf-8-sig drops the byte-order mark some Windows editors add.
        return key_file.read_text(encoding="utf-8-sig").strip()
    return None


def main():
    parser = argparse.ArgumentParser(description="Play on Open Poker.")
    parser.add_argument("--hands", type=int, help="leave the table after this many hands")
    parser.add_argument("--buy-in", type=int, default=BUY_IN, help="chips to sit down with")
    parser.add_argument("--url", default=URL)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)

    api_key = load_api_key()
    if not api_key:
        raise SystemExit(f"Set OPEN_POKER_API_KEY or put your key in {KEY_FILE}.")

    logs = Path(__file__).parent / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log.info("raw messages go to %s", log_path)
    try:
        asyncio.run(run(api_key, args.url, args.hands, log_path, args.buy_in))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
