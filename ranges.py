"""Estimates what opponents hold from how they bet, then prices our options.

Each opponent starts from the hands a typical player would play, and every
action narrows that range. We then deal out the rest of the hand many times
to find the expected chips of each option. Every number below is a fixed
assumption about the player pool, set from logged opponent behavior and
game theory. None are fitted to whether particular hands won or lost.
"""

import itertools
import time
from dataclasses import dataclass

import evaluator as ev
from preflop_table import PERCENTILE

# Share of hands played preflop, from logged VPIP and PFR near 30% and 20%.
OPEN_RAISE_SHARE = 0.20
THREE_BET_SHARE = 0.06
FOUR_BET_SHARE = 0.02
CALL_RAISE_SHARE = 0.25
CALL_THREE_BET_SHARE = 0.10
LIMP_SHARE = 0.35

# From 403 logged unbet spots and 367 responses to a bet.
BET_SHARE = 0.29
RAISE_SHARE = 0.15
FOLD_SHARE = 0.45
# Hands a checker or caller would usually have raised, kept at a low weight.
CHECK_TRAP_SHARE = 0.20
CHECK_TRAP_WEIGHT = 0.30
CALL_TRAP_SHARE = 0.15
CALL_TRAP_WEIGHT = 0.20
CALL_FOLD_WEIGHT = 0.10

# Out of position we can't always see the showdown our equity assumes.
REALIZED_EQUITY = {3: 0.90, 4: 0.95, 5: 1.00}
BET_SIZES = (0.33, 0.66, 1.00)
RAISE_SIZES = (1.00,)
DRAW_OUTS = 8
MIN_SAMPLES = 200

COMBOS = list(itertools.combinations(range(51, -1, -1), 2))


@dataclass(frozen=True)
class Act:
    seat: int
    street_cards: int  # Board size when the action happened: 0, 3, 4 or 5.
    kind: str  # "bet", "raise", "call", "check" or "fold".
    size: float = 0.0  # Chips above the call, as a fraction of the pot.


@dataclass
class Option:
    action: str  # "check", "call" or "raise".
    fraction: float = 0.0
    chips: int = 0
    total: float = 0.0
    cuts: dict = None


def hand_key(hole):
    high, low = sorted(hole, reverse=True)
    high_rank, low_rank = high >> 2, low >> 2
    if high_rank == low_rank:
        return ev.RANKS[high_rank] * 2
    suited = "s" if high & 3 == low & 3 else "o"
    return ev.RANKS[high_rank] + ev.RANKS[low_rank] + suited


def draw_outs(hole, board):
    """Rough outs from flush and straight draws."""
    if len(board) >= 5:
        return 0
    cards = hole + board
    suits = [0, 0, 0, 0]
    rank_mask = 0
    for card in cards:
        suits[card & 3] += 1
        rank_mask |= 1 << (card >> 2)
    outs = 0
    if any(suits[card & 3] == 4 for card in hole):
        outs += 9
    straight_ranks = ev.straight_outs(rank_mask)
    outs += min(straight_ranks, 2) * 4
    return min(outs, 15)


def preflop_shares(history):
    """Top share of starting hands each seat's preflop actions suggest."""
    raises = 0
    shares = {}
    for act in history:
        if act.street_cards != 0:
            continue
        if act.kind in ("bet", "raise"):
            raises += 1
            shares[act.seat] = (OPEN_RAISE_SHARE if raises == 1
                                else THREE_BET_SHARE if raises == 2 else FOUR_BET_SHARE)
        elif act.kind == "call":
            shares[act.seat] = (LIMP_SHARE if raises == 0
                                else CALL_RAISE_SHARE if raises == 1 else CALL_THREE_BET_SHARE)
    return shares


def street_table(board):
    """Every possible hand's (score, has_draw) on this board."""
    dead = set(board)
    board = list(board)
    table = {}
    for combo in COMBOS:
        if combo[0] in dead or combo[1] in dead:
            continue
        hole = list(combo)
        table[combo] = (ev.evaluate(hole + board), draw_outs(hole, board) >= DRAW_OUTS)
    return table


def narrow(weights, table, kind, size):
    """Reweight a range after one action, walking from the strongest hand down."""
    order = sorted(weights, key=lambda combo: table[combo][0], reverse=True)
    total = sum(weights.values())
    if total <= 0:
        return weights

    result = {}
    seen = 0.0
    if kind in ("bet", "raise"):
        # Game theory: a bet of size s can carry s / (1 + 2s) bluffs.
        bluffs = size / (1 + 2 * size)
        share = BET_SHARE if kind == "bet" else RAISE_SHARE
        value = share * (1 - bluffs)
        rest = max(1 - value, 1e-9)
        bluff_weight = min(share * bluffs / rest, 1.0)
        for combo in order:
            position = seen / total
            seen += weights[combo]
            result[combo] = weights[combo] * (1.0 if position < value else bluff_weight)
    elif kind == "call":
        for combo in order:
            position = seen / total
            seen += weights[combo]
            factor = 1.0
            if position < CALL_TRAP_SHARE:
                factor = CALL_TRAP_WEIGHT
            elif position > 1 - FOLD_SHARE and not table[combo][1]:
                factor = CALL_FOLD_WEIGHT
            result[combo] = weights[combo] * factor
    elif kind == "check":
        for combo in order:
            position = seen / total
            seen += weights[combo]
            factor = CHECK_TRAP_WEIGHT if position < CHECK_TRAP_SHARE else 1.0
            result[combo] = weights[combo] * factor
    else:
        return weights
    return result


def estimate_range(seat, hole, board, history, tables, shares):
    share = shares.get(seat, 1.0)
    dead = set(hole) | set(board)
    weights = {
        combo: 1.0 for combo in COMBOS
        if combo[0] not in dead and combo[1] not in dead
        and PERCENTILE[hand_key(list(combo))] <= share
    }
    for act in history:
        if act.seat != seat or act.street_cards == 0 or act.street_cards > len(board):
            continue
        table = tables[act.street_cards]
        weights = narrow(weights, table, act.kind, act.size)
    weights = {combo: weight for combo, weight in weights.items() if weight > 0}
    if not weights:
        weights = {combo: 1.0 for combo in COMBOS
                   if combo[0] not in dead and combo[1] not in dead}
    return weights


def continue_cut(weights, table, fold_share):
    """The weakest made-hand score that calls when `fold_share` of the range folds."""
    order = sorted(weights, key=lambda combo: table[combo][0], reverse=True)
    target = sum(weights.values()) * (1 - fold_share)
    seen = 0.0
    for combo in order:
        seen += weights[combo]
        if seen >= target:
            return table[combo][0]
    return table[order[-1]][0]


def fold_share(fraction):
    """Logged folds barely move with size, and theory sets the floor for big bets."""
    return max(FOLD_SHARE, fraction / (1 + fraction))


def build_options(pot, to_call, my_bet, stack, ranges, table):
    passive = Option("call" if to_call else "check", chips=min(to_call, stack))
    options = [passive]
    for fraction in (RAISE_SIZES if to_call else BET_SIZES):
        chips = int(to_call + fraction * (pot + to_call))
        if chips >= stack:
            chips = stack
        if chips <= to_call:
            continue
        pressure = (chips - to_call) / max(pot + to_call, 1)
        cuts = {seat: continue_cut(weights, table, fold_share(pressure))
                for seat, weights in ranges.items()}
        options.append(Option("raise", fraction, chips, 0.0, cuts))
    return options


def price_options(hole, board, pot, to_call, my_bet, stack, ranges, rng, samples,
                  in_position, seconds=3.0):
    """Returns (options with expected chips, our equity against every range)."""
    table = street_table(board)
    options = build_options(pot, to_call, my_bet, stack, ranges, table)
    seats = list(ranges)
    picks = []
    for seat in seats:
        combos = list(ranges[seat])
        picks.append(rng.choices(combos, weights=[ranges[seat][c] for c in combos], k=samples))

    dead = set(hole) | set(board)
    deck = [card for card in range(52) if card not in dead]
    needed = 5 - len(board)
    realized = 1.0 if in_position else REALIZED_EQUITY[len(board)]
    deadline = time.perf_counter() + seconds
    played = 0
    equity = 0.0

    for index in range(samples):
        hands = [picks[slot][index] for slot in range(len(seats))]
        cards = [card for hand in hands for card in hand]
        if len(set(cards)) != len(cards):
            continue
        taken = set(cards)
        runout = rng.sample([card for card in deck if card not in taken], needed)
        final = board + runout
        mine = ev.evaluate(hole + final)
        theirs = [ev.evaluate(list(hand) + final) for hand in hands]
        played += 1

        share = showdown_share(mine, theirs)
        equity += share
        for option in options:
            if option.action == "raise":
                option.total += raise_value(option, hands, seats, theirs, mine, pot, to_call,
                                            table, realized)
            else:
                option.total += realized * share * (pot + option.chips) - option.chips
        if index >= MIN_SAMPLES and index % 50 == 0 and time.perf_counter() > deadline:
            break

    played = max(played, 1)
    for option in options:
        option.total /= played
    return options, equity / played


def showdown_share(mine, theirs):
    best = max(theirs, default=-1)
    if mine < best:
        return 0.0
    return 1.0 / (1 + sum(score == mine for score in theirs))


def raise_value(option, hands, seats, theirs, mine, pot, to_call, table, realized):
    callers = [
        score for seat, hand, score in zip(seats, hands, theirs)
        if table[hand][0] >= option.cuts[seat] or table[hand][1]
    ]
    if not callers:
        return pot
    extra = option.chips - to_call
    final_pot = pot + option.chips + extra * len(callers)
    return realized * showdown_share(mine, callers) * final_pot - option.chips


def best_option(view):
    """Pick the option with the most expected chips. Folding is worth zero."""
    board = list(view.board)
    hole = list(view.hole)
    tables = {cards: street_table(board[:cards]) for cards in (3, 4, 5) if cards <= len(board)}
    shares = preflop_shares(view.history)
    seats = view.opponent_seats
    ranges = {seat: estimate_range(seat, hole, board, view.history, tables, shares)
              for seat in seats}
    options, _ = price_options(hole, board, view.pot, view.to_call, view.my_bet, view.stack,
                               ranges, view.rng, view.samples, view.players_behind == 0)
    best = max(options, key=lambda option: option.total)
    if view.to_call and best.total <= 0:
        return ("fold", 0)
    if best.action == "raise":
        return ("raise", view.my_bet + best.chips)
    return (best.action, 0)
