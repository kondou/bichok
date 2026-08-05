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


def test_silence_watchdog():
    """A connected but silent session is rebuilt; a live or empty one is left alone.

    The dead-session shape comes from ContestPan's field observation: the peer is recreated on
    the other side, no disconnect event ever arrives, and reception just stops. PeerInformation
    is due every five seconds, so a long silence with peers connected can only mean the session
    is dead.
    """
    print('silence watchdog')
    import time  # pylint: disable=import-outside-toplevel
    from MultipeerConnectivity import MCPeerID, MCSession  # pylint: disable=import-outside-toplevel
    from snet.session import SkookumNetPeer  # pylint: disable=import-outside-toplevel

    class FakeSession:
        """Stands in for MCSession, remembering whether it was torn down."""
        def __init__(self, peers):
            self.peers = peers
            self.disconnected = False
        def connectedPeers(self):
            return self.peers
        def setDelegate_(self, _delegate):
            pass
        def disconnect(self):
            self.disconnected = True

    class FakeListener:
        """Records the one callback the rebuild makes."""
        def __init__(self):
            self.ended = False
        def session_ended(self):
            self.ended = True

    peer_id = MCPeerID.alloc().initWithDisplayName_('BicHok')

    def build(peers):
        listener = FakeListener()
        session = FakeSession(peers)
        made = SkookumNetPeer.alloc().initWithSession_peerID_listener_(session, peer_id, listener)
        return made, session, listener

    peer, session, _ = build([])
    peer.last_received = time.monotonic() - 999
    peer._check_silence()  # pylint: disable=protected-access
    check('no peers means no rebuild', peer.session is session, True)
    check('and the baseline is dropped', peer.last_received, None)

    peer, session, _ = build(['fake'])
    peer.last_received = time.monotonic()
    peer._check_silence()  # pylint: disable=protected-access
    check('a live session is left alone', peer.session is session, True)

    peer, session, _ = build(['fake'])
    peer.last_received = None
    peer._check_silence()  # pylint: disable=protected-access
    check('a connected peer with no baseline starts one', peer.last_received is not None, True)
    check('without rebuilding', peer.session is session, True)

    peer, session, listener = build(['fake'])
    peer.last_received = time.monotonic() - (protocol.SILENCE_TIMEOUT + 1)
    peer._check_silence()  # pylint: disable=protected-access
    check('a silent session is torn down', session.disconnected, True)
    check('and replaced with a real one', isinstance(peer.session, MCSession), True)
    check('the listener hears the session end', listener.ended, True)
    check('the baseline resets for the next session', peer.last_received, None)


def build_peer(connected):
    """Return a SkookumNetPeer wired to recording stand-ins, plus those stand-ins.

    The session records what gets sent instead of sending it, and the listener counts the
    callbacks the epoch rules make. Nothing touches the network.
    """
    from MultipeerConnectivity import MCPeerID  # pylint: disable=import-outside-toplevel
    from snet.session import SkookumNetPeer  # pylint: disable=import-outside-toplevel

    class RecordingSession:
        """Stands in for MCSession, remembering what was sent through it."""
        def __init__(self, peers):
            self.peers = list(peers)
            self.sent = []
        def connectedPeers(self):
            return self.peers
        def setDelegate_(self, _delegate):
            pass
        def disconnect(self):
            pass
        def sendData_toPeers_withMode_error_(self, data, peers, mode, error):  # pylint: disable=unused-argument
            self.sent.append(data)
            return (True, None)

    class RecordingListener:
        """Counts the callbacks the session makes."""
        def __init__(self):
            self.cleared = 0
        def log_cleared(self):
            self.cleared += 1
        def session_started(self, contest):
            pass
        def session_ended(self):
            pass
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

    listener = RecordingListener()
    session = RecordingSession(connected)
    peer_id = MCPeerID.alloc().initWithDisplayName_('BicHok')
    peer = SkookumNetPeer.alloc().initWithSession_peerID_listener_(session, peer_id, listener)
    return peer, session, listener


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
    check('a 5.x delete list marks the owner',
          peer._marks_owner(protocol.LEGACY_DELETE_QSOS, [qso]), True)  # pylint: disable=protected-access
    check('a list under any other tag does not',
          peer._marks_owner(protocol.LEGACY_GAB, [qso]), False)  # pylint: disable=protected-access

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
    peer.dialect = 'sync2'

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


def test_readvertise():
    """A long wait with nobody connected replaces the advertisement, and keeps replacing it."""
    print('re-advertising when alone')
    import time  # pylint: disable=import-outside-toplevel

    class FakeAdvertiser:
        """Stands in for MCNearbyServiceAdvertiser."""
        def __init__(self):
            self.advertising = False
        def startAdvertisingPeer(self):
            self.advertising = True
        def stopAdvertisingPeer(self):
            self.advertising = False

    peer, session, _ = build_peer([])
    peer.service_type = 'skookumnetwork'
    peer._make_advertiser = FakeAdvertiser  # pylint: disable=protected-access
    first = FakeAdvertiser()
    first.advertising = True
    peer.advertiser = first

    peer._check_alone()  # pylint: disable=protected-access
    check('the first lonely look starts the clock', peer.alone_since is not None, True)
    check('without replacing anything', peer.advertiser is first, True)

    peer.alone_since = time.monotonic() - (protocol.READVERTISE_TIMEOUT + 1)
    peer._check_alone()  # pylint: disable=protected-access
    check('the timeout replaces the advertiser', peer.advertiser is not first, True)
    check('the old one was withdrawn', first.advertising, False)
    check('the new one is advertising', peer.advertiser.advertising, True)
    check('and the clock restarts for the next round', peer.alone_since is not None, True)

    second = peer.advertiser
    peer.alone_since = time.monotonic() - (protocol.READVERTISE_TIMEOUT + 1)
    peer._check_alone()  # pylint: disable=protected-access
    check('it repeats for as long as nobody connects', peer.advertiser is not second, True)

    third = peer.advertiser
    session.peers = ['fake']
    peer.alone_since = time.monotonic() - (protocol.READVERTISE_TIMEOUT + 1)
    peer._check_alone()  # pylint: disable=protected-access
    check('a connected peer stops the clock', peer.alone_since is None, True)
    check('and the advertiser stays', peer.advertiser is third, True)


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


def main():
    """Run every check and report."""
    for test in (test_clock_relationships, test_example_one, test_example_three,
                 test_no_action_cases, test_in_place_rewrite, test_tombstones, test_identity,
                 test_peer_information_roundtrip, test_log_hash, test_qso_hash,
                 test_silence_watchdog, test_owner_epoch_rules, test_owner_identification,
                 test_owner_going_epochless, test_reply_announce, test_readvertise,
                 test_duplicate_fill, test_null_epoch_packet):
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
