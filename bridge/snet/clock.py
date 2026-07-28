"""Vector clocks for the SkookumNet-2 automerge protocol.

A vector clock counts events -- creates, edits and deletions -- per station. SkookumLogger sends
them as an NSDictionary of station name to event count; to_dict() converts one to a plain dict
so the rest of this module can stay free of Foundation types.

A station missing from a clock reads as zero, which is what lets a station nobody has heard from
compare equal to one whose counter has simply not moved yet.
"""

IDENTICAL = 'identical'
DOMINATES = 'dominates'    # left knows everything right knows, and more
DOMINATED = 'dominated'    # right knows everything left knows, and more
CONCURRENT = 'concurrent'  # each side knows something the other does not


def to_dict(clock):
    """Convert an NSDictionary (or None) to a plain station -> count dict."""
    if not clock:
        return {}
    converted = {}
    for station, count in clock.items():
        try:
            converted[str(station)] = int(count)
        except (TypeError, ValueError):
            continue
    return converted


def compare(left, right):
    """Return how left relates to right: IDENTICAL, DOMINATES, DOMINATED or CONCURRENT."""
    left_ahead = False
    right_ahead = False

    for station in set(left) | set(right):
        lcount = left.get(station, 0)
        rcount = right.get(station, 0)
        if lcount > rcount:
            left_ahead = True
        elif lcount < rcount:
            right_ahead = True

    if left_ahead and right_ahead:
        return CONCURRENT
    if left_ahead:
        return DOMINATES
    if right_ahead:
        return DOMINATED
    return IDENTICAL


def merge(left, right):
    """Return the element-wise maximum of two clocks."""
    merged = dict(left)
    for station, count in right.items():
        if count > merged.get(station, 0):
            merged[station] = count
    return merged
