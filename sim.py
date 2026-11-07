"""Quick local sanity check: the strategy against simple baseline bots.

Plays 6-max no-limit hands with stacks reset every hand and reports bb/100.
This is not the No Limit tournament format: there is no ICM or rising blinds.

Usage: python sim.py --hands 20000
"""

import argparse
import math
import random
import time

import evaluator as ev
from preflop_table import PERCENTILE
from ranges import Act
from strategy import (AIR, GOOD, MEDIUM, MONSTER, STRONG, WEAK, View, action_order, hand_key,
                      postflop_tier, safe_decide)


def offered(valid, name):
    return next((entry for entry in valid if entry["action"] == name), None)


def passive(valid):
    if offered(valid, "check"):
        return ("check", None)
    if offered(valid, "call"):
        return ("call", None)
    if offered(valid, "all_in"):
        return ("all_in", None)
    return ("fold", None)


def hero(view, valid):
    return safe_decide(view, valid)


def calling_station(view, valid):
    return passive(valid)


def maniac(view, valid):
    entry = offered(valid, "raise")
    if entry and view.rng.random() < 0.5:
        return ("raise", max(entry["min"], min(entry["max"], view.pot)))
    return passive(valid)


def random_bot(view, valid):
    roll = view.rng.random()
    entry = offered(valid, "raise")
    if roll < 0.25 and entry:
        return ("raise", view.rng.randint(entry["min"], entry["max"]))
    if roll < 0.60 and not offered(valid, "check"):
        return ("fold", None)
    return passive(valid)


def nit(view, valid):
    """Plays the top 15% of hands and folds to bets without a made hand."""
    if view.to_call == 0:
        return ("check", None)
    if not view.board:
        strong = PERCENTILE[hand_key(view.hole)] <= 0.15
        return passive(valid) if strong else ("fold", None)
    made = ev.category(ev.evaluate(view.hole + view.board)) >= ev.PAIR
    return passive(valid) if made else ("fold", None)


# Chances by hand tier, set so a table of these bots matches the logged Open Poker field.
FIELD_BET = {AIR: 0.17, WEAK: 0.17, MEDIUM: 0.25, GOOD: 0.55, STRONG: 0.75, MONSTER: 0.75}
FIELD_FOLD = {AIR: 0.62, WEAK: 0.38, MEDIUM: 0.12, GOOD: 0.03, STRONG: 0.0, MONSTER: 0.0}
FIELD_RAISE = {AIR: 0.05, WEAK: 0.05, MEDIUM: 0.05, GOOD: 0.12, STRONG: 0.40, MONSTER: 0.50}
FIELD_BET_SIZE = 0.60
FIELD_SIZE_FOLDS = 0.0  # Extra fold chance per pot of size above FIELD_BET_SIZE, for stress tests.


def field_raise(view, entry, raise_to):
    return ("raise", max(entry["min"], min(entry["max"], int(raise_to))))


def field(view, valid):
    """Copies the logged player pool: about 30% of hands played, 20% raised, few big bluffs."""
    entry = offered(valid, "raise")
    if not view.board:
        percentile = PERCENTILE[hand_key(view.hole)]
        if view.raises_this_street == 0:
            if percentile <= 0.26 and entry:
                return field_raise(view, entry, 2.5 * view.big_blind)
            return passive(valid) if percentile <= 0.33 or view.to_call == 0 else ("fold", None)
        # Logged openers facing a 3-bet call 60%, fold 30% and 4-bet 11%.
        reraise = 0.06 if view.raises_this_street == 1 else 0.03
        if percentile <= reraise and entry and view.raises_this_street < 3:
            return field_raise(view, entry, 3 * (view.my_bet + view.to_call))
        limit = {1: 0.25, 2: 0.18}.get(view.raises_this_street, 0.06)
        return passive(valid) if percentile <= limit else ("fold", None)

    tier = postflop_tier(view.hole, view.board)
    raise_to = view.my_bet + view.to_call + FIELD_BET_SIZE * (view.pot + view.to_call)
    if view.to_call == 0:
        if entry and view.rng.random() < FIELD_BET[tier]:
            return field_raise(view, entry, raise_to)
        return ("check", None)
    roll = view.rng.random()
    size = view.to_call / max(view.pot - view.to_call, 1)
    extra_fold = FIELD_SIZE_FOLDS * max(size - FIELD_BET_SIZE, 0) if tier < STRONG else 0
    if roll < FIELD_FOLD[tier] + extra_fold:
        return ("fold", None)
    if entry and roll > 1 - FIELD_RAISE[tier]:
        return field_raise(view, entry, raise_to)
    return passive(valid)


def aggro(view, valid):
    """Copies the loose logged raisers (MimicOctopus, Unkn0wn): about 70% of hands
    played, a 3-bet to about 25% of opens, and bets on most streets."""
    entry = offered(valid, "raise")
    if not view.board:
        percentile = PERCENTILE[hand_key(view.hole)]
        if view.raises_this_street == 0:
            if percentile <= 0.55 and entry:
                return field_raise(view, entry, 2.5 * view.big_blind)
            return passive(valid) if percentile <= 0.70 or view.to_call == 0 else ("fold", None)
        if view.raises_this_street == 1:
            if percentile <= 0.25 and entry:
                return field_raise(view, entry, 3 * (view.my_bet + view.to_call))
            return passive(valid) if percentile <= 0.65 else ("fold", None)
        if percentile <= 0.04 and entry and view.raises_this_street < 3:
            return field_raise(view, entry, 3 * (view.my_bet + view.to_call))
        return passive(valid) if percentile <= 0.20 else ("fold", None)

    tier = postflop_tier(view.hole, view.board)
    raise_to = view.my_bet + view.to_call + 0.75 * (view.pot + view.to_call)
    if view.to_call == 0:
        bet = 0.50 if tier <= WEAK else 0.70
        if entry and view.rng.random() < bet:
            return field_raise(view, entry, raise_to)
        return ("check", None)
    roll = view.rng.random()
    fold = {AIR: 0.45, WEAK: 0.25, MEDIUM: 0.05}.get(tier, 0.0)
    if roll < fold:
        return ("fold", None)
    if entry and roll > (0.80 if tier < GOOD else 0.55):
        return field_raise(view, entry, raise_to)
    return passive(valid)


class Player:
    def __init__(self, name, bot):
        self.name = name
        self.bot = bot
        self.reset(0)

    def reset(self, stack):
        self.stack = stack
        self.bet = 0
        self.total = 0
        self.folded = False
        self.hole = []

    def pay(self, amount):
        amount = min(amount, self.stack)
        self.stack -= amount
        self.bet += amount
        self.total += amount
        return amount

    @property
    def can_act(self):
        return not self.folded and self.stack > 0


class Table:
    def __init__(self, players, stack, big_blind, rng):
        self.players = players
        self.start_stack = stack
        self.big_blind = big_blind
        self.rng = rng
        self.dealer = 0
        self.decision_seconds = []

    @property
    def pot(self):
        return sum(player.total for player in self.players)

    def play_hand(self):
        rng, players = self.rng, self.players
        for player in players:
            player.reset(self.start_stack)
        deck = list(range(52))
        rng.shuffle(deck)
        for player in players:
            player.hole = [deck.pop(), deck.pop()]
        runout = deck[:5]

        seats = list(range(len(players)))
        postflop_order = action_order(seats, self.dealer, preflop=False)
        small, big = postflop_order[0], postflop_order[1]
        players[small].pay(self.big_blind // 2)
        players[big].pay(self.big_blind)
        preflop_order = action_order(seats, self.dealer, preflop=True)

        self.aggressor = None
        self.street_bettors = []
        self.history = []
        self.betting_round([], preflop_order, self.big_blind)
        for street_cards in (3, 4, 5):
            if sum(not player.folded for player in players) <= 1:
                break
            for player in players:
                player.bet = 0
            self.street_bettors.append(set())
            self.betting_round(runout[:street_cards], postflop_order, 0)

        self.settle(runout)
        self.dealer = (self.dealer + 1) % len(players)
        return [player.stack - self.start_stack for player in players]

    def betting_round(self, board, order, current_bet):
        players = self.players
        last_raise = self.big_blind
        raises = 0
        needs_action = [seat for seat in order if players[seat].can_act]
        while needs_action:
            seat = needs_action.pop(0)
            player = players[seat]
            if not player.can_act:
                continue
            if sum(not other.folded for other in players) <= 1:
                return
            to_call = current_bet - player.bet
            others_can_act = any(
                other.can_act for index, other in enumerate(players) if index != seat
            )
            if to_call <= 0 and not others_can_act:
                return
            valid = self.valid_actions(player, to_call, current_bet, last_raise, others_can_act)
            view = self.view(seat, board, order, to_call, valid, raises)

            started = time.perf_counter()
            action, amount = player.bot(view, valid)
            if player.bot is hero:
                self.decision_seconds.append(time.perf_counter() - started)

            action, amount = self.check_legal(action, amount, valid, player)
            self.record(seat, len(board), action, amount, to_call, player)
            if action == "fold":
                player.folded = True
            elif action == "call":
                player.pay(to_call)
            elif action in ("raise", "all_in"):
                target = amount if action == "raise" else player.bet + player.stack
                player.pay(target - player.bet)
                if player.bet > current_bet:
                    increase = player.bet - current_bet
                    if increase >= last_raise:
                        last_raise = increase
                    current_bet = player.bet
                    raises += 1
                    self.aggressor = seat
                    if board:
                        self.street_bettors[-1].add(seat)
                    index = order.index(seat)
                    next_seats = order[index + 1:] + order[:index]
                    needs_action = [s for s in next_seats if players[s].can_act]

    def record(self, seat, board_size, action, amount, to_call, player):
        pot = self.pot + max(to_call, 0)
        target = amount if action == "raise" else player.bet + player.stack
        extra = target - player.bet - to_call if action in ("raise", "all_in") else 0
        kind = action
        if action in ("raise", "all_in"):
            kind = "call" if extra <= 0 else "raise" if to_call > 0 else "bet"
        self.history.append(Act(seat, board_size, kind, max(extra, 0) / pot))

    @staticmethod
    def valid_actions(player, to_call, current_bet, last_raise, others_can_act):
        valid = [{"action": "fold"}]
        if to_call <= 0:
            valid = [{"action": "check"}]
        else:
            valid.append({"action": "call", "amount": min(to_call, player.stack)})
        max_total = player.bet + player.stack
        min_total = current_bet + last_raise
        if others_can_act and player.stack > to_call:
            if max_total >= min_total:
                valid.append({"action": "raise", "min": min_total, "max": max_total})
            valid.append({"action": "all_in", "amount": player.stack})
        return valid

    @staticmethod
    def check_legal(action, amount, valid, player):
        entry = offered(valid, action)
        if entry is None:
            raise ValueError(f"{player.name} chose illegal {action}: {valid}")
        if action == "raise" and not entry["min"] <= amount <= entry["max"]:
            raise ValueError(f"{player.name} raise {amount} outside {entry}")
        return action, amount

    def view(self, seat, board, order, to_call, valid, raises):
        player = self.players[seat]
        live = [s for s in order if not self.players[s].folded]
        opponent_stacks = [self.players[s].stack for s in live if s != seat]
        entry = offered(valid, "raise") or {}
        return View(
            hole=player.hole,
            board=board,
            pot=self.pot,
            to_call=to_call,
            stack=player.stack,
            big_blind=self.big_blind,
            min_raise=entry.get("min", 0),
            max_raise=entry.get("max", 0),
            my_bet=player.bet,
            opponents=max(len(opponent_stacks), 1),
            players_behind=len(live) - 1 - live.index(seat),
            raises_this_street=raises,
            effective_stack=min(player.stack, max(opponent_stacks, default=player.stack)),
            was_aggressor=self.aggressor == seat,
            opponent_bet_streets=sum(bool(bettors - {seat}) for bettors in self.street_bettors),
            history=tuple(self.history),
            opponent_seats=tuple(s for s in live if s != seat),
            rng=self.rng,
        )

    def settle(self, runout):
        players = self.players
        live = [p for p in players if not p.folded]
        scores = {}
        if len(live) > 1:
            for player in live:
                scores[id(player)] = ev.evaluate(player.hole + runout)
        for level in sorted({p.total for p in players if p.total > 0}):
            previous = max((t for t in (p.total for p in players) if t < level), default=0)
            slice_size = level - previous
            pot = sum(slice_size for p in players if p.total >= level)
            eligible = [p for p in live if p.total >= level]
            if not eligible:
                # Everyone who paid this level folded: it goes to the live players.
                eligible = live
            best = max((scores.get(id(p), 0) for p in eligible), default=0)
            winners = [p for p in eligible if scores.get(id(p), 0) == best]
            share, odd = divmod(pot, len(winners))
            for index, winner in enumerate(winners):
                winner.stack += share + (1 if index < odd else 0)
        if sum(p.stack for p in players) != self.start_stack * len(players):
            raise AssertionError("chips were not conserved")


LINEUPS = {
    "mixed field": [("station", calling_station), ("maniac", maniac), ("random", random_bot),
                    ("nit", nit), ("station2", calling_station)],
    "all stations": [(f"station{i}", calling_station) for i in range(5)],
    "all maniacs": [(f"maniac{i}", maniac) for i in range(5)],
    "all nits": [(f"nit{i}", nit) for i in range(5)],
    "all random": [(f"random{i}", random_bot) for i in range(5)],
    "logged field": [(f"field{i}", field) for i in range(5)],
    "field + 1 aggro": [("aggro0", aggro)] + [(f"field{i}", field) for i in range(4)],
    "field + 2 aggro": [("aggro0", aggro), ("aggro1", aggro)] + [(f"field{i}", field) for i in range(3)],
    "all aggro": [(f"aggro{i}", aggro) for i in range(5)],
}


def play(lineup, hands, seed, stack=2000, big_blind=20):
    rng = random.Random(seed)
    players = [Player("hero", hero)] + [Player(name, bot) for name, bot in lineup]
    rng.shuffle(players)
    table = Table(players, stack, big_blind, rng)
    hero_index = next(i for i, player in enumerate(players) if player.bot is hero)
    results = [table.play_hand()[hero_index] / big_blind for _ in range(hands)]
    mean = sum(results) / hands
    deviation = math.sqrt(sum((r - mean) ** 2 for r in results) / (hands - 1))
    return mean * 100, 196 * deviation / math.sqrt(hands), table.decision_seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hands", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    timings = []
    for name, lineup in LINEUPS.items():
        winrate, margin, seconds = play(lineup, args.hands, args.seed)
        timings += seconds
        print(f"{name:14s} {winrate:+8.1f} bb/100  (95% interval +/- {margin:.1f})")

    timings.sort()
    count = len(timings)
    print(f"decisions: {count}, median {timings[count // 2] * 1000:.3f} ms, "
          f"p99.9 {timings[int(count * 0.999)] * 1000:.3f} ms, "
          f"max {timings[-1] * 1000:.3f} ms")


if __name__ == "__main__":
    main()
