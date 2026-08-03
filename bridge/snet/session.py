"""The SkookumNet peer itself: discovery, packet decoding, and the sync state machine.

This peer only listens. It advertises itself so SkookumLogger can invite it, broadcasts its own
PeerInformation so it takes part in the sync properly, and never sends anything that could change
another station's log.

Two rules govern the sync epoch and both matter:

  * An epoch is never invented here. Only SkookumLogger's Reset button creates one. This peer
    adopts whichever epoch it observes and echoes that back.
  * A packet stamped with an epoch older than the current one is stale and gets dropped. A packet
    with no epoch at all is only good for its PeerInformation. PeerInformation is always read
    regardless, because it is what carries the epoch that every other decision depends on.
"""
import logging
import plistlib
import pprint
import time

import objc
from Foundation import NSObject, NSDate, NSNull, NSTimer, NSKeyedArchiver, NSKeyedUnarchiver, NSProcessInfo, NSString
from MultipeerConnectivity import MCSession, MCEncryptionNone, MCSessionSendDataReliable
from MultipeerConnectivity import MCSessionStateConnected, MCSessionStateNotConnected

from . import clock as vclock
from . import log as qsolog
from . import protocol
from .objects import PeerInformation, TransientQso, Activity, PeerContestInfo, allowed_classes

MCNearbyServiceAdvertiserDelegate = objc.protocolNamed('MCNearbyServiceAdvertiserDelegate')
MCSessionDelegate = objc.protocolNamed('MCSessionDelegate')


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


class SkookumNetPeer(NSObject, protocols=[MCSessionDelegate, MCNearbyServiceAdvertiserDelegate]):
    """Receives SkookumNet traffic and reports QSO changes to a listener."""

    def initWithSession_peerID_listener_(self, session, peerID, listener):
        """Wire up the peer. The listener receives the QSO and session callbacks."""
        self = objc.super(SkookumNetPeer, self).init()
        if self is None:
            return None

        self.session = session
        self.peerID = peerID
        self.listener = listener
        # Announce ourselves under our own peer name, not the machine's station name. This bridge
        # normally runs on the same Mac as SkookumLogger, which derives its station name from the
        # host name -- taking that name too would collide with it in the peer table and in every
        # vector clock on the network.
        self.station = str(peerID.displayName())
        self.contest = None
        self.epoch = None
        self.log = qsolog.QsoLog()
        self.classes = allowed_classes()
        self.timer = None
        self.dialect = None
        self.asked_for_legacy_log = False
        self.last_peer_info = {}
        self.known_stations = set()
        self.invited_on = None
        self.listen_only = False
        self.dump_peer_info = False
        self.dumped = set()
        self.contest_start = None
        self.contest_end = None
        self.peer_version = None
        self.hash_agreed = None
        self.binary_version = None
        self.sync_existing_log = False
        self.last_received = None
        return self

    # --- outgoing ---

    @objc.python_method
    def start(self):
        """Begin broadcasting our own PeerInformation on the usual interval."""
        if self.timer is None:
            self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                protocol.PEER_INFORMATION_INTERVAL, self, 'announce:', None, True)

    @objc.python_method
    def stop(self):
        """Stop broadcasting."""
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None

    def announce_(self, _timer):
        """Broadcast what we have seen.

        Under SkookumNet-2 this doubles as the request for a fill: the clock we send tells the
        other peers what we are missing, and they push it to us. It is the only reason a
        listen-only peer has to transmit at all.
        """
        self._check_silence()
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

        # Everything below describes the contest we were invited to, echoed back from what the
        # peers themselves advertise.
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
        """Archive and send one packet to every connected peer, in the dialect that peer speaks.

        Nothing goes out before the generation is known, and a SkookumNet-2 packet always carries
        three elements. Both rules follow from the same fact: a SkookumNet-2 peer takes the payload
        from the third element, so a two-element packet is not something it can act on. An epoch we
        do not have yet therefore travels as NSNull rather than as a missing element.
        """
        peers = self.session.connectedPeers()
        if not peers or self.dialect is None or self.listen_only:
            return

        if self.dialect == 'sync2':
            packet = [tag, self.epoch if self.epoch is not None else NSNull.null(), payload]
        else:
            packet = [tag, payload]

        data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(packet, True, None)
        if error is not None:
            logging.error("Could not archive packet %s: %s", tag, error)
            return

        _, error = self.session.sendData_toPeers_withMode_error_(data, peers, MCSessionSendDataReliable, None)
        if error is not None:
            logging.error("Could not send packet %s: %s", tag, error)
        else:
            logging.debug("Sent tag %s to %d peer(s) as %d elements, epoch %s",
                          tag, len(peers), len(packet), self.epoch)

    @objc.python_method
    def _check_silence(self):
        """Rebuild the session when connected peers have gone silent.

        A peer can vanish without a disconnect event ever arriving. PeerInformation comes every
        five seconds while a session is alive, so a long silence from a connected peer means the
        session is dead, not idle. The advertiser is unaffected, so a fresh session gets found and
        invited again, and the log we kept means the gap arrives as a fill.
        """
        if not self.session.connectedPeers():
            self.last_received = None
            return
        now = time.monotonic()
        if self.last_received is None:
            self.last_received = now
            return
        quiet = now - self.last_received
        if quiet < protocol.SILENCE_TIMEOUT:
            return
        logging.warning("Nothing has arrived for %.0f seconds although %d peer(s) look connected; "
                        "rebuilding the session so we can be invited again",
                        quiet, len(self.session.connectedPeers()))
        self._rebuild_session()

    @objc.python_method
    def _rebuild_session(self):
        """Replace a dead session with a fresh one, telling the listener the old one is over.

        The old session's delegate is detached first so its teardown cannot deliver stale
        callbacks. Everything else -- log, epoch, dialect -- is kept: the peer and its log have
        not changed, only the transport died under them.
        """
        old = self.session
        old.setDelegate_(None)
        old.disconnect()
        self.session = MCSession.alloc().initWithPeer_securityIdentity_encryptionPreference_(
            self.peerID, None, MCEncryptionNone)
        self.session.setDelegate_(self)
        self.last_received = None
        self.listener.session_ended()

    @objc.python_method
    def _request_legacy_log(self):
        """Ask a SkookumLogger 5.x peer for its whole log.

        SkookumNet-2 has no equivalent and needs none: joining is enough to be sent everything.
        """
        packet = [protocol.LEGACY_REQUEST_ALL_QSOS, self.peerID]
        data, error = NSKeyedArchiver.archivedDataWithRootObject_requiringSecureCoding_error_(packet, True, None)
        if error is not None:
            logging.error("Could not archive the log request: %s", error)
            return
        peers = self.session.connectedPeers()
        self.session.sendData_toPeers_withMode_error_(data, peers, MCSessionSendDataReliable, None)
        logging.info("Asked the 5.x peer for its log")

    # --- MCNearbyServiceAdvertiser delegate ---

    def advertiser_didNotStartAdvertisingPeer_(self, advertiser, error):
        """Report a failure to advertise; without it SkookumLogger can never find us."""
        logging.error("Advertising failed: %s", error)

    def advertiser_didReceiveInvitationFromPeer_withContext_invitationHandler_(
            self, advertiser, peerID, context, invitationHandler):
        """Accept every invitation, taking the contest name from the first one that carries it."""
        if context is not None and self.contest is None:
            try:
                self.contest = bytes(context).decode('utf-8')
            except (UnicodeDecodeError, TypeError) as error:
                logging.warning("Could not read the contest name from the invitation: %s", error)
        invitationHandler(True, self.session)

        # Record which advertised name the invitation actually arrived on. SkookumLogger renamed
        # the service somewhere after 5.1 -- that release advertises skookum-network, while the
        # last 5.x and 6.0 both use skookumnetwork -- so if a second advertiser is ever needed for
        # the older name, this log says whether anyone is still calling on it.
        try:
            self.invited_on = str(advertiser.serviceType())
        except AttributeError:
            self.invited_on = None

        logging.info("Joined %s at the invitation of %s, on _%s._tcp",
                     self.contest or 'an unnamed contest', peerID.displayName(),
                     self.invited_on or 'unknown')

    # --- MCSession delegate ---

    @objc.typedSelector(b'v@:@@q')
    def session_peer_didChangeState_(self, session, peerID, state):
        """Track peers joining and leaving."""
        if state == MCSessionStateConnected:
            self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
                'peerConnected:', {'peer': str(peerID.displayName())}, False)
        elif state == MCSessionStateNotConnected:
            logging.info("%s left", peerID.displayName())
            if len(session.connectedPeers()) == 0:
                self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
                    'lastPeerLeft:', {'peer': str(peerID.displayName())}, False)

    def session_didReceiveData_fromPeer_(self, session, data, peerID):
        """Hand the packet to the main thread, where all decoding and merging happens."""
        self.pyobjc_performSelectorOnMainThread_withObject_waitUntilDone_(
            'receive:', {'peer': str(peerID.displayName()), 'data': data}, False)

    def session_didReceiveCertificate_fromPeer_certificateHandler_(self, session, certificate, peerID, handler):
        """Accept peers unconditionally. SkookumNet runs unencrypted on a local network."""
        handler(True)

    def session_didReceiveStream_withName_fromPeer_(self, session, stream, streamName, peerID):
        """SkookumNet sends no streams."""

    def session_didStartReceivingResourceWithName_fromPeer_withProgress_(
            self, session, resourceName, peerID, progress):
        """SkookumNet sends no resources."""

    def session_didFinishReceivingResourceWithName_fromPeer_atURL_withError_(
            self, session, resourceName, peerID, localURL, error):
        """SkookumNet sends no resources."""

    # --- main-thread handlers ---

    def peerConnected_(self, info):
        """Note the new peer, but say nothing until it has shown us which protocol it speaks."""
        logging.info("%s joined; waiting to hear which protocol it speaks before transmitting", info['peer'])
        self.last_received = time.monotonic()
        if self.sync_existing_log and len(self.session.connectedPeers()) > 1:
            logging.warning("More than one peer is on the network. Other operators will see this "
                            "bridge listed with their own version string in its tooltip; start with "
                            "--no-sync-existing-log if that would confuse anyone.")
        self.listener.session_started(self.contest)

    def lastPeerLeft_(self, info):
        """Treat the departure of the last peer as the end of the session."""
        logging.info("%s was the last peer; the session is over", info['peer'])
        self.listener.session_ended()

    def receive_(self, info):
        """Decode one packet and act on it."""
        self.last_received = time.monotonic()  # any bytes at all mean the session is alive
        packet, error = NSKeyedUnarchiver.unarchivedObjectOfClasses_fromData_error_(
            self.classes, info['data'], None)
        if error is not None:
            logging.error("Could not decode a packet from %s: %s", info['peer'], error)
            return
        if packet is None or len(packet) < 2:
            logging.error("Ignoring a malformed packet from %s", info['peer'])
            return

        self._learn_dialect(packet, info['peer'])

        if self.dump_peer_info and info['peer'] not in self.dumped:
            self._dump_archive(info['data'], info['peer'], packet)

        tag, epoch, payload = protocol.split_packet(packet)
        if payload is None:
            logging.error("Ignoring a packet from %s with no payload", info['peer'])
            return

        # PeerInformation is exempt from the epoch check: it is what carries the epoch, so
        # dropping it would leave us permanently unable to learn the one we are missing.
        if not isinstance(payload, PeerInformation) and not self._epoch_allows(epoch, info['peer']):
            return

        self._dispatch(tag, payload, info['peer'])

    @objc.python_method
    def _dump_archive(self, data, peer, packet):
        """Print the raw archive of the first PeerInformation a peer sends.

        Our decoder only reads the keys we already know about, so a field SkookumLogger added and
        we have never heard of is invisible -- it simply does not appear anywhere. Reading the
        archive itself shows every key that actually travels, which is the only way to find out
        what we are failing to send back.
        """
        _, _, payload = protocol.split_packet(packet)
        if not isinstance(payload, PeerInformation):
            return
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
    def _learn_dialect(self, packet, peer):
        """Work out which generation the network speaks, from the shape of what it sends us.

        This has to be settled before we transmit anything at all, because the two generations
        disagree about how many elements a packet has and what sits in each. We stay silent until a
        peer has told us what it is by example.
        """
        dialect = 'sync2' if protocol.is_sync2_packet(packet) else 'legacy'
        if self.dialect == dialect:
            return

        if self.dialect is not None:
            # A different generation means a different SkookumLogger, and so a different log.
            # Whatever we were holding belongs to the one that went away.
            logging.warning("%s switched from the %s protocol to %s; starting over", peer, self.dialect, dialect)
            self.epoch = None
            self.log.clear()
            self.listener.log_cleared()
        else:
            logging.info("%s speaks the %s protocol; answering in kind", peer, dialect)
        self.dialect = dialect

        if self.listen_only:
            return
        if dialect == 'legacy' and not self.asked_for_legacy_log:
            self.asked_for_legacy_log = True
            self.announce_(None)
            self._request_legacy_log()
        elif dialect == 'sync2':
            self.announce_(None)

    @objc.python_method
    def _epoch_allows(self, epoch, peer):
        """Decide whether a packet's epoch lets us act on it, resetting the log if it is newer."""
        if epoch is None:
            # Either a 5.x peer, which has no epoch at all, or a 6.x peer whose log has not been
            # initialised yet. Nothing to check against.
            return True
        if self.epoch is None:
            self._adopt_epoch(epoch, peer)
            return True

        order = self.epoch.compare_(epoch)
        if order == 0:
            return True
        if order > 0:
            logging.debug("Dropping a stale packet from %s", peer)
            return False

        self._adopt_epoch(epoch, peer)
        return True

    @objc.python_method
    def _adopt_epoch(self, epoch, peer):
        """Take on a new epoch and start over, which is what a reset on any peer means."""
        if self.epoch is not None:
            logging.info("%s reset the log; starting over at epoch %s", peer, epoch)
        else:
            logging.info("Adopting the epoch %s advertised by %s", epoch, peer)
        self.epoch = epoch
        self.log.clear()
        self.listener.log_cleared()

    @objc.python_method
    def _dispatch(self, tag, payload, peer):
        """Route a payload by its class, using the tag only where the class is ambiguous."""
        if isinstance(payload, PeerInformation):
            self._handle_peer_information(payload, peer)
        elif isinstance(payload, Activity):
            logging.debug("Spot from %s: %s", peer, payload)
        elif isinstance(payload, PeerContestInfo):
            logging.debug("Contest settings from %s, ignored", peer)
        elif isinstance(payload, TransientQso):
            self._handle_legacy_qso(tag, payload)
        elif isinstance(payload, dict) or hasattr(payload, 'objectForKey_'):
            self._handle_sync(payload, peer)
        elif isinstance(payload, (list, tuple)) or hasattr(payload, 'objectAtIndex_'):
            self._handle_legacy_qso_list(tag, payload)
        elif isinstance(payload, str):
            logging.info("Message from %s: %s", peer, payload)
        else:
            logging.debug("Ignoring a %s payload from %s", type(payload).__name__, peer)

    @objc.python_method
    def _handle_peer_information(self, info, peer):
        """Record what a peer knows, and work out which generation it belongs to."""
        if str(info) != self.last_peer_info.get(peer):
            logging.info("%s", info)
            self.last_peer_info[peer] = str(info)

        if info.syncEpoch is not None:
            self._epoch_allows(info.syncEpoch, peer)

        # Deliberately not folded into our own clock. A peer's broadcast says what *it* has, and
        # claiming those events as ours would tell it we are up to date -- so it would send us
        # nothing, forever. Our clock advances only when QSOs actually arrive.
        if info.binaryVersion and self.peer_version != str(info.binaryVersion):
            self.peer_version = str(info.binaryVersion)
            logging.info("%s is running %s", peer, self.peer_version)
        if info.contestStartTime is not None:
            self.contest_start = info.contestStartTime
        if info.contestEndTime is not None:
            self.contest_end = info.contestEndTime
        if info.contestName and not self.contest:
            self.contest = str(info.contestName)

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

    @objc.python_method
    def _handle_sync(self, payload, peer):
        """Apply a SkookumNet-2 sync packet: some QSOs, maybe contest settings, maybe a clock."""
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
    def _handle_legacy_qso(self, tag, qso):
        """Apply a 5.x add or change, where the tag names the operation."""
        if tag == protocol.LEGACY_ADD_QSO:
            self._report(self.log.legacy_add(qso), qso)
        elif tag == protocol.LEGACY_CHANGE_QSO:
            self.log.legacy_add(qso)
            self._report(qsolog.UPDATED, qso)
        else:
            logging.debug("Ignoring a QSO carried by tag %s", tag)

    @objc.python_method
    def _handle_legacy_qso_list(self, tag, qsos):
        """Apply a 5.x packet carrying several QSOs at once."""
        if tag == protocol.LEGACY_DELETE_QSOS:
            for qso in qsos:
                self._report(self.log.legacy_delete(qso), qso)
            return

        if tag in (protocol.LEGACY_ALL_QSOS_RESPONSE, protocol.LEGACY_REPLACE_LOG):
            logging.info("Replacing the log with %d QSO(s)", len(qsos))
            self.log.clear()
            self.listener.log_cleared()
            for qso in qsos:
                self._report(self.log.legacy_add(qso), qso)
            return

        logging.debug("Ignoring a list carried by tag %s", tag)

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
