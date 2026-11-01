"""Fast pure-Python poker hand evaluator.

Cards are ints 0-51: rank = card >> 2 (0 = deuce, 12 = ace), suit = card & 3.
evaluate() takes 5 or more cards and returns an int; a higher score wins.
"""

RANKS = "23456789TJQKA"
SUITS = "hdcs"

HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)

_CATEGORY_SHIFT = 20


def parse_card(text):
    return RANKS.index(text[0]) << 2 | SUITS.index(text[1])


def card_text(card):
    return RANKS[card >> 2] + SUITS[card & 3]


def _build_straight_table():
    table = [-1] * 8192
    wheel = 0b1000000001111
    for mask in range(8192):
        best = -1
        for high in range(12, 3, -1):
            window = 0b11111 << (high - 4)
            if mask & window == window:
                best = high
                break
        if best < 0 and mask & wheel == wheel:
            best = 3
        table[mask] = best
    return table


_STRAIGHT_HIGH = _build_straight_table()
_POPCOUNT = [bin(mask).count("1") for mask in range(8192)]


def _top_ranks(mask, count):
    out = 0
    taken = 0
    for rank in range(12, -1, -1):
        if mask >> rank & 1:
            out = out << 4 | rank
            taken += 1
            if taken == count:
                break
    return out


def category(score):
    return score >> _CATEGORY_SHIFT


def evaluate(cards):
    counts = [0] * 13
    suit_masks = [0, 0, 0, 0]
    for card in cards:
        rank = card >> 2
        counts[rank] += 1
        suit_masks[card & 3] |= 1 << rank

    flush_mask = 0
    for mask in suit_masks:
        if _POPCOUNT[mask] >= 5:
            flush_mask = mask
            break
    if flush_mask:
        high = _STRAIGHT_HIGH[flush_mask]
        if high >= 0:
            return STRAIGHT_FLUSH << _CATEGORY_SHIFT | high

    rank_mask = suit_masks[0] | suit_masks[1] | suit_masks[2] | suit_masks[3]
    quads = -1
    trips = []
    pairs = []
    for rank in range(12, -1, -1):
        count = counts[rank]
        if count == 4:
            quads = rank
        elif count == 3:
            trips.append(rank)
        elif count == 2:
            pairs.append(rank)

    if quads >= 0:
        return QUADS << _CATEGORY_SHIFT | quads << 4 | _top_ranks(rank_mask & ~(1 << quads), 1)
    if trips and (len(trips) > 1 or pairs):
        low = max(trips[1] if len(trips) > 1 else -1, pairs[0] if pairs else -1)
        return FULL_HOUSE << _CATEGORY_SHIFT | trips[0] << 4 | low
    if flush_mask:
        return FLUSH << _CATEGORY_SHIFT | _top_ranks(flush_mask, 5)
    high = _STRAIGHT_HIGH[rank_mask]
    if high >= 0:
        return STRAIGHT << _CATEGORY_SHIFT | high
    if trips:
        return TRIPS << _CATEGORY_SHIFT | trips[0] << 8 | _top_ranks(rank_mask & ~(1 << trips[0]), 2)
    if len(pairs) >= 2:
        first, second = pairs[0], pairs[1]
        kicker = _top_ranks(rank_mask & ~(1 << first) & ~(1 << second), 1)
        return TWO_PAIR << _CATEGORY_SHIFT | first << 8 | second << 4 | kicker
    if pairs:
        return PAIR << _CATEGORY_SHIFT | pairs[0] << 12 | _top_ranks(rank_mask & ~(1 << pairs[0]), 3)
    return HIGH_CARD << _CATEGORY_SHIFT | _top_ranks(rank_mask, 5)


def straight_outs(ranks):
    """Count the ranks that would complete a straight from this rank mask."""
    outs = 0
    if _STRAIGHT_HIGH[ranks] >= 0:
        return 0
    for rank in range(13):
        if not ranks >> rank & 1 and _STRAIGHT_HIGH[ranks | 1 << rank] >= 0:
            outs += 1
    return outs
