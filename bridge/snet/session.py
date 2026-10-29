"""The SkookumNet peer itself: packet decoding and the sync state machine.

This peer only listens. The transport finds SkookumLogger and keeps a connection to it; this side
broadcasts its own PeerInformation so it takes part in the sync properly, and never sends anything
that could change another station's log.

Three rules govern the sync epoch and all of them matter:

  * An epoch is never invented here. Only SkookumLogger's Reset button creates one. This peer
    adopts whichever epoch it observes and echoes that back.
  * The epoch follows the log's owner -- the peer that sends sync packets, which only a logger
    ever does. The owner's word
    is followed in both directions, older as well as newer (backward only from its
    PeerInformation, which cannot be a straggler -- see _epoch_allows), because replacing the log file on
    the owner rolls its epoch *back*; a passive peer that keeps naming the newest epoch it ever
    saw reads to SkookumLogger as a request to reset the log, and confirming that dialog erases
    the log for real. Until an owner is known, newer epochs are adopted from anyone (a freshly
    reset SkookumLogger has no QSOs to send, so insisting on an owner there would deadlock);
    once one is known, other peers' epochs are just echoes of the past and never a trigger.
  * A packet stamped with an epoch that was not accepted by the above is dropped. A packet with
    no epoch at all is only good for its PeerInformation. PeerInformation is always read
    regardless, because it is what carries the epoch that every other decision depends on.

Announcements are replies, not a heartbeat. Answering the owner's advertisement right after
taking in its current epoch shrinks the window in which a stale epoch of ours is on the wire
from five seconds to milliseconds -- and it was exactly that window that produced the reset
dialog above. While no owner is known, every peer's advertisement is answered, since without
announcing our clock nobody sends us a fill.
"""
import logging
import plistlib
import pprint
import time

import objc
from Foundation import NSObject, NSDate, NSNull, NSKeyedArchiver, NSKeyedUnarchiver, NSProcessInfo, NSString

from . import clock as vclock
from . import log as qsolog
from . import protocol
from .objects import PeerInformation, Activity, allowed_classes, archivable


def station_name():
    """Derive this machine's station name the way SkookumLogger does.

    The name has to match, because it is the key this station occupies in every vector clock on
    the network. SkookumLogger takes the host name, drops a .local or .lan suffix and capitalises
    what is left, then uppercases the whole thing if it contains a digit.
    """
    host = str(NSProcessInfo.processInfo().hostName())
    for suffix in ('.local', '.lan'):
        if host.endswith(suffix):
            host = str(NSString.stringWithString_(host[:-len(suffix)]).capitalizedString())
            break
    if any(character.isdigit() for character in host):
        host = host.upper()
    return host


def _is_dictionary(value):
    return isinstance(value, dict) or hasattr(value, 'objectForKey_')


class SkookumNetPeer(NSObject):
    """Receives SkookumNet traffic and reports QSO changes to a listener."""

    def initWithStation_listener_(self, station, listener):
        """Set the peer up. The listener receives the QSO and session callbacks; the transport
        that carries the packets is attached afterwards."""
        self = objc.super(SkookumNetPeer, self).init()
        if self is None:
            return None

        self.transport = None
        self.listener = listener
        # Announce ourselves under our own peer name, not the machine's station name. This bridge
        # normally runs on the same Mac as SkookumLogger, which derives its station name from the
        # host name -- taking that name too would collide with it in the peer table and in every
        # vector clock on the network.
        self.station = station
        self.contest = None
        self.epoch = None
        self.log = qsolog.QsoLog()
        self.classes = allowed_classes()
        self.last_peer_info = {}
        self.known_stations = set()
        self.listen_only = False
        self.dump_peer_info = False
        self.dumped = set()
        self.contest_start = None
        self.contest_end = None
        self.peer_version = None
        self.hash_agreed = None
        self.binary_version = None
        self.sync_existing_log = False
        self.owner = None
        self.last_announce = None
        self.in_session = False
        return self

    # --- outgoing ---

    def announce_(self, _timer):
        """Broadcast what we have seen.

        This doubles as the request for a fill: the clock we send tells the other peers what we
        are missing, and they push it to us. It is the only reason a listen-only peer has to
        transmit at all.
        """
        info = PeerInformation.alloc().initWithStationName_(self.station)
        # The clock names every station we know of, not just the ones we hold events from. Our own
        # entry is required and never moves past zero, since we originate nothing. The others are
        # there so a peer reading our clock can see the gap without having to infer that a missing
        # station means zero -- a peer that scans only the names we sent would otherwise never
        # compare itself against us at all, and would never work out that we are behind.
        clock = dict(self.log.node_clock)
        clock.setdefault(self.station, 0)
        for station in self.known_stations:
            clock.setdefault(station, 0)
        info.vectorClock = clock
        info.syncEpoch = self.epoch

        # Everything below describes the contest the peers are working, echoed back from what
        # they themselves advertise.
        info.contestName = self.contest or ''
        info.contestStartTime = self.contest_start
        info.contestEndTime = self.contest_end
        info.timestamp = NSDate.date()
        info.hashOfQSOs = self.log.log_hash()

        # SkookumLogger sends a peer the events it is missing only when this field matches its own
        # identifier exactly, down to the UUID of its binary. Nothing that is not SkookumLogger can
        # match that honestly -- and neither can two SkookumLoggers of different builds, which is
        # why the check reads as a guard against mixing test builds rather than against other
        # software. We name ourselves honestly by default and go without the log we missed;
        # --sync-existing-log repeats the peer's own identifier back to it, which is what it takes
        # to be sent that log.
        if self.sync_existing_log and self.peer_version:
            info.binaryVersion = self.peer_version
        else:
            info.binaryVersion = self.binary_version
        logging.debug("Announcing ourselves as %s holding %s", self.station, info.vectorClock)
        self._send(protocol.PEER_INFORMATION, info)

    @objc.python_method
    def _send(self, tag, payload):
        """Archive and send one packet to every connected peer.

        A packet always has three elements, so an epoch we do not have yet travels as NSNull
        rather than as a missing element.
        """
        if self.transport is None or self.listen_only:
            return
        peers = self.transport.peers()
        if not peers:
            return

        packet = archivable([tag, self.epoch if self.epoch is not None else NSNull.null(), payload])
        data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(packet, True, None)
        if error is not None:
            logging.error("Could not archive packet %s: %s", tag, error)
            return
        self.transport.send(bytes(data))
        logging.debug("Sent tag %s to %d peer(s), epoch %s", tag, len(peers), self.epoch)

    # --- transport delegate, called on the transport's thread ---

    @objc.python_method
    def transport_connected(self, peer):
        """Pass the news to the main thread, where all decoding and merging happens."""
        self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_('peerConnected:', {'peer': peer}, False)

    @objc.python_method
    def transport_disconnected(self, peer):
        """Pass the news to the main thread."""
        self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_('peerDisconnected:', {'peer': peer}, False)

    @objc.python_method
    def transport_received(self, peer, data):
        """Pass the packet to the main thread."""
        self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
            'receive:', {'peer': peer, 'data': data}, False)

    # --- main-thread handlers ---

    def peerConnected_(self, info):
        """Note the new peer. The session starts once a PeerInformation names the contest."""
        logging.info("%s joined; waiting for its PeerInformation", info['peer'])
        if self.sync_existing_log and len(self.transport.peers()) > 1:
            logging.warning("More than one peer is on the network. Other operators will see this "
                            "bridge listed with their own version string in its tooltip; start with "
                            "--no-sync-existing-log if that would confuse anyone.")

    def peerDisconnected_(self, info):
        """Treat the departure of the last peer as the end of the session."""
        logging.info("%s left", info['peer'])
        if self.in_session and not self.transport.peers():
            logging.info("%s was the last peer; the session is over", info['peer'])
            self.in_session = False
            self.listener.session_ended()

    def receive_(self, info):
        """Decode one packet and act on it."""
        peer = info['peer']
        packet, error = NSKeyedUnarchiver.unarchivedObjectOfClasses_fromData_error_(
            self.classes, info['data'], None)
        if error is not None or packet is None:
            logging.error("Could not decode a packet from %s (%s): %s",
                          peer, protocol.peek_packet(bytes(info['data'])), error)
            return
        parts = protocol.split_packet(packet)
        if parts is None:
            logging.error("Ignoring a malformed packet from %s", peer)
            return
        tag, epoch, payload = parts

        if tag == protocol.SERIAL_NUMBER:
            # Serial numbers are handed out by a server of their own between loggers. SkookumLogger
            # exempts these packets from every check below, and nothing here takes part in it.
            logging.debug("Ignoring a serial number packet from %s", peer)
            return

        if self.dump_peer_info and peer not in self.dumped and isinstance(payload, PeerInformation):
            self._dump_archive(info['data'], peer)

        if epoch is None and tag != protocol.PEER_INFORMATION:
            # Only PeerInformation travels without an epoch; SkookumLogger drops anything else so sent
            logging.debug("Dropping a tag %s packet from %s that carries no epoch", tag, peer)
            return

        # A peer that sends sync packets is the log's owner -- the one whose epoch is to be
        # followed. Learned before the epoch check so that the very packet that identifies the
        # owner is already judged by the owner's rules.
        if self._marks_owner(tag, payload):
            self._learn_owner(peer)

        # PeerInformation is exempt from the epoch check: it is what carries the epoch, so
        # dropping it would leave us permanently unable to learn the one we are missing.
        if tag != protocol.PEER_INFORMATION and not self._epoch_allows(epoch, peer):
            return

        self._dispatch(tag, payload, peer)

    @objc.python_method
    def _marks_owner(self, tag, payload):
        """Say whether this packet identifies its sender as the log's owner.

        Any sync dictionary counts, QSOs or not: only a logger ever sends SyncQso, so the packet
        kind alone says what its sender is. Counting the empty ones matters -- a freshly reset
        SkookumLogger really does send QSO-less SyncQso packets (ContestPan measured a burst of
        them right after an Initialize), and those let it establish itself as owner before it has
        a single QSO to its name. (Agreed with ContestPan, 2026-08-05, after first requiring QSOs
        here.)
        """
        return tag == protocol.SYNC_QSO and _is_dictionary(payload)

    @objc.python_method
    def _learn_owner(self, peer):
        """Remember which peer the log belongs to."""
        if self.owner != peer:
            logging.info("The log belongs to %s; its epoch is the one to follow", peer)
            self.owner = peer

    @objc.python_method
    def _dump_archive(self, data, peer):
        """Print the raw archive of the first PeerInformation a peer sends.

        Our decoder only reads the keys we already know about, so a field SkookumLogger added and
        we have never heard of is invisible -- it simply does not appear anywhere. Reading the
        archive itself shows every key that actually travels, which is the only way to find out
        what we are failing to send back.
        """
        self.dumped.add(peer)
        try:
            archive = plistlib.loads(bytes(data))
        except (plistlib.InvalidFileException, ValueError, TypeError) as error:
            logging.warning("Could not read the raw archive from %s: %s", peer, error)
            return
        logging.info("Raw PeerInformation archive from %s:\n%s",
                     peer, pprint.pformat(archive.get('$objects', archive), width=110))

    @objc.python_method
    def _compare_log_hash(self, info, peer):
        """Say whether our copy of the log agrees with the one the peer advertises.

        This is the comparison behind SkookumLogger's "QSOs do not match" tooltip, so knowing
        which side of it we are on -- and logging the numbers when we are on the wrong side --
        is what lets the hashing be calibrated against the real thing.
        """
        if info.hashOfQSOs is None:
            return
        ours = self.log.log_hash()
        agreed = (ours == info.hashOfQSOs)
        if agreed != self.hash_agreed:
            self.hash_agreed = agreed
            if agreed:
                logging.info("Our copy of the log agrees with %s (hash %d over %d QSOs)",
                             peer, ours, len(self.log.qsos))
            else:
                logging.info("Our copy of the log differs from %s: ours %d (%d QSOs, "
                             "without tombstones %d), theirs %d",
                             peer, ours, len(self.log.qsos),
                             self.log.log_hash(include_deleted=False), info.hashOfQSOs)

    @objc.python_method
    def _epoch_allows(self, epoch, peer, advertised=False):
        """Decide whether a packet's epoch lets us act on it, adopting it when its source may
        change ours. `advertised` says the epoch came from a PeerInformation payload.

        The owner's epoch is followed in both directions -- replacing the log file on the owner
        rolls its epoch back, and refusing to roll back with it is what turns into the reset
        dialog on SkookumLogger's side. The *backward* direction is followed only from the
        owner's PeerInformation, though: an advertisement carries its sender's present, while a
        data packet stamped with an old epoch can equally be a straggler from before the move,
        and rolling back on one of those would clear the log for nothing (the owner's next
        advertisement would move it right back). Anyone's newer epoch is followed only while no
        owner is known; once one is, other peers' epochs are echoes of the past and get dropped.
        """
        if epoch is None:
            return False  # only PeerInformation may come without one, and it is judged elsewhere
        if self.epoch is not None and self.epoch.compare_(epoch) == 0:
            return True

        if self.owner is None:
            if self.epoch is None or self.epoch.compare_(epoch) < 0:
                self._adopt_epoch(epoch, peer)
                return True
            logging.debug("Dropping a stale packet from %s", peer)
            return False

        if peer == self.owner:
            if self.epoch is None or self.epoch.compare_(epoch) < 0 or advertised:
                self._adopt_epoch(epoch, peer)
                return True
            logging.debug("Dropping a straggler from before %s moved the log", peer)
            return False

        logging.debug("Ignoring the epoch %s named by %s: the log belongs to %s", epoch, peer, self.owner)
        return False

    @objc.python_method
    def _adopt_epoch(self, epoch, peer):
        """Take on another epoch and start over: the log we held belongs to a different generation.

        Newer means somebody reset; older means the owner's log file was replaced by one with an
        earlier reset in its history. Both change which log this is, so both start it afresh.
        """
        if self.epoch is not None:
            logging.info("%s moved the log to epoch %s (we were at %s); starting over", peer, epoch, self.epoch)
        else:
            logging.info("Adopting the epoch %s advertised by %s", epoch, peer)
        self.epoch = epoch
        self.log.clear()
        self.listener.log_cleared()

    @objc.python_method
    def _dispatch(self, tag, payload, peer):
        """Route a payload by its tag, checking that it is what the tag promises."""
        if tag == protocol.PEER_INFORMATION and isinstance(payload, PeerInformation):
            self._handle_peer_information(payload, peer)
        elif tag == protocol.SYNC_QSO and _is_dictionary(payload):
            self._handle_sync(payload, peer)
        elif tag in (protocol.ADD_ACTIVITY, protocol.CHANGE_ACTIVITY, protocol.DELETE_ACTIVITY) \
                and isinstance(payload, Activity):
            logging.debug("Spot from %s: %s", peer, payload)
        elif tag == protocol.GAB and isinstance(payload, str):
            logging.info("Message from %s: %s", peer, payload)
        elif tag == protocol.PARTNER_PROMPT:
            logging.debug("Partner prompt from %s: %s", peer, payload)
        else:
            logging.debug("Ignoring a %s payload under tag %s from %s", type(payload).__name__, tag, peer)

    @objc.python_method
    def _handle_peer_information(self, info, peer):
        """Record what a peer knows, and answer it."""
        if str(info) != self.last_peer_info.get(peer):
            logging.info("%s", info)
            self.last_peer_info[peer] = str(info)

        if info.syncEpoch is not None:
            self._epoch_allows(info.syncEpoch, peer, advertised=True)
        elif peer == self.owner and self.epoch is not None:
            # The owner now advertises no epoch at all: its log was replaced by a brand-new one
            # with no reset in its history. Following it down matters twice over -- the QSOs we
            # hold belong to the log that went away, and a SkookumLogger without an epoch cannot
            # treat a peer that names one as a sync partner. Only the payload's own field says
            # this; the NSNull in the packet frame is the normal shape of "not set" and says
            # nothing. (An epoch-less SkookumLogger still leaves every peer "not synced" until
            # its Initialize button issues a fresh epoch -- that part only the operator can do.)
            logging.info("%s no longer advertises an epoch; clearing ours and starting over", peer)
            self.epoch = None
            self.log.clear()
            self.listener.log_cleared()

        # Deliberately not folded into our own clock. A peer's broadcast says what *it* has, and
        # claiming those events as ours would tell it we are up to date -- so it would send us
        # nothing, forever. Our clock advances only when QSOs actually arrive.
        if self.owner is None or peer == self.owner:
            self._echo_from(info, peer)

        self._compare_log_hash(info, peer)

        theirs = vclock.to_dict(info.vectorClock)
        self.known_stations.update(theirs)
        if info.peerHostName:
            self.known_stations.add(str(info.peerHostName))
        self.known_stations.discard(self.station)

        if theirs and vclock.compare(self.log.node_clock, theirs) == vclock.DOMINATED:
            logging.debug("%s is ahead of us (%s against our %s); expecting a fill",
                          peer, theirs, self.log.node_clock)

        self.listener.peer_information(info)
        self._reply(peer)

    @objc.python_method
    def _echo_from(self, info, peer):
        """Take on what we echo back -- the build, the contest and its times -- from this peer,
        and start the session once we know which contest it is about.

        SkookumLogger shares the log only with a peer that names its own build and contest, so
        these come from the owner once one is known and from anyone until then: a passive peer
        that names itself honestly would otherwise talk us into a mismatch. The contest name
        arrives here and nowhere else, so the session begins -- or begins again, under a new
        name -- only now.
        """
        if info.binaryVersion and self.peer_version != str(info.binaryVersion):
            self.peer_version = str(info.binaryVersion)
            logging.info("%s is running %s", peer, self.peer_version)
        if info.contestStartTime is not None:
            self.contest_start = info.contestStartTime
        if info.contestEndTime is not None:
            self.contest_end = info.contestEndTime

        contest = str(info.contestName) if info.contestName else None
        if contest and contest != self.contest:
            logging.info("%s is working %s", peer, contest)
            self.contest = contest
            self.in_session = False
        if not self.in_session:
            self.in_session = True
            self.listener.session_started(self.contest)

    @objc.python_method
    def _reply(self, peer):
        """Answer an advertisement with our own, when this peer's advertisements deserve one.

        Announcing here rather than on a timer means every announcement of ours goes out moments
        after taking in the owner's current state, so a stale epoch of ours is on the wire for
        milliseconds instead of a five-second window. The owner is always answered; while no owner
        is known, everyone is (announcing our clock is what makes somebody send us a fill). A
        passive peer's advertisement is never answered once an owner is known -- that one reply
        would be the hole a stale epoch escapes through. The floor breaks the loop of two
        ownerless passive peers answering each other's answers forever.
        """
        if self.owner is not None and peer != self.owner:
            return
        now = time.monotonic()
        if self.last_announce is not None and now - self.last_announce < protocol.ANNOUNCE_FLOOR:
            return
        self.last_announce = now
        self.announce_(None)

    @objc.python_method
    def _handle_sync(self, payload, peer):
        """Apply a sync packet: some QSOs, maybe contest settings, maybe a clock."""
        qsos = payload.get(protocol.SYNC_QSOS) if hasattr(payload, 'get') else payload[protocol.SYNC_QSOS]
        sender_clock = payload.get(protocol.SYNC_CLOCK) if hasattr(payload, 'get') else None

        if sender_clock is not None:
            self.log.observe_clock(sender_clock)

        if not qsos:
            return

        logging.info("%d QSO(s) from %s", len(qsos), peer)
        for qso in qsos:
            outcome = self.log.merge_qso(qso)
            self._report(outcome, qso)

    @objc.python_method
    def _report(self, outcome, qso):
        """Pass a merge outcome on to the listener."""
        if outcome is not None:
            received = qso.receivedExchange.as_dict() if qso.receivedExchange is not None else {}
            logging.info("%s: %s on %s %s at %s, station %s seq %s clock %s flags %s",
                         outcome, received.get('call', '?'), qso.transmitFrequency, qso.mode,
                         qso.timeStamp, qso.stationName, qso.sequenceID,
                         vclock.to_dict(qso.vectorClock), protocol.describe_flags(int(qso.flags or 0)))

        if outcome == qsolog.ADDED:
            self.listener.qso_added(qso)
        elif outcome == qsolog.UPDATED:
            self.listener.qso_updated(qso)
        elif outcome == qsolog.DELETED:
            self.listener.qso_deleted(qso)
        elif outcome == qsolog.CONFLICTED:
            self.listener.qso_conflicted(qso)
