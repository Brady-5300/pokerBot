import asyncio
import itertools
import json
import random
import sys
from pathlib import Path

import pytest
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import evaluator as ev
import openpoker_client as client
import ranges
import sim
import strategy
from ranges import Act
from strategy import View, action_order, legalize, postflop_tier


def cards(text):
    return [ev.parse_card(card) for card in text.split()]


def test_all_five_card_hand_categories_match_known_counts():
    counts = [0] * 9
    for hand in itertools.combinations(range(52), 5):
        counts[ev.category(ev.evaluate(hand))] += 1
    assert counts == [1302540, 1098240, 123552, 54912, 10200, 5108, 3744, 624, 40]


@pytest.mark.parametrize("better, worse", [
    ("As Ks Qs Js Ts 2d 3c", "9h 9d 9c 9s 2d 3c 4h"),  # royal over quads
    ("Ah 2d 3c 4s 5h 9d Kc", "Ah Ad Ac 2s 3h 9d Kc"),  # wheel over trips
    ("6h 2d 3c 4s 5h 9d Kc", "Ah 2d 3c 4s 5h 9d Kc"),  # six-high straight over wheel
    ("Kh Kd Kc 2s 2h 9d 9c", "Qh Qd Qc As Ah 9d 9c"),  # full house by trips rank
    ("Ah Ad Kc Kd 3h 3s 2c", "Ah Ad Qc Qd Jh Js Tc"),  # kings up over queens up
    ("Ah Ad Kc 7d 3h 2s 9c", "Ah Ad Qc 7d 3h 2s 9c"),  # pair kicker
    ("Ah 9h 7h 4h 2h Kd Qc", "Kh Qh Jh 9h 7h Ad Ac"),  # ace-high flush
])
def test_seven_card_ordering(better, worse):
    assert ev.evaluate(cards(better)) > ev.evaluate(cards(worse))


def test_seven_cards_equals_best_five_card_subset():
    rng = random.Random(5)
    for _ in range(3000):
        hand = rng.sample(range(52), 7)
        best = max(ev.evaluate(combo) for combo in itertools.combinations(hand, 5))
        assert ev.evaluate(hand) == best


def test_two_full_houses_use_highest_pair_or_second_trips():
    assert ev.evaluate(cards("Ah Ad Ac Kh Kd Ks 2c")) > ev.evaluate(cards("Ah Ad Ac Qh Qd 3s 2c"))


@pytest.mark.parametrize("hole, board, expected", [
    ("Ah Ad", "7c 2d 9s", strategy.GOOD),        # overpair
    ("7h 7d", "7c 2d Ks", strategy.MONSTER),     # set
    ("Ah Kd", "Ac 7d 2s", strategy.GOOD),        # top pair, top kicker
    ("9h 8d", "As 8c 2d", strategy.MEDIUM),      # middle pair
    ("3h 4d", "Kc 9d 2s", strategy.AIR),
    ("Ah 3d", "Kc 9d 2s", strategy.WEAK),        # ace high
    ("2h 3d", "Ah Kh Qh Jh Th", strategy.WEAK),  # board plays
    ("Qd Td", "Kh Js 7s Jc 5c", strategy.AIR),   # the pair is on the board
    ("Ad 4s", "7h 7s 9c", strategy.WEAK),        # ace high on a paired board
    ("Ad Ks", "7h 7s 7c", strategy.WEAK),        # the trips are on the board
    ("Qd Td", "7h 7s 5c 5d", strategy.AIR),      # both pairs are on the board
    ("Jd Td", "Kh Js 7s 7c", strategy.MEDIUM),   # our pair plus the board's pair
    ("Kd Td", "Kh Ks 7s 2c", strategy.STRONG),   # trips with one of our cards
])
def test_postflop_tiers(hole, board, expected):
    assert postflop_tier(cards(hole), cards(board)) == expected


def facing_river_bet(streets):
    # Hand 94 from the live logs: a big river bet into two pair.
    return View(hole=cards("As Tc"), board=cards("8h Qs Ac 6c Qd"), pot=4561, to_call=1797,
                stack=5495, big_blind=20, raises_this_street=1, opponent_bet_streets=streets)


def test_a_bet_on_every_street_folds_a_hand_that_calls_a_single_bet():
    assert strategy.decide(facing_river_bet(1))[0] == "call"
    assert strategy.decide(facing_river_bet(3))[0] == "fold"


def test_strong_hands_still_call_after_a_bet_on_every_street():
    view = View(hole=cards("8c Ac"), board=cards("4c Jh Ad 8h Jd"), pot=1883, to_call=425,
                stack=5000, big_blind=20, raises_this_street=2, opponent_bet_streets=3)
    assert strategy.decide(view)[0] == "call"


def test_position_order_six_handed_and_heads_up():
    assert action_order([0, 1, 2, 3, 4, 5], 0, preflop=True) == [3, 4, 5, 0, 1, 2]
    assert action_order([0, 1, 2, 3, 4, 5], 0, preflop=False) == [1, 2, 3, 4, 5, 0]
    assert action_order([1, 4], 4, preflop=True) == [4, 1]
    assert action_order([1, 4], 4, preflop=False) == [1, 4]


FULL = [{"action": "fold"}, {"action": "call", "amount": 20},
        {"action": "raise", "min": 40, "max": 2000}, {"action": "all_in", "amount": 2000}]


def test_legalize_clamps_raises_and_never_folds_a_free_check():
    assert legalize("raise", 5, FULL) == ("raise", 40)
    assert legalize("raise", 100, FULL) == ("raise", 100)
    assert legalize("raise", 1900, FULL) == ("raise", 2000)
    assert legalize("jam", 0, FULL) == ("raise", 2000)
    assert legalize("fold", 0, [{"action": "fold"}, {"action": "check"}]) == ("check", None)


def test_legalize_only_uses_all_in_when_offered():
    short = [{"action": "fold"}, {"action": "call", "amount": 50}]
    assert legalize("jam", 0, short) == ("call", None)
    assert legalize("jam", 0, short + [{"action": "all_in", "amount": 50}]) == ("all_in", None)


def test_strategy_error_falls_back_to_check_or_fold():
    broken = View(hole=[], board=[], pot=30, to_call=20, stack=2000, big_blind=20)
    assert strategy.safe_decide(broken, FULL) == ("fold", None)
    assert strategy.safe_decide(broken, [{"action": "check"}]) == ("check", None)


def test_aces_raise_and_seven_deuce_folds_under_the_gun():
    base = dict(board=[], pot=30, to_call=20, stack=2000, big_blind=20,
                min_raise=40, max_raise=2000, players_behind=5, opponents=5,
                effective_stack=2000)
    assert strategy.decide(View(hole=cards("Ah Ad"), **base))[0] == "raise"
    assert strategy.decide(View(hole=cards("7h 2d"), **base))[0] == "fold"


def turn(**overrides):
    message = {
        "type": "your_turn", "hand_id": "h1", "turn_token": "t1", "valid_actions": FULL,
        "pot": 30, "community_cards": [], "min_raise": 40, "max_raise": 2000,
        "players": [{"seat": 0, "name": "them", "stack": 1980},
                    {"seat": 3, "name": "me", "stack": 2000}],
    }
    message.update(overrides)
    return message


def seated_session():
    session = client.Session(random.Random(1))
    session.handle({"type": "table_joined", "table_id": "t", "seat": 3, "players": []})
    session.handle({"type": "hand_start", "hand_id": "h1", "seat": 3, "dealer_seat": 3,
                    "blinds": {"small_blind": 10, "big_blind": 20}})
    session.handle({"type": "hole_cards", "cards": ["Ah", "Ad"]})
    return session


def test_your_turn_echoes_token_and_hand_and_uses_raise_to():
    [message] = seated_session().handle(turn())
    assert message["type"] == "action"
    assert (message["hand_id"], message["turn_token"]) == ("h1", "t1")
    assert message["action"] == "raise" and 40 <= message["amount"] <= 2000
    assert message["client_action_id"]


def test_missing_hole_cards_still_acts_safely():
    session = client.Session()
    [message] = session.handle(turn())
    assert message["action"] == "fold"


def test_rejected_action_retries_once_with_a_fresh_id_then_stops():
    session = seated_session()
    [first] = session.handle(turn())
    [retry] = session.handle({"type": "action_rejected", "code": "invalid_action", "reason": "x"})
    assert retry["action"] == "fold"
    assert retry["client_action_id"] != first["client_action_id"]
    assert session.handle({"type": "action_rejected", "code": "invalid_action"}) == []


def test_lifecycle_messages():
    session = client.Session()
    assert session.handle({"type": "connected", "name": "b"}) == [{"type": "join_lobby", "buy_in": 5000}]
    assert session.handle({"type": "lobby_joined", "position": 1}) == [
        {"type": "set_auto_rebuy", "enabled": True}]
    assert session.handle({"type": "lobby_joined", "position": 1}) == []
    assert session.handle({"type": "error", "code": "insufficient_season_chips"}) == [
        {"type": "rebuy", "amount": 0}]
    assert session.handle({"type": "rebuy_confirmed", "chip_balance": 1500})[0]["type"] == "join_lobby"
    [later] = session.handle({"type": "auto_rebuy_scheduled", "cooldown_seconds": 300})
    assert later.delay == 301 and later.message["type"] == "join_lobby"
    assert session.handle({"type": "some_future_message"}) == []


def test_low_balance_tries_smaller_buy_in_before_rebuying():
    session = client.Session()
    for buy_in in (2500, 1250, 1000):
        assert session.handle({"type": "error", "code": "insufficient_funds"}) == [
            {"type": "join_lobby", "buy_in": buy_in}]
    assert session.handle({"type": "error", "code": "insufficient_funds"}) == [
        {"type": "rebuy", "amount": 0}]
    session.handle({"type": "table_joined", "table_id": "t", "seat": 1})
    assert session.join_lobby()["buy_in"] == 5000


def test_failed_rebuy_retries_after_a_delay():
    session = client.Session()
    session.handle({"type": "error", "code": "insufficient_season_chips"})
    [later] = session.handle({"type": "error", "code": "some_cooldown_code"})
    assert later.delay == client.REBUY_RETRY_DELAY and later.message["type"] == "join_lobby"
    assert session.handle({"type": "error", "code": "some_cooldown_code"}) == []


def test_new_season_turns_auto_rebuy_back_on():
    session = client.Session()
    session.handle({"type": "lobby_joined", "position": 1})
    assert session.handle({"type": "season_ended", "season_number": 15})[0]["type"] == "join_lobby"
    assert session.handle({"type": "lobby_joined", "position": 1}) == [
        {"type": "set_auto_rebuy", "enabled": True}]


def test_hand_already_running_at_sit_down_is_not_counted():
    session = client.Session()
    session.handle({"type": "hand_result", "final_stacks": {"1": 500}})
    assert session.hands_played == 0


def test_reconnect_resyncs_and_acts_from_snapshot():
    session = seated_session()
    session.handle({"type": "player_action", "seat": 0, "action": "call", "table_id": "t", "table_seq": 9})
    assert session.handle({"type": "connected"}) == [
        {"type": "resync_request", "table_id": "t", "last_table_seq": 9}]
    [message] = session.handle({"type": "resync_response", "role": "player", "snapshot": {
        "hand_id": "h1", "pot": 40, "board": [], "seats": turn()["players"],
        "hero": {"seat": 3, "hole_cards": ["Ah", "Ad"], "valid_actions": FULL, "turn_token": "t2"},
    }})
    assert message["turn_token"] == "t2"


def test_preflop_bet_includes_posted_blind():
    seats = [{"seat": seat, "name": "p", "stack": 2000} for seat in range(6)]
    session = client.Session()
    session.handle({"type": "hand_start", "hand_id": "h1", "seat": 0, "dealer_seat": 5,
                    "blinds": {"small_blind": 10, "big_blind": 20}})
    session.handle({"type": "hole_cards", "cards": ["Ah", "Ad"]})
    assert session.build_view(turn(players=seats)).my_bet == 10
    session.seat = 1
    assert session.build_view(turn(players=seats)).my_bet == 20
    session.seat = 2
    assert session.build_view(turn(players=seats)).my_bet == 0
    flop = turn(players=seats, community_cards=["2c", "3c", "4c"])
    session.seat = 0
    assert session.build_view(flop).my_bet == 0


def test_valid_actions_with_null_fields_from_the_live_server():
    session = seated_session()
    live = [{"action": "fold", "amount": None, "min": None, "max": None},
            {"action": "call", "amount": 214, "min": None, "max": None},
            {"action": "raise", "amount": None, "min": 378, "max": 2000},
            {"action": "all_in", "amount": 2000, "min": None, "max": None}]
    [message] = session.handle(turn(valid_actions=live))
    assert message["action"] in ("call", "raise")
    assert session.pending_turn is not None


def test_raises_are_counted_but_blind_sized_actions_are_not():
    session = seated_session()
    session.handle({"type": "player_action", "seat": 0, "action": "raise", "amount": 20})
    assert session.hand.raises == 0
    session.handle({"type": "player_action", "seat": 0, "action": "raise", "amount": 60})
    assert session.hand.raises == 1
    session.handle({"type": "community_cards", "cards": ["2c", "3c", "4c"], "street": "flop"})
    assert session.hand.raises == 0


@pytest.mark.parametrize("hole, expected", [("Ah 3d", {"check"}), ("3h 4d", {"check"}),
                                            ("9h 8d", {"check", "raise"})])
def test_continuation_bets_need_at_least_a_medium_hand(hole, expected):
    actions = set()
    for seed in range(40):
        view = View(hole=cards(hole), board=cards("As 8c 2d") if hole == "9h 8d" else cards("Kc 9d 2s"),
                    pot=110, to_call=0, stack=2000, big_blind=20, players_behind=1,
                    was_aggressor=True, rng=random.Random(seed))
        actions.add(strategy.decide(view)[0])
    assert actions == expected


def test_value_bets_scale_with_hand_strength():
    base = dict(board=cards("Ac 7d 2s"), pot=100, to_call=0, stack=2000, big_blind=20)
    assert strategy.decide(View(hole=cards("Ah Kd"), **base)) == ("raise", 70)
    assert strategy.decide(View(hole=cards("7h 7s"), **base)) == ("raise", 90)


def test_field_bots_play_only_legal_actions():
    sim.play(sim.LINEUPS["logged field"], 300, 9, stack=5000)


def river_view(hole, to_call, pot=1000, **overrides):
    act = Act(0, 5, "bet", to_call / max(pot - to_call, 1)) if to_call else Act(0, 5, "check")
    settings = dict(hole=cards(hole), board=cards("2c 7d Jh Qs 3d"), pot=pot, to_call=to_call,
                    stack=5000, big_blind=20, opponents=1, opponent_seats=(0,),
                    history=(Act(0, 0, "raise", 2.5), Act(0, 3, "check"), Act(0, 4, "check"), act),
                    samples=600, rng=random.Random(4))
    settings.update(overrides)
    return View(**settings)


def test_a_bet_moves_weight_to_strong_hands_and_keeps_some_bluffs():
    board = cards("2c 7d Jh Qs 3d")
    table = ranges.street_table(board)
    weights = {combo: 1.0 for combo in table}
    narrowed = ranges.narrow(weights, table, "bet", 1.0)
    assert narrowed[tuple(sorted(cards("Qh Qd"), reverse=True))] == 1.0
    assert 0 < narrowed[tuple(sorted(cards("5h 4c"), reverse=True))] < 1.0


def test_preflop_ranges_tighten_with_each_raise():
    history = [Act(0, 0, "raise"), Act(1, 0, "raise"), Act(2, 0, "call")]
    assert ranges.preflop_shares(history) == {0: 0.20, 1: 0.06, 2: 0.10}


def test_equity_pricing_folds_air_and_raises_the_nuts():
    assert strategy.decide(river_view("5h 4h", 1000, pot=2000))[0] == "fold"
    action, amount = strategy.decide(river_view("Ks Tc", 500, pot=1500, board=cards("2c 7d Jh Qs Ad")))
    assert action == "raise" and amount > 500


def test_equity_pricing_bets_value_when_checked_to():
    view = river_view("Qh Qd", 0, pot=400, players_behind=0)
    assert strategy.decide(view)[0] == "raise"


def test_equity_matches_a_known_price():
    hole, board = cards("Ah Ad"), cards("2c 7d Jh")
    weights = {combo: 1.0 for combo in ranges.street_table(board)
               if not set(combo) & set(hole)}
    _, equity = ranges.price_options(hole, board, 100, 0, 0, 2000, {0: weights},
                                     random.Random(2), 3000, True)
    assert 0.86 < equity < 0.93


def test_pricing_errors_fall_back_to_the_rule_based_decision(monkeypatch):
    def broken(view):
        raise ValueError
    monkeypatch.setattr(ranges, "best_option", broken)
    assert strategy.decide(river_view("5h 4h", 1000, pot=2000))[0] == "fold"


def test_client_records_each_action_for_range_estimates():
    session = seated_session()
    session.handle({"type": "player_action", "seat": 0, "action": "raise", "amount": 50,
                    "contribution_delta": 50, "to_call_before": 20, "pot_before": 30})
    session.handle({"type": "community_cards", "cards": ["2c", "3c", "4c"], "street": "flop"})
    session.handle({"type": "player_action", "seat": 0, "action": "all_in", "amount": None,
                    "contribution_delta": 900, "to_call_before": 0, "pot_before": 300})
    session.handle({"type": "player_action", "seat": 1, "action": "all_in", "amount": None,
                    "contribution_delta": 400, "to_call_before": 900, "pot_before": 1200})
    assert [(act.street_cards, act.kind) for act in session.hand.history] == [
        (0, "raise"), (3, "bet"), (3, "call")]
    assert session.hand.history[1].size == 3.0
    view = session.build_view(turn(community_cards=["2c", "3c", "4c"]))
    assert view.samples == client.SAMPLES and view.opponent_seats == (0,)


def test_simulator_works_with_equity_pricing(monkeypatch):
    def priced(view, valid):
        view.samples = 40
        return strategy.safe_decide(view, valid)
    monkeypatch.setattr(sim, "hero", priced)
    rate, _, _ = sim.play(sim.LINEUPS["mixed field"], 60, 3)
    assert rate == rate


def test_all_ins_with_no_amount_count_as_raises():
    session = seated_session()
    session.handle({"type": "player_action", "seat": 0, "action": "all_in", "amount": None,
                    "contribution_delta": 1980})
    assert session.hand.raises == 1


def test_opponent_bet_streets_count_postflop_bets_once_per_street():
    session = seated_session()
    raise_by = lambda seat: {"type": "player_action", "seat": seat, "action": "raise", "amount": 60}
    session.handle(raise_by(0))  # Preflop raises don't count.
    assert session.build_view(turn()).opponent_bet_streets == 0
    session.handle({"type": "community_cards", "cards": ["2c", "3c", "4c"], "street": "flop"})
    session.handle(raise_by(3))  # Neither do our own bets.
    assert session.build_view(turn()).opponent_bet_streets == 0
    session.handle(raise_by(0))
    session.handle(raise_by(0))
    assert session.build_view(turn()).opponent_bet_streets == 1
    session.handle({"type": "community_cards", "cards": ["2c", "3c", "4c", "5d"], "street": "turn"})
    assert session.build_view(turn()).opponent_bet_streets == 1
    session.handle(raise_by(0))
    assert session.build_view(turn()).opponent_bet_streets == 2


def test_client_plays_a_hand_over_a_real_websocket():
    received = []

    async def server(socket):
        assert socket.request.headers["Authorization"] == "Bearer test-key"
        await socket.send(json.dumps({"type": "connected", "agent_id": "a", "name": "bot"}))
        received.append(json.loads(await socket.recv()))  # join_lobby
        await socket.send(json.dumps({"type": "lobby_joined", "position": 1}))
        received.append(json.loads(await socket.recv()))  # set_auto_rebuy
        for message in (
            {"type": "table_joined", "table_id": "t", "seat": 3, "players": []},
            {"type": "hand_start", "hand_id": "h1", "seat": 3, "dealer_seat": 3,
             "blinds": {"small_blind": 10, "big_blind": 20}},
            {"type": "hole_cards", "cards": ["Ah", "Ad"]},
            turn(),
        ):
            await socket.send(json.dumps(message))
        received.append(json.loads(await socket.recv()))  # action
        await socket.send(json.dumps({"type": "hand_result", "final_stacks": {"3": 2030}}))
        received.append(json.loads(await socket.recv()))  # leave_table

    async def main():
        async with websockets.serve(server, "127.0.0.1", 0) as instance:
            port = instance.sockets[0].getsockname()[1]
            await asyncio.wait_for(
                client.run("test-key", f"ws://127.0.0.1:{port}", max_hands=1), timeout=10)

    asyncio.run(main())
    assert [m["type"] for m in received] == ["join_lobby", "set_auto_rebuy", "action", "leave_table"]
    assert received[2]["action"] == "raise"


def test_api_key_comes_from_env_first_then_key_file(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    monkeypatch.delenv("OPEN_POKER_API_KEY", raising=False)
    assert client.load_api_key(key_file) is None
    key_file.write_bytes(b"\xef\xbb\xbffile-key\r\n")
    assert client.load_api_key(key_file) == "file-key"
    monkeypatch.setenv("OPEN_POKER_API_KEY", "env-key")
    assert client.load_api_key(key_file) == "env-key"


def test_simulator_conserves_chips_and_every_action_is_legal():
    winrate, margin, timings = sim.play(sim.LINEUPS["mixed field"], 2000, seed=3)
    assert timings
