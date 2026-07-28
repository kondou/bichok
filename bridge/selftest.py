#!/usr/bin/env python3
"""Checks the merge rules against the worked examples in the SkookumNetAutomerge article.

Run it directly:  python3 bridge/selftest.py

Nothing here touches the network. The QSOs are stand-ins carrying just the fields the merge looks
at, which is enough to exercise every branch of the algorithm.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from snet import clock as vclock                      # noqa: E402  pylint: disable=wrong-import-position
from snet import protocol                             # noqa: E402  pylint: disable=wrong-import-position
from snet import log as qsolog                        # noqa: E402  pylint: disable=wrong-import-position

FAILURES = []


def check(description, actual, expected):
    """Compare one result and remember it if it is wrong."""
    if actual == expected:
        print(f'  ok    {description}')
    else:
        print(f'  FAIL  {description}: expected {expected!r}, got {actual!r}')
        FAILURES.append(description)


class FakeQso:
    """A QSO carrying only what the merge reads."""

    def __init__(self, station, sequence, callsign='W1ABC', flags=0, vector_clock=None):
        self.stationName = station
        self.sequenceID = sequence
        self.identifier = None
        self.vectorClock = vector_clock or {}
        self.flags = flags
        self.timeStamp = '2026-07-28 00:00'
        self.mainReceiveFrequency = 14000000
        self.subReceiveFrequency = 0
        self.transmitFrequency = 14000000
        self.mode = 'CW'
        self.operatorCall = callsign
        self.notes = ''
        self.sentExchange = None
        self.receivedExchange = None


def test_clock_relationships():
    """The four ways two clocks can relate."""
    print('vector clock comparison')
    check('identical', vclock.compare({'A': 3, 'B': 2}, {'A': 3, 'B': 2}), vclock.IDENTICAL)
    check('dominates', vclock.compare({'A': 4, 'B': 2}, {'A': 3, 'B': 2}), vclock.DOMINATES)
    check('dominated', vclock.compare({'A': 3, 'B': 2}, {'A': 3, 'B': 3}), vclock.DOMINATED)
    check('concurrent', vclock.compare({'A': 5, 'B': 2}, {'A': 4, 'B': 3}), vclock.CONCURRENT)
    check('a missing station reads as zero',
          vclock.compare({'A': 3}, {'A': 3, 'B': 0}), vclock.IDENTICAL)


def test_example_one():
    """Two stations log different contacts and both converge without comparing anything."""
    print('article example 1: normal operation, no conflicts')
    alpha = qsolog.QsoLog()
    alpha.node_clock = {'A': 3, 'B': 2}

    qso_x = FakeQso('A', 4, vector_clock={'A': 4, 'B': 2})
    qso_y = FakeQso('B', 3, vector_clock={'A': 3, 'B': 3})

    check('a new QSO from us is added', alpha.merge_qso(qso_x), qsolog.ADDED)
    check("a new QSO from the other station is added", alpha.merge_qso(qso_y), qsolog.ADDED)

    alpha.observe_clock({'A': 4, 'B': 3})
    check('the node clocks merge to the element-wise maximum', alpha.node_clock, {'A': 4, 'B': 3})
    check('both QSOs are held', len(alpha.snapshot()), 2)


def test_example_three():
    """Two operators correct the same QSO differently, which is a real conflict."""
    print('article example 3: genuine conflict from concurrent edits')
    alpha = qsolog.QsoLog()

    original = FakeQso('A', 4, callsign='W1ABC', vector_clock={'A': 4, 'B': 2})
    alpha.merge_qso(original)

    ours = FakeQso('A', 4, callsign='W1ABD', vector_clock={'A': 5, 'B': 2})
    alpha.merge_qso(ours)

    theirs = FakeQso('A', 4, callsign='W1ABE', vector_clock={'A': 4, 'B': 3})
    check('neither clock dominates and the fields differ, so it is a conflict',
          alpha.merge_qso(theirs), qsolog.CONFLICTED)

    resolution = FakeQso('A', 4, callsign='W1ABD', vector_clock={'A': 6, 'B': 3})
    check('a resolution that dominates both is accepted',
          alpha.merge_qso(resolution), qsolog.UPDATED)


def test_no_action_cases():
    """Copies we already have, and copies older than ours, change nothing."""
    print('idempotence')
    alpha = qsolog.QsoLog()
    qso = FakeQso('A', 4, vector_clock={'A': 4, 'B': 2})
    alpha.merge_qso(qso)

    check('the same copy again does nothing', alpha.merge_qso(qso), None)
    check('an older copy does nothing',
          alpha.merge_qso(FakeQso('A', 4, vector_clock={'A': 3, 'B': 2})), None)

    same_content = FakeQso('A', 4, vector_clock={'A': 5, 'B': 1})
    check('concurrent clocks with identical content are not a conflict',
          alpha.merge_qso(same_content), None)
    check('and the clocks are merged instead', alpha.clocks[('A', 4)], {'A': 5, 'B': 2})


def test_tombstones():
    """A deletion is a flag on the QSO, not the removal of one."""
    print('tombstones')
    alpha = qsolog.QsoLog()
    alpha.merge_qso(FakeQso('A', 4, vector_clock={'A': 4}))
    check('the QSO counts as worked', len(alpha.snapshot()), 1)

    deleted = FakeQso('A', 4, flags=protocol.FLAG_DELETED, vector_clock={'A': 5})
    check('the tombstone reports a deletion', alpha.merge_qso(deleted), qsolog.DELETED)
    check('and it stops counting as worked', len(alpha.snapshot()), 0)
    check('but it is still held, so a later edit can find it', len(alpha.qsos), 1)

    restored = FakeQso('A', 4, vector_clock={'A': 6})
    check('un-deleting reports it as added again', alpha.merge_qso(restored), qsolog.ADDED)


def test_identity():
    """Identity is the station and sequence pair, falling back to the UUID."""
    print('QSO identity')
    check('a sequence ID pairs with the station',
          qsolog.qso_key(FakeQso('Alpha', 7)), ('Alpha', 7))

    legacy = FakeQso('Alpha', None)
    legacy.identifier = 'F274A1E0-3A23-436E-AC0B-15614C6B2FD7'
    check('without one the UUID stands in',
          qsolog.qso_key(legacy), ('uuid', 'F274A1E0-3A23-436E-AC0B-15614C6B2FD7'))

    check('two stations can share a sequence number without colliding',
          qsolog.qso_key(FakeQso('Alpha', 7)) != qsolog.qso_key(FakeQso('Bravo', 7)), True)


def test_peer_information_roundtrip():
    """Our own PeerInformation must survive being archived and read back.

    A key that cannot be decoded does not fail on its own -- it fails the whole packet, and a
    packet that never decodes never reaches the code that decides which protocol to answer in. So
    a mistake here looks like total silence on the network, which is expensive to diagnose against
    a real SkookumLogger. Catching it locally is worth the few lines.
    """
    print('PeerInformation archive round-trip')
    from Foundation import NSKeyedArchiver, NSKeyedUnarchiver, NSDate  # pylint: disable=import-outside-toplevel
    from snet.objects import PeerInformation, allowed_classes          # pylint: disable=import-outside-toplevel

    now = NSDate.date()
    info = PeerInformation.alloc().initWithStationName_('BicHoc')
    info.vectorClock = {'BicHoc': 0, 'Dismal': 7}
    info.syncEpoch = now
    info.contestName = 'CQ World Wide DX Contest CW'
    info.contestStartTime = now
    info.contestEndTime = now
    info.timestamp = now

    packet = [protocol.PEER_INFORMATION, now, info]
    data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(packet, True, None)
    check('it archives', error, None)

    decoded, error = NSKeyedUnarchiver.unarchivedObjectOfClasses_fromData_error_(allowed_classes(), data, None)
    check('it reads back', error, None)

    if decoded is None:
        return
    tag, epoch, payload = protocol.split_packet(decoded)
    check('the tag survives', tag, protocol.PEER_INFORMATION)
    check('the epoch survives', epoch is not None, True)
    check('the station name survives', str(payload.peerHostName), 'BicHoc')
    check('the clock survives', vclock.to_dict(payload.vectorClock), {'BicHoc': 0, 'Dismal': 7})
    check('the contest name survives', str(payload.contestName), 'CQ World Wide DX Contest CW')


def main():
    """Run every check and report."""
    for test in (test_clock_relationships, test_example_one, test_example_three,
                 test_no_action_cases, test_tombstones, test_identity,
                 test_peer_information_roundtrip):
        test()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} check(s) failed:')
        for description in FAILURES:
            print(f'  - {description}')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
