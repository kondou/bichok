#!/usr/bin/env python3
"""Checks the merge rules against the worked examples in the SkookumNetAutomerge article, and the
transport against the wire protocol.

Run it directly:  python3 bridge/selftest.py

Nothing here needs SkookumLogger, Bonjour or a network: the transport checks talk to themselves
over the loopback interface. The QSOs are stand-ins carrying just the fields the merge looks at,
which is enough to exercise every branch of the algorithm.
"""
import json
import logging
import os
import plistlib
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from snet import clock as vclock                      # noqa: E402  pylint: disable=wrong-import-position
from snet import protocol                             # noqa: E402  pylint: disable=wrong-import-position
from snet import log as qsolog                        # noqa: E402  pylint: disable=wrong-import-position
from snet import webbridge                            # noqa: E402  pylint: disable=wrong-import-position
from snet import transport as sntransport            # noqa: E402  pylint: disable=wrong-import-position

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


def test_in_place_rewrite():
    """The owner may rewrite a QSO without counting an event; the resend is its stored truth.

    Field observation (ContestPan, 2026-08-04, three-way check of wire dump, mirror log and the
    peer's sqlite): the country annotation lands in notes after the initial send, with no clock
    advance and no immediate resend. The annotated copy arrives later as the answer to a lagging
    clock, carrying the same per-QSO clock as the first send. Ignoring it freezes the mirror at
    the un-annotated shape and the advertised log hashes disagree from then on.
    """
    print('in-place rewrite by the owner')
    alpha = qsolog.QsoLog()
    first = FakeQso('A', 4, vector_clock={'A': 4})
    first.notes = ''
    alpha.merge_qso(first)

    annotated = FakeQso('A', 4, vector_clock={'A': 4})
    annotated.notes = 'UA9 Asiatic Russia'
    check('a same-clock resend with new content replaces our copy',
          alpha.merge_qso(annotated), qsolog.UPDATED)
    check('and the annotation is what we now hold',
          alpha.qsos[('A', 4)].notes, 'UA9 Asiatic Russia')

    check('resending the annotated copy again does nothing',
          alpha.merge_qso(annotated), None)

    stale = FakeQso('A', 4, vector_clock={'A': 3})
    stale.notes = ''
    check('an older arrival still cannot undo it', alpha.merge_qso(stale), None)


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
    info = PeerInformation.alloc().initWithStationName_('BicHok')
    info.vectorClock = {'BicHok': 0, 'Dismal': 7}
    info.syncEpoch = now
    info.contestName = 'CQ World Wide DX Contest CW'
    info.contestStartTime = now
    info.contestEndTime = now
    info.timestamp = now

    # A hash with the top bit set: a signed 64-bit encode rejects this, and a real log hash
    # lands here often enough that the first deleted QSO in live testing found the bug.
    info.hashOfQSOs = 0x8000000000000001

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
    check('the station name survives', str(payload.peerHostName), 'BicHok')
    check('the clock survives', vclock.to_dict(payload.vectorClock), {'BicHok': 0, 'Dismal': 7})
    check('the contest name survives', str(payload.contestName), 'CQ World Wide DX Contest CW')
    check('a top-bit hash survives', payload.hashOfQSOs, 0x8000000000000001)


def test_log_hash():
    """The whole-log hash: empty is 1, deterministic, and order-dependent."""
    print('log hash')

    class Hashable:
        """Stands in for a QSO whose per-QSO hash is already known."""
        def __init__(self, value):
            self.value = value
            self.flags = 0
        def qso_hash(self):
            return self.value

    def hash_of(values):
        log = qsolog.QsoLog()
        for index, value in enumerate(values):
            log.qsos[index] = Hashable(value)
        return log.log_hash()

    check('an empty log hashes to 1', hash_of([]), 1)
    check('the same QSOs give the same hash', hash_of([7, 11]), hash_of([7, 11]))
    check('order changes the hash', hash_of([7, 11]) != hash_of([11, 7]), True)

    tomb = Hashable(99)
    tomb.flags = protocol.FLAG_DELETED
    log = qsolog.QsoLog()
    log.qsos[0] = Hashable(7)
    log.qsos[1] = tomb
    check('tombstones can be left out of the hash',
          log.log_hash(include_deleted=False), hash_of([7]))


def test_qso_hash():
    """The per-QSO hash over real Foundation objects: deterministic, and sensitive to content."""
    print('QSO hash')
    from Foundation import NSDate, NSUUID, NSNumber, NSDictionary  # pylint: disable=import-outside-toplevel
    from snet.objects import TransientQso, Exchange, EXCHANGE_FIELDS  # pylint: disable=import-outside-toplevel

    def exchange(call):
        made = Exchange.alloc().init()
        for name in EXCHANGE_FIELDS:
            setattr(made, name, '')
        made.call = call
        return made

    def qso(callsign):
        made = TransientQso.alloc().init()
        made.identifier = NSUUID.alloc().initWithUUIDString_('F274A1E0-3A23-436E-AC0B-15614C6B2FD7')
        made.timeStamp = NSDate.dateWithTimeIntervalSince1970_(806917764.0)
        made.mainReceiveFrequency = NSNumber.numberWithLongLong_(14002000)
        made.subReceiveFrequency = NSNumber.numberWithLongLong_(0)
        made.transmitFrequency = NSNumber.numberWithLongLong_(14002000)
        made.mode = 'CW'
        made.sentExchange = exchange('JH5GHM')
        made.receivedExchange = exchange(callsign)
        made.operatorCall = 'JH5GHM'
        made.stationName = 'Dismal'
        made.notes = ''
        made.flags = NSNumber.numberWithLongLong_(1)
        made.sequenceID = NSNumber.numberWithLongLong_(1)
        made.vectorClock = NSDictionary.dictionaryWithDictionary_({'Dismal': 1})
        made.conflictInfo = None
        made.lastModifiedBy = 'Dismal'
        return made

    check('the same QSO hashes the same', qso('W3KX').qso_hash(), qso('W3KX').qso_hash())
    check('different content hashes differently',
          qso('W3KX').qso_hash() != qso('DJ5AN').qso_hash(), True)
    check('the hash fits in 64 bits', qso('W3KX').qso_hash() < 2 ** 64, True)


class RecordingTransport:
    """Stands in for the transport, remembering what was sent through it."""

    def __init__(self, peers):
        self.connected = list(peers)
        self.sent = []

    def peers(self):
        """The stations that count as connected."""
        return list(self.connected)

    def send(self, data):
        """Record a packet instead of sending it."""
        self.sent.append(data)


class RecordingListener:
    """Counts the callbacks the session makes."""

    def __init__(self):
        self.cleared = 0
        self.started = []
        self.ended = 0

    def log_cleared(self):
        self.cleared += 1

    def session_started(self, contest):
        self.started.append(contest)

    def session_ended(self):
        self.ended += 1

    def peer_information(self, info):
        pass

    def qso_added(self, qso):
        pass

    def qso_updated(self, qso):
        pass

    def qso_deleted(self, qso):
        pass

    def qso_conflicted(self, qso):
        pass


def build_peer(connected):
    """Return a SkookumNetPeer wired to recording stand-ins, plus those stand-ins.

    The transport records what gets sent instead of sending it, and the listener counts the
    callbacks the epoch rules make. Nothing touches the network.
    """
    from snet.session import SkookumNetPeer  # pylint: disable=import-outside-toplevel

    listener = RecordingListener()
    peer = SkookumNetPeer.alloc().initWithStation_listener_('BicHok', listener)
    peer.transport = RecordingTransport(connected)
    return peer, peer.transport, listener


def archived(packet):
    """Archive a packet the way a peer would send it."""
    from Foundation import NSKeyedArchiver  # pylint: disable=import-outside-toplevel
    from snet.objects import archivable  # pylint: disable=import-outside-toplevel

    data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(
        archivable(packet), True, None)
    assert error is None, error
    return bytes(data)


def test_owner_epoch_rules():
    """The epoch follows the log's owner, in both directions; others only ever move it forward.

    Field observation (ContestPan, 2026-08-05): replacing the log file on SkookumLogger rolls its
    epoch back, and a passive peer that keeps naming the newest epoch it ever saw reads to
    SkookumLogger as a request to reset the log -- confirming that dialog erases the log for real.
    """
    print('owner epoch rules')
    from Foundation import NSDate  # pylint: disable=import-outside-toplevel

    e1 = NSDate.dateWithTimeIntervalSince1970_(1000.0)
    e2 = NSDate.dateWithTimeIntervalSince1970_(2000.0)
    e3 = NSDate.dateWithTimeIntervalSince1970_(3000.0)
    peer, _, listener = build_peer(['fake'])

    check('with no epoch and no owner, anyone is adopted',
          peer._epoch_allows(e1, 'CP'), True)  # pylint: disable=protected-access
    check('and the epoch is theirs', peer.epoch is e1, True)
    check('with no owner, a newer epoch is adopted from anyone',
          peer._epoch_allows(e2, 'CP'), True)  # pylint: disable=protected-access
    check('but an older one is stale',
          peer._epoch_allows(e1, 'CP'), False)  # pylint: disable=protected-access
    check('and does not move the epoch', peer.epoch is e2, True)

    peer._learn_owner('SL')  # pylint: disable=protected-access
    peer.log.qsos[('K1AB', 1)] = FakeQso('K1AB', 1)
    check('an old stamp on the owner\'s data packet is a straggler and is dropped',
          peer._epoch_allows(e1, 'SL'), False)  # pylint: disable=protected-access
    check('without moving the epoch', peer.epoch is e2, True)
    check('or touching the log', len(peer.log.qsos), 1)
    cleared = listener.cleared
    check('the owner\'s advertisement rolls the epoch back',
          peer._epoch_allows(e1, 'SL', advertised=True), True)  # pylint: disable=protected-access
    check('to its value', peer.epoch is e1, True)
    check('and the log of the replaced generation is dropped', len(peer.log.qsos), 0)
    check('the listener saw it dropped', listener.cleared, cleared + 1)

    check('a newer epoch from a non-owner is an echo and is dropped',
          peer._epoch_allows(e3, 'CP'), False)  # pylint: disable=protected-access
    check('and adopts nothing', peer.epoch is e1, True)
    check('the same epoch passes from anyone',
          peer._epoch_allows(e1, 'CP'), True)  # pylint: disable=protected-access

    peer.epoch = None
    check('with no epoch but a known owner, a non-owner is refused',
          peer._epoch_allows(e3, 'CP'), False)  # pylint: disable=protected-access
    check('and we still hold none', peer.epoch is None, True)
    check('while the owner is followed',
          peer._epoch_allows(e3, 'SL'), True)  # pylint: disable=protected-access
    check('to its epoch', peer.epoch is e3, True)


def test_owner_identification():
    """A sync packet of any content marks its sender as the owner; only a logger sends one.

    Counting the empty ones is deliberate and agreed with ContestPan: a freshly reset
    SkookumLogger sends QSO-less SyncQso packets, and those let it establish itself as owner
    before it has a single QSO to send.
    """
    print('owner identification')
    from snet.objects import PeerInformation  # pylint: disable=import-outside-toplevel

    peer, _, _ = build_peer(['fake'])
    qso = FakeQso('K1AB', 1)

    check('a sync dictionary with QSOs marks the owner',
          peer._marks_owner(protocol.SYNC_QSO, {'qsos': [qso], 'vc': {}}), True)  # pylint: disable=protected-access
    check('so does a clock-only one',
          peer._marks_owner(protocol.SYNC_QSO, {'vc': {'SL': 5}}), True)  # pylint: disable=protected-access
    check('and an empty one',
          peer._marks_owner(protocol.SYNC_QSO, {}), True)  # pylint: disable=protected-access
    check('a PeerInformation does not, passive peers send those too',
          peer._marks_owner(protocol.PEER_INFORMATION,  # pylint: disable=protected-access
                            PeerInformation.alloc().initWithStationName_('CP')), False)
    check('nor does a serial number dictionary',
          peer._marks_owner(protocol.SERIAL_NUMBER, {'qsos': [qso]}), False)  # pylint: disable=protected-access

    peer._learn_owner('SL')  # pylint: disable=protected-access
    check('the owner is remembered', peer.owner, 'SL')


def test_owner_going_epochless():
    """When the owner stops advertising an epoch, ours goes too, and so does the old log.

    Field observation (ContestPan): a brand-new log has no reset in its history, so its
    SkookumLogger advertises no epoch at all -- and cannot treat a peer that still names one as a
    sync partner. Only the payload's own syncEpoch says this; the NSNull in the packet frame is
    the normal shape of "not set".
    """
    print('the owner going epoch-less')
    from Foundation import NSDate  # pylint: disable=import-outside-toplevel
    from snet.objects import PeerInformation  # pylint: disable=import-outside-toplevel

    def advertisement(name):
        info = PeerInformation.alloc().initWithStationName_(name)
        info.hashOfQSOs = None
        return info

    epoch = NSDate.dateWithTimeIntervalSince1970_(1000.0)
    peer, _, listener = build_peer(['fake'])
    peer.owner = 'SL'
    peer.epoch = epoch
    peer.log.qsos[('K1AB', 1)] = FakeQso('K1AB', 1)

    peer._handle_peer_information(advertisement('CP'), 'CP')  # pylint: disable=protected-access
    check('a non-owner without an epoch changes nothing', peer.epoch is epoch, True)
    check('and the log is kept', len(peer.log.qsos), 1)

    cleared = listener.cleared
    peer._handle_peer_information(advertisement('SL'), 'SL')  # pylint: disable=protected-access
    check('the owner without an epoch clears ours', peer.epoch is None, True)
    check('and the old log with it', len(peer.log.qsos), 0)
    check('and the listener heard', listener.cleared, cleared + 1)


def test_reply_announce():
    """Announcements travel as replies: always to the owner, to anyone while no owner is known,
    never to another passive peer once one is -- and never twice within the floor."""
    print('announcements as replies')
    from snet.objects import PeerInformation  # pylint: disable=import-outside-toplevel

    def advertisement(name):
        info = PeerInformation.alloc().initWithStationName_(name)
        info.hashOfQSOs = None
        return info

    peer, session, _ = build_peer(['fake'])

    peer._handle_peer_information(advertisement('CP'), 'CP')  # pylint: disable=protected-access
    check('with no owner known, anyone is answered', len(session.sent), 1)

    peer._handle_peer_information(advertisement('CP'), 'CP')  # pylint: disable=protected-access
    check('but not twice within the floor', len(session.sent), 1)

    peer.owner = 'SL'
    peer.last_announce = None
    peer._handle_peer_information(advertisement('CP'), 'CP')  # pylint: disable=protected-access
    check('once the owner is known, a passive peer is not answered', len(session.sent), 1)

    peer._handle_peer_information(advertisement('SL'), 'SL')  # pylint: disable=protected-access
    check('while the owner always is', len(session.sent), 2)


def test_duplicate_fill():
    """The same fill arriving twice changes nothing the second time.

    Field observation (ContestPan): identical fills can arrive 0.2 to 9 seconds apart.
    """
    print('duplicate fills')
    log = qsolog.QsoLog()
    check('the first copy lands',
          log.merge_qso(FakeQso('K1AB', 1, vector_clock={'K1AB': 1})), qsolog.ADDED)
    check('the second is silently ignored',
          log.merge_qso(FakeQso('K1AB', 1, vector_clock={'K1AB': 1})), None)
    check('and the log holds one QSO', len(log.qsos), 1)


def test_null_epoch_packet():
    """A packet with NSNull in the epoch slot survives the secure unarchiver.

    NSNull is what an epoch-less peer -- a freshly reset SkookumLogger -- puts there, and one
    unlisted class makes the unarchiver reject the whole packet. ContestPan hit this from its
    own side first (2026-07-29), and its crosscheck of spec 2.2 caught the class missing from
    this client's allowed set too.
    """
    print('the NSNull epoch slot')
    from Foundation import NSNull, NSKeyedArchiver, NSKeyedUnarchiver  # pylint: disable=import-outside-toplevel
    from snet.objects import allowed_classes  # pylint: disable=import-outside-toplevel

    packet = [protocol.SYNC_QSO, NSNull.null(), {'vc': {'SL': 1}}]
    data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(
        packet, True, None)
    check('the packet archives', error is None, True)
    decoded, error = NSKeyedUnarchiver.unarchivedObjectOfClasses_fromData_error_(
        allowed_classes(), data, None)
    check('and decodes with the allowed classes', error is None, True)
    tag, epoch, payload = protocol.split_packet(decoded)
    check('the tag survives', tag, protocol.SYNC_QSO)
    check('the NSNull reads as no epoch', epoch is None, True)
    check('and the clock is intact', vclock.to_dict(payload['vc']) if payload else None, {'SL': 1})


def test_packet_shape():
    """A packet is [tag, epoch, payload]; NSNull stands for no epoch, and anything else is malformed."""
    print('packet shape')
    from Foundation import NSDate, NSNull  # pylint: disable=import-outside-toplevel

    now = NSDate.date()
    check('a dated packet splits', protocol.split_packet([6, now, 'x']), (6, now, 'x'))
    check('NSNull reads as no epoch', protocol.split_packet([1, NSNull.null(), 'x']), (1, None, 'x'))
    check('two elements are malformed', protocol.split_packet([1, now]), None)
    check('an epoch that is not a date is malformed', protocol.split_packet([1, 'soon', 'x']), None)
    check('a tag that is not a number is malformed', protocol.split_packet(['1', now, 'x']), None)
    check('nothing at all is malformed', protocol.split_packet(None), None)


def test_framing():
    """Frames survive being split and joined in transit, and a bad length is caught."""
    print('framing')
    hello = sntransport.frame(protocol.FRAME_HELLO, b'{}')
    check('the length counts the kind byte and the payload', hello[:4], struct.pack('>I', 3))
    check('the kind follows the length', hello[4], protocol.FRAME_HELLO)

    data = sntransport.frame(protocol.FRAME_DATA, b'packet')
    buffer = bytearray(hello + data[:5])
    frames, intact = sntransport.unframe(buffer)
    check('a whole frame comes off the front', frames, [(protocol.FRAME_HELLO, b'{}')])
    check('and the stream is still sound', intact, True)
    check('a partial frame waits for the rest', bytes(buffer), data[:5])
    buffer.extend(data[5:])
    frames, _ = sntransport.unframe(buffer)
    check('and comes off once complete', frames, [(protocol.FRAME_DATA, b'packet')])
    check('leaving nothing behind', len(buffer), 0)

    buffer = bytearray(data + struct.pack('>I', 0) + b'\x01')
    frames, intact = sntransport.unframe(buffer)
    check('a zero length is malformed', intact, False)
    check('but the frame before it is kept', frames, [(protocol.FRAME_DATA, b'packet')])
    buffer = bytearray(struct.pack('>I', protocol.FRAME_LIMIT + 1) + b'\x01')
    check('a length over 64 MB is malformed', sntransport.unframe(buffer)[1], False)
    buffer = bytearray(struct.pack('>I', protocol.FRAME_LIMIT))
    check('64 MB itself is allowed, and waited for', sntransport.unframe(buffer), ([], True))


def test_hello():
    """A hello names a valid UUID and a non-empty station name, or the connection is closed."""
    print('hello')
    good = '6f1c3b2e-0000-4000-8000-000000000001'

    def hello(**fields):
        return json.dumps(fields).encode('utf-8')

    check('a good hello reads, its UUID uppercased',
          sntransport.read_hello(hello(version=1, uuid=good, name='Phobos')), (good.upper(), 'Phobos'))
    check('the version is not checked',
          sntransport.read_hello(hello(version=99, uuid=good, name='Phobos')) is not None, True)
    check('no name is malformed', sntransport.read_hello(hello(version=1, uuid=good)), None)
    check('an empty name is malformed', sntransport.read_hello(hello(version=1, uuid=good, name='')), None)
    check('a bad UUID is malformed', sntransport.read_hello(hello(version=1, uuid='nope', name='Phobos')), None)
    check('a missing UUID is malformed', sntransport.read_hello(hello(version=1, name='Phobos')), None)
    check('a JSON array is malformed', sntransport.read_hello(b'[1, 2]'), None)
    check('text that is not JSON is malformed', sntransport.read_hello(b'hello'), None)
    check('bytes that are not UTF-8 are malformed', sntransport.read_hello(b'\xff\xfe'), None)


def test_who_connects():
    """Of each pair, the station whose name sorts first opens the connection."""
    print('who connects')
    check('Macbook-Air connects to Phobos', sntransport.opens_connection('Macbook-Air', 'Phobos'), True)
    check('and to Trinity', sntransport.opens_connection('Macbook-Air', 'Trinity'), True)
    check('Phobos connects to Trinity', sntransport.opens_connection('Phobos', 'Trinity'), True)
    check('Trinity waits for Phobos', sntransport.opens_connection('Trinity', 'Phobos'), False)
    check('BicHok connects to Dismal', sntransport.opens_connection('BicHok', 'Dismal'), True)


def test_duplicate_choice():
    """When two connections to the same peer exist, both stations keep the same one."""
    print('duplicate connections')

    class Link:
        """A connection as each station sees it: which way it runs from that station's side."""
        def __init__(self, name, outgoing):
            self.name = name
            self.outgoing = outgoing

    big, small = 'F0000000-0000-4000-8000-000000000000', '10000000-0000-4000-8000-000000000000'
    # X was opened by the station with the larger UUID, Y by the one with the smaller
    at_big = {'X': Link('X', True), 'Y': Link('Y', False)}
    at_small = {'X': Link('X', False), 'Y': Link('Y', True)}

    def kept(links, first, second, ours, theirs):
        return sntransport.keeper(links[first], links[second], ours, theirs).name

    check('the larger UUID gives up its outgoing connection',
          kept(at_big, 'X', 'Y', big, small), 'Y')
    check('whichever hello arrives first', kept(at_big, 'Y', 'X', big, small), 'Y')
    check('the smaller UUID gives up its incoming one, so it keeps the same',
          kept(at_small, 'X', 'Y', small, big), 'Y')
    check('again whichever arrives first', kept(at_small, 'Y', 'X', small, big), 'Y')
    check('UUIDs compare as uppercase strings',
          kept(at_big, 'X', 'Y', big.lower(), small), 'Y')
    check('two connections the same way mean a reconnect, so the newer one stays',
          sntransport.keeper(Link('old', True), Link('new', True), big, small).name, 'new')


def test_serial_number_ignored():
    """A serial number packet is a dictionary like a sync packet, and must not be taken for one."""
    print('serial number packets')
    from Foundation import NSDate  # pylint: disable=import-outside-toplevel

    epoch = NSDate.dateWithTimeIntervalSince1970_(1000.0)
    peer, _, listener = build_peer(['SL'])
    peer.receive_({'peer': 'SL', 'data': archived([protocol.SERIAL_NUMBER, epoch, {'op': 'next'}])})
    check('it does not make its sender the owner', peer.owner, None)
    check('nor move the epoch', peer.epoch, None)
    check('nor clear the log', listener.cleared, 0)

    peer.receive_({'peer': 'SL', 'data': archived([protocol.SYNC_QSO, epoch, {'vc': {'SL': 1}}])})
    check('while a sync packet does make its sender the owner', peer.owner, 'SL')
    check('and brings its epoch', peer.epoch is not None and peer.epoch.compare_(epoch) == 0, True)


def test_epochless_packets():
    """Without an epoch, only PeerInformation is acted on, as on SkookumLogger's side."""
    print('packets without an epoch')
    from Foundation import NSNull  # pylint: disable=import-outside-toplevel

    peer, _, listener = build_peer(['SL'])
    clock = {'qsos': [], 'vc': {'SL': 1}}
    peer.receive_({'peer': 'SL', 'data': archived([protocol.SYNC_QSO, NSNull.null(), clock])})
    check('an epoch-less sync packet names no owner', peer.owner, None)
    check('and its clock is not taken in', peer.log.node_clock, {})
    check('nor is anything cleared', listener.cleared, 0)


def test_conflict_info_decodes():
    """A conflicting QSO's conflictInfo carries dates, arrays, NSNull and formatters, and none of
    them may cost us the packet -- one unlisted class used to reject a whole fill."""
    print('conflictInfo contents')
    from Foundation import NSDate, NSKeyedUnarchiver, NSNull, NSNumber, NSUUID  # pylint: disable=import-outside-toplevel
    from snet import objects  # pylint: disable=import-outside-toplevel

    def exchange(call):
        made = objects.Exchange.alloc().init()
        for name in objects.EXCHANGE_FIELDS:
            setattr(made, name, '')
        made.call = call
        return made

    def qso(conflict):
        made = objects.TransientQso.alloc().init()
        made.identifier = NSUUID.UUID()
        made.timeStamp = NSDate.date()
        made.mainReceiveFrequency = 14025000
        made.subReceiveFrequency = 0
        made.transmitFrequency = 14025000
        made.mode = 'CW'
        made.sentExchange = exchange('JH5GHM')
        made.receivedExchange = exchange('W3KX')
        made.operatorCall = 'JH5GHM'
        made.stationName = 'SL'
        made.notes = ''
        made.flags = 0
        made.sequenceID = 1
        made.vectorClock = objects.archivable({'SL': 1})
        made.conflictInfo = objects.archivable(conflict)
        made.lastModifiedBy = 'SL'
        return made

    epoch = NSDate.date()
    for label, conflict in (('a date', {'timeStamp': NSDate.date()}),
                            ('an array', {'mode': ['CW', 'SSB']}),
                            ('NSNull', {'notes': NSNull.null()}),
                            ('a number', {'flags': NSNumber.numberWithInt_(5)}),
                            ('every formatter', {name: cls.alloc().init() for name, cls in (
                                ('operatorCall', objects.OperatorsFormatter),
                                ('flags', objects.BooleanFormatter),
                                ('mainReceiveFrequency', objects.FrequencyFormatter),
                                ('serial', objects.SerialInfoFormatter),
                                ('timeStamp', objects.TimestampFormatter))})):
        data = archived([protocol.SYNC_QSO, epoch, {'qsos': [qso(conflict), qso(None)]}])
        packet, error = NSKeyedUnarchiver.unarchivedObjectOfClasses_fromData_error_(
            objects.allowed_classes(), data, None)
        decoded = protocol.split_packet(packet) if error is None else None
        qsos = decoded[2]['qsos'] if decoded else []
        check(f'a fill whose conflictInfo holds {label} decodes whole', len(qsos), 2)
        if qsos:
            check(f'and the QSO with {label} still hashes', qsos[0].qso_hash() > 0, True)


def test_sent_archive_is_foundation():
    """What we send carries the Foundation classes SkookumLogger sends, not PyObjC's wrappers."""
    print('the classes we send')
    from Foundation import NSDate  # pylint: disable=import-outside-toplevel

    peer, transport, _ = build_peer(['SL'])
    peer.epoch = NSDate.date()
    peer.contest = 'CQ World Wide DX Contest CW'
    peer.known_stations = {'SL'}
    peer.announce_(None)
    check('one packet went out', len(transport.sent), 1)
    if transport.sent:
        archive = plistlib.loads(transport.sent[0])
        names = {name for entry in archive['$objects'] if isinstance(entry, dict)
                 for name in entry.get('$classes', [])}
        check('no PyObjC wrapper class appears in it',
              sorted(name for name in names if name.startswith('OC_')), [])
        check('the packet itself is an array', 'NSArray' in names, True)


def test_session_start():
    """The session starts on the first PeerInformation, which is where the contest name now comes
    from, starts again only when that name changes or after everyone left, and copies what it
    echoes from the owner alone once there is one."""
    print('session start')
    from snet.objects import PeerInformation  # pylint: disable=import-outside-toplevel

    def advertisement(name, contest, version='SL-6.1-16'):
        info = PeerInformation.alloc().initWithStationName_(name)
        info.hashOfQSOs = None
        info.contestName = contest
        info.binaryVersion = version
        return info

    peer, transport, listener = build_peer(['SL'])
    peer.peerConnected_({'peer': 'SL'})
    check('a connection alone starts nothing', listener.started, [])
    peer._handle_peer_information(advertisement('SL', 'CQ WW CW'), 'SL')  # pylint: disable=protected-access
    check('the first PeerInformation starts the session with its contest', listener.started, ['CQ WW CW'])
    peer._handle_peer_information(advertisement('SL', 'CQ WW CW'), 'SL')  # pylint: disable=protected-access
    check('the next one does not start it again', listener.started, ['CQ WW CW'])

    peer.owner = 'SL'
    peer._handle_peer_information(advertisement('CP', 'Other', 'BicHok-1.0'), 'CP')  # pylint: disable=protected-access
    check('a passive peer does not change the contest we echo', peer.contest, 'CQ WW CW')
    check('nor the build', peer.peer_version, 'SL-6.1-16')

    peer._handle_peer_information(advertisement('SL', 'CQ WW SSB'), 'SL')  # pylint: disable=protected-access
    check('the owner moving to another contest starts the session afresh',
          listener.started, ['CQ WW CW', 'CQ WW SSB'])

    transport.connected = []
    peer.peerDisconnected_({'peer': 'SL'})
    check('the last peer leaving ends the session', listener.ended, 1)
    transport.connected = ['SL']
    peer._handle_peer_information(advertisement('SL', 'CQ WW SSB'), 'SL')  # pylint: disable=protected-access
    check('and its return starts it again', listener.started[-1], 'CQ WW SSB')
    check('three starts in all', len(listener.started), 3)


def test_restart_replays():
    """A session that starts again resends the held QSOs right behind the startup message,
    because the page drops its live QSOs on hearing it."""
    print('replay on a new session')
    bridge = webbridge.WebBridge()
    sent = []
    bridge._broadcast_all = sent.extend  # pylint: disable=protected-access
    bridge.cache = {'a': {'id': 'a'}, 'b': {'id': 'b'}}
    bridge.session_started('CQ WW CW')
    check('startup goes first', sent[0], {'type': 'startup', 'contest': 'CQ WW CW'})
    check('then every held QSO', [message['qso']['id'] for message in sent[1:]], ['a', 'b'])
    check('as additions', {message['type'] for message in sent[1:]}, {'addQSO'})


class _Events:
    """Records a transport's delegate callbacks, which arrive on the transport's thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []

    def transport_connected(self, name):
        with self.lock:
            self.items.append(('connected', name))

    def transport_disconnected(self, name):
        with self.lock:
            self.items.append(('disconnected', name))

    def transport_received(self, name, data):
        with self.lock:
            self.items.append(('data', name, bytes(data)))

    def snapshot(self):
        with self.lock:
            return list(self.items)


def _wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def _live_socket(transport):
    """The socket of the one connection that counts, for checking both ends agree on it."""
    connections = list(transport._live.values())  # pylint: disable=protected-access
    return connections[0].sock if len(connections) == 1 else None


def test_transport_loopback():
    """Two transports find each other's listeners, say hello, and carry packets -- over loopback,
    without Bonjour."""
    print('transport over loopback')
    alpha_events, bravo_events = _Events(), _Events()
    alpha = sntransport.Transport('Alpha', alpha_events, discover=False)
    bravo = sntransport.Transport('Bravo', bravo_events, discover=False)
    alpha.start()
    bravo.start()
    try:
        bravo.add_service(alpha.uuid, 'Alpha', '127.0.0.1', alpha.port)  # Bravo sorts second and waits
        alpha.add_service(bravo.uuid, 'Bravo', '127.0.0.1', bravo.port)  # Alpha sorts first and connects
        check('both ends count the connection',
              _wait_for(lambda: alpha.peers() == ['Bravo'] and bravo.peers() == ['Alpha']), True)
        check('each reported its peer once',
              (alpha_events.snapshot(), bravo_events.snapshot()),
              ([('connected', 'Bravo')], [('connected', 'Alpha')]))

        alpha.send(b'from alpha')
        bravo.send(b'from bravo')
        check('a packet arrives as it was sent',
              _wait_for(lambda: ('data', 'Alpha', b'from alpha') in bravo_events.snapshot()), True)
        check('in both directions',
              _wait_for(lambda: ('data', 'Bravo', b'from bravo') in alpha_events.snapshot()), True)

        bravo.stop()
        check('a peer that leaves is reported gone',
              _wait_for(lambda: ('disconnected', 'Bravo') in alpha_events.snapshot()), True)
        check('and no longer counts', alpha.peers(), [])
    finally:
        alpha.stop()
        bravo.stop()


def test_transport_duplicates():
    """Two stations that dial each other at once end up on one and the same connection."""
    print('transport duplicates over loopback')
    charlie_events, delta_events = _Events(), _Events()
    charlie = sntransport.Transport('Charlie', charlie_events, discover=False)
    delta = sntransport.Transport('Delta', delta_events, discover=False)
    charlie.start()
    delta.start()
    try:
        # Both dial at once, as the fallback can make happen
        to_delta = sntransport._Service(delta.uuid, 'Delta', '127.0.0.1', delta.port)  # pylint: disable=protected-access
        to_charlie = sntransport._Service(charlie.uuid, 'Charlie', '127.0.0.1', charlie.port)  # pylint: disable=protected-access
        charlie._post(lambda: charlie._dial(to_delta))  # pylint: disable=protected-access
        delta._post(lambda: delta._dial(to_charlie))  # pylint: disable=protected-access
        check('both ends count one peer',
              _wait_for(lambda: charlie.peers() == ['Delta'] and delta.peers() == ['Charlie']), True)
        time.sleep(protocol.DUPLICATE_LINGER + 0.5)
        ours, theirs = _live_socket(charlie), _live_socket(delta)
        # Ports, not whole addresses: the listener takes IPv4 as IPv4-mapped IPv6
        check('and both kept the same connection',
              ours is not None and theirs is not None
              and (ours.getsockname()[1], ours.getpeername()[1]) == (theirs.getpeername()[1], theirs.getsockname()[1]),
              True)
        check('the loser closed without the peer ever reading as gone',
              [event for event in charlie_events.snapshot() + delta_events.snapshot()
               if event[0] == 'disconnected'], [])
        charlie.send(b'still here')
        check('and packets still flow',
              _wait_for(lambda: ('data', 'Charlie', b'still here') in delta_events.snapshot()), True)
    finally:
        charlie.stop()
        delta.stop()


def test_transport_guards():
    """A raw socket that breaks the rules is turned away."""
    print('transport guards')
    events = _Events()
    echo = sntransport.Transport('Echo', events, discover=False, max_peers=1)
    echo.start()

    def dial():
        sock = socket.create_connection(('127.0.0.1', echo.port), timeout=5)
        sock.settimeout(5)
        return sock

    def hello(sock, peer_uuid, name):
        payload = json.dumps({'version': 1, 'uuid': peer_uuid, 'name': name}).encode('utf-8')
        sock.sendall(sntransport.frame(protocol.FRAME_HELLO, payload))

    def closed_by_echo(sock):
        """Read past Echo's own hello until the connection ends; True when Echo closed it."""
        try:
            while True:
                if not sock.recv(65536):
                    return True
        except socket.timeout:
            return False
        except OSError:
            return True

    try:
        sock = dial()
        sock.sendall(struct.pack('>I', 0) + b'\x01')
        check('a zero-length frame closes the connection', closed_by_echo(sock), True)
        sock.close()

        sock = dial()
        sock.sendall(sntransport.frame(protocol.FRAME_HELLO, b'not json'))
        check('a malformed hello closes it', closed_by_echo(sock), True)
        sock.close()

        sock = dial()
        hello(sock, echo.uuid, 'Echo')
        check('a hello carrying our own UUID closes it', closed_by_echo(sock), True)
        sock.close()

        first = dial()
        first.sendall(sntransport.frame(protocol.FRAME_DATA, b'too early'))
        hello(first, '20000000-0000-4000-8000-000000000001', 'Foxtrot')
        check('a good hello connects', _wait_for(lambda: echo.peers() == ['Foxtrot']), True)
        check('and data sent before it was dropped',
              [event for event in events.snapshot() if event[0] == 'data'], [])
        first.sendall(sntransport.frame(9, b'from the future'))
        first.sendall(sntransport.frame(protocol.FRAME_DATA, b'on time'))
        check('an unknown frame kind is skipped, and what follows arrives',
              _wait_for(lambda: ('data', 'Foxtrot', b'on time') in events.snapshot()), True)

        second = dial()
        hello(second, '30000000-0000-4000-8000-000000000001', 'Golf')
        check('past the peer limit a new connection is closed once its hello arrives',
              closed_by_echo(second), True)
        second.close()
        check('leaving the first in place', echo.peers(), ['Foxtrot'])
        first.close()
    finally:
        echo.stop()


def test_transport_unreachable():
    """A station advertised where nothing listens is given up on cleanly. The failure is handed
    back to the transport's thread after the connecting thread's except clause has ended, which
    is exactly where a carelessly captured exception name has already gone."""
    print('transport to an unreachable station')
    records = []

    class Capture(logging.Handler):
        """Keeps what the transport logs."""
        def emit(self, record):
            records.append(record.getMessage())

    capture = Capture()
    root = logging.getLogger()
    level = root.level
    root.addHandler(capture)
    root.setLevel(logging.INFO)
    events = _Events()
    hotel = sntransport.Transport('Hotel', events, discover=False)
    hotel.start()
    try:
        spare = socket.socket()
        spare.bind(('127.0.0.1', 0))
        closed_port = spare.getsockname()[1]
        spare.close()  # nothing listens there now
        hotel.add_service('40000000-0000-4000-8000-000000000001', 'India', '127.0.0.1', closed_port)
        check('the failed attempt is reported',
              _wait_for(lambda: any('could not connect to India' in record for record in records)), True)
        check('without an unexpected error',
              [record for record in records if 'unexpected error' in record], [])
        check('and nothing is left waiting for a hello',
              _wait_for(lambda: not hotel._pending), True)  # pylint: disable=protected-access
        check('with nobody reported connected', events.snapshot(), [])
    finally:
        hotel.stop()
        root.removeHandler(capture)
        root.setLevel(level)


def test_browser_token():
    """A browser is served only when it presents the token left beside the page."""
    import tempfile  # pylint: disable=import-outside-toplevel
    print('Browser token')
    check('the token is read from the query', webbridge.token_in('/?t=abc'), 'abc')
    check('a bare path carries none', webbridge.token_in('/'), '')
    check('nor does a missing path', webbridge.token_in(None), '')

    with tempfile.TemporaryDirectory() as folder:
        token = webbridge.issue_token(folder)
        with open(os.path.join(folder, webbridge.TOKEN_FILE), encoding='utf-8') as handle:
            written = handle.read()
        check('the token is left where the page can load it',
              written, f'window.__BICHOK_TOKEN = "{token}";\n')
        check('and each start mints a new one', webbridge.issue_token(folder) != token, True)

    bridge = webbridge.WebBridge(token=token)
    check('the right token is admitted', bridge.admits(f'/?t={token}'), True)
    check('a wrong one is refused', bridge.admits('/?t=' + 'x' * len(token)), False)
    check('none at all is refused', bridge.admits('/'), False)
    check('an empty one is refused', bridge.admits('/?t='), False)
    check('a bridge given no token admits anyone', webbridge.WebBridge().admits('/'), True)


def main():
    """Run every check and report."""
    for test in (test_clock_relationships, test_example_one, test_example_three,
                 test_no_action_cases, test_in_place_rewrite, test_tombstones, test_identity,
                 test_peer_information_roundtrip, test_log_hash, test_qso_hash,
                 test_owner_epoch_rules, test_owner_identification,
                 test_owner_going_epochless, test_reply_announce,
                 test_duplicate_fill, test_null_epoch_packet, test_packet_shape, test_framing,
                 test_hello, test_who_connects, test_duplicate_choice, test_serial_number_ignored,
                 test_epochless_packets, test_conflict_info_decodes, test_sent_archive_is_foundation,
                 test_session_start, test_restart_replays, test_transport_loopback,
                 test_transport_duplicates, test_transport_guards, test_transport_unreachable,
                 test_browser_token):
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
