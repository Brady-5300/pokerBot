"""Poker decision logic, independent of any platform.

decide() takes a View and returns (action, raise_to). Actions are "fold",
"check", "call", "raise" and "jam". Adapters map these onto a platform's legal
moves with legalize(), so this file can be reused on any table format.
With samples set on the View, postflop spots are priced by ranges.py. With
samples at 0 they use fast rule-based lookups, for tight per-decision limits.
"""

import logging
import random
from dataclasses import dataclass, field

import evaluator as ev
import ranges
from preflop_table import PERCENTILE
from ranges import draw_outs, hand_key

log = logging.getLogger("strategy")

# Top fraction of hands to open, keyed by players left to act behind us.
# Logged players fold 71% to an open, call 24% and 3-bet only 5%, then rarely
# bet the flop, so we open 90% of hands from any seat (+43-64 bb/100 in sim.py).
OPEN_RANGE = {5: 0.90, 4: 0.90, 3: 0.90, 2: 0.90, 1: 0.90, 0: 0.90}
SHOVE_RANGE = {5: 0.11, 4: 0.14, 3: 0.19, 2: 0.29, 1: 0.40, 0: 0.40}
SHORT_STACK_BB = 12
THREE_BET_RANGE = 0.035
FOUR_BET_RANGE = 0.015
RAISE_JAM_BB = 30
# Top fraction of hands to call an open with: closing the action cheaply, cheap price, dear price.
CLOSE_CALL_RANGE = 0.70
CALL_RANGE = 0.45
DEAR_CALL_RANGE = 0.06
# Top fraction of hands to call a 3-bet or bigger raise with. Opening 90% means
# folding to 3-bets too often, so this runs wide (+3 to +23 bb/100 in sim.py).
RERAISE_CALL_RANGE = 0.15
OPEN_SIZE_BB = 2.5

# The pre-Nov 7 ranges, used while a player who 3-bets a lot is seated (see View.tight).
TIGHT_OPEN_RANGE = {5: 0.22, 4: 0.28, 3: 0.40, 2: 0.60, 1: 0.54, 0: 0.54}
TIGHT_CLOSE_CALL_RANGE = 0.30
TIGHT_CALL_RANGE = 0.11
TIGHT_RERAISE_CALL_RANGE = 0.03

# Rough equity by made-hand tier; see postflop_tier().
AIR, WEAK, MEDIUM, GOOD, STRONG, MONSTER = range(6)
TIER_EQUITY = {AIR: 0.08, WEAK: 0.20, MEDIUM: 0.40, GOOD: 0.58, STRONG: 0.75, MONSTER: 0.92}
# Betting street after street is rarely a bluff, so each extra street cuts our equity.
BARREL_DISCOUNT = 0.60
# Pot fractions. Logged opponents barely fold more to bigger bets, so value bets run large.
GOOD_VALUE_BET = 0.70
STRONG_VALUE_BET = 0.90
# Heads-up flop bet as the preflop raiser, with any hand. Logged opponents fold
# to about 45% of small heads-up flop bets, more than a third-pot bet needs.
CBET_SIZE = 0.33


@dataclass
class View:
    hole: list
    board: list
    pot: int
    to_call: int
    stack: int
    big_blind: int
    min_raise: int = 0
    max_raise: int = 0
    my_bet: int = 0
    opponents: int = 1
    players_behind: int = 0
    raises_this_street: int = 0
    effective_stack: int = 0
    was_aggressor: bool = False
    opponent_bet_streets: int = 0  # Postflop streets, including this one, an opponent bet.
    history: tuple = ()  # Every ranges.Act so far this hand.
    opponent_seats: tuple = ()
    tight: bool = False  # Play the tight preflop ranges.
    samples: int = 0
    rng: random.Random = field(default_factory=random.Random)


def action_order(seats, dealer_seat, preflop):
    """Seats in the order they act this street, given all seats dealt in."""
    ordered = sorted(seats)
    after = [seat for seat in ordered if seat > dealer_seat]
    before = [seat for seat in ordered if seat <= dealer_seat]
    order = after + before  # Small blind first, dealer last.
    if preflop:
        # Heads-up the dealer posts the small blind and acts first preflop.
        skip = 1 if len(order) == 2 else 2
        order = order[skip:] + order[:skip]
    return order


def effective_bb(view):
    stack = min(view.stack, view.effective_stack or view.stack)
    return stack / view.big_blind


def decide(view):
    if not view.board:
        return decide_preflop(view)
    return decide_postflop(view)


def check_or_fold(view):
    return ("check", 0) if view.to_call == 0 else ("fold", 0)


def decide_preflop(view):
    percentile = PERCENTILE[hand_key(view.hole)]
    behind = min(view.players_behind, 5)
    depth = effective_bb(view)

    if view.raises_this_street == 0:
        return unopened_preflop(view, percentile, behind, depth)

    is_reraised = view.raises_this_street >= 2
    premium = FOUR_BET_RANGE if is_reraised else THREE_BET_RANGE
    if percentile <= premium:
        if depth <= RAISE_JAM_BB or is_reraised:
            return ("jam", 0)
        return ("raise", 3 * (view.to_call + view.my_bet))

    price = view.to_call / max(view.stack, 1)
    # A big shove is strong whatever our own stack, so price it by its size too.
    if price >= 0.33 or view.to_call >= RAISE_JAM_BB * view.big_blind:
        limit = 0.04
    elif is_reraised:
        limit = TIGHT_RERAISE_CALL_RANGE if view.tight else RERAISE_CALL_RANGE
    elif view.players_behind == 0 and view.to_call <= 2.5 * view.big_blind:
        limit = TIGHT_CLOSE_CALL_RANGE if view.tight else CLOSE_CALL_RANGE
    elif price <= 0.08:
        limit = TIGHT_CALL_RANGE if view.tight else CALL_RANGE
    else:
        limit = DEAR_CALL_RANGE
    if percentile <= limit:
        return ("call", 0)
    return check_or_fold(view)


def unopened_preflop(view, percentile, behind, depth):
    if depth <= SHORT_STACK_BB:
        # Widen as the stack shrinks: at 6 BB the shove range is 1.5x its base.
        limit = SHOVE_RANGE[behind] * (1 + (SHORT_STACK_BB - depth) / SHORT_STACK_BB)
        return ("jam", 0) if percentile <= limit else cheap_look(view, percentile)

    opens = TIGHT_OPEN_RANGE if view.tight else OPEN_RANGE
    if percentile <= opens[behind]:
        limpers = max(0, round(view.pot / view.big_blind - 1.5))
        return ("raise", int(view.big_blind * (OPEN_SIZE_BB + limpers)))
    return cheap_look(view, percentile)


def cheap_look(view, percentile):
    if view.to_call == 0:
        return ("check", 0)
    if view.to_call * 2 <= view.big_blind and percentile <= 0.45:
        return ("call", 0)
    return ("fold", 0)


def postflop_tier(hole, board):
    """Bucket a hand by how it uses the board, not by its absolute rank."""
    score = ev.evaluate(hole + board)
    cat = ev.category(score)
    board_ranks = sorted((card >> 2 for card in board), reverse=True)
    hole_ranks = sorted((card >> 2 for card in hole), reverse=True)
    board_paired = len(set(board_ranks)) < len(board_ranks)
    pocket_pair = hole_ranks[0] == hole_ranks[1]
    top, bottom = board_ranks[0], board_ranks[-1]

    if len(board) >= 5 and ev.evaluate(board) == score:
        return WEAK  # Playing the board: at best a chop.
    if cat <= ev.TRIPS and not pocket_pair and not set(hole_ranks) & set(board_ranks):
        # Any pair or trips here is on the board, so everyone has it.
        return WEAK if hole_ranks[0] == 12 else AIR

    if cat >= ev.STRAIGHT:
        tier = MONSTER
    elif cat == ev.TRIPS:
        tier = MONSTER if pocket_pair else STRONG
    elif cat == ev.TWO_PAIR:
        if board_paired and not (set(hole_ranks) <= set(board_ranks)):
            tier = GOOD if hole_ranks[0] >= top or pocket_pair else MEDIUM
        else:
            tier = STRONG
    elif cat == ev.PAIR:
        if pocket_pair:
            if hole_ranks[0] > top:
                tier = GOOD
            elif hole_ranks[0] > bottom:
                tier = MEDIUM
            else:
                tier = WEAK
        else:
            paired = next(rank for rank in hole_ranks if rank in board_ranks)
            kicker = hole_ranks[0] if paired != hole_ranks[0] else hole_ranks[1]
            if paired == top:
                tier = GOOD if kicker >= 8 else MEDIUM
            elif paired == bottom:
                tier = WEAK
            else:
                tier = MEDIUM

    # Scary boards cap one-pair and two-pair hands.
    if cat < ev.STRAIGHT and tier >= MEDIUM and board_is_scary(board):
        tier -= 1
    return tier


def board_is_scary(board):
    suits = [0, 0, 0, 0]
    rank_mask = 0
    for card in board:
        suits[card & 3] += 1
        rank_mask |= 1 << (card >> 2)
    if max(suits) >= 4 or (len(board) == 3 and max(suits) == 3):
        return True
    return len(board) >= 4 and ev.straight_outs(rank_mask) >= 1


def draw_equity(hole, board):
    """Approximate equity from draws, using the rule of 2 and 4."""
    return draw_outs(hole, board) * (0.04 if len(board) == 3 else 0.02)


def pot_raise_to(view, fraction):
    """Raise-to total for a bet of `fraction` of the pot after calling."""
    return int(view.my_bet + view.to_call + fraction * (view.pot + view.to_call))


def decide_postflop(view):
    if view.samples and view.opponent_seats:
        try:
            return ranges.best_option(view)
        except Exception:
            log.exception("range pricing failed; using the rule-based decision")
    tier = postflop_tier(view.hole, view.board)
    draw = draw_equity(view.hole, view.board)
    if view.to_call == 0:
        return unbet_postflop(view, tier, draw)
    return facing_bet_postflop(view, tier, draw)


def unbet_postflop(view, tier, draw):
    in_position = view.players_behind == 0
    heads_up = view.opponents == 1

    if tier >= GOOD:
        return ("raise", pot_raise_to(view, STRONG_VALUE_BET if tier >= STRONG else GOOD_VALUE_BET))
    if draw >= 0.30 and view.opponents <= 2:
        return ("raise", pot_raise_to(view, 0.55))
    if tier == MEDIUM and heads_up and in_position:
        return ("raise", pot_raise_to(view, 0.40))
    if view.was_aggressor and heads_up and len(view.board) == 3:
        return ("raise", pot_raise_to(view, CBET_SIZE))
    return ("check", 0)


def facing_bet_postflop(view, tier, draw):
    pot_odds = view.to_call / (view.pot + view.to_call)
    equity = max(TIER_EQUITY[tier], draw)
    # More opponents, or extra raises, mean they are likelier to have us beat.
    equity *= 0.90 ** (view.opponents - 1)
    equity *= 0.80 ** max(view.raises_this_street - 1, 0)
    equity *= BARREL_DISCOUNT ** max(view.opponent_bet_streets - 1, 0)
    if pot_odds > 0.40 and tier < STRONG:
        equity *= 0.85

    commit_ratio = view.stack / max(view.pot, 1)
    if tier == MONSTER or (tier == STRONG and view.raises_this_street <= 1):
        if commit_ratio <= 1.0:
            return ("jam", 0)
        return ("raise", pot_raise_to(view, 0.75))
    if equity >= pot_odds + 0.03:
        return ("call", 0)
    return ("fold", 0)


def legalize(action, raise_to, valid_actions):
    """Map a strategy decision onto the moves on offer, never an illegal one.

    valid_actions is a list of dicts like {"action": "raise", "min": 40, "max": 2000}.
    Returns (action, amount) where amount is set only for "raise".
    """
    offered = {entry["action"]: entry for entry in valid_actions}

    def passive():
        if "check" in offered:
            return ("check", None)
        if "call" in offered:
            return ("call", None)
        if "all_in" in offered:
            return ("all_in", None)
        return ("fold", None)

    if action in ("raise", "jam"):
        entry = offered.get("raise")
        if entry is not None:
            low, high = int(entry["min"]), int(entry["max"])
            target = high if action == "jam" else max(low, min(int(raise_to), high))
            # Leaving under 15% behind is pointless: just get it in.
            if target >= 0.85 * high:
                target = high
            return ("raise", target)
        if action == "jam" and "all_in" in offered:
            return ("all_in", None)
        return passive()

    if action == "call":
        return passive()
    if "check" in offered:
        return ("check", None)
    return ("fold", None)


def safe_decide(view, valid_actions):
    """decide() + legalize(), falling back to check/fold on any strategy error."""
    try:
        action, raise_to = decide(view)
        return legalize(action, raise_to, valid_actions)
    except Exception:
        offered = {entry.get("action") for entry in valid_actions}
        return ("check", None) if "check" in offered else ("fold", None)
