#!/usr/bin/env python3
"""Bridges a SkookumLogger session to BicHok's analyser page.

Run this alongside SkookumLogger, join SkookumNet in SkookumLogger, and open the analyser page.
QSOs then appear in the page as they are logged.

This is a listening peer. It advertises itself so SkookumLogger can invite it and broadcasts its
own state so it takes part in the sync properly, but it never sends anything that could change
another station's log.
"""
import argparse
import logging
import os
import sys

from PyObjCTools import AppHelper
from MultipeerConnectivity import MCSession, MCPeerID, MCNearbyServiceAdvertiser, MCEncryptionNone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from snet import protocol                                    # noqa: E402  pylint: disable=wrong-import-position
from snet.session import SkookumNetPeer                      # noqa: E402  pylint: disable=wrong-import-position
from snet.webbridge import WebBridge, DEFAULT_HOST, DEFAULT_PORT  # noqa: E402  pylint: disable=wrong-import-position

DEFAULT_LOG = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'bichoc-bridge.log')

# Deliberately not the machine's station name: SkookumLogger derives that from the host name, and
# this bridge usually runs on the same Mac, so reusing it would collide with SkookumLogger itself.
DEFAULT_PEER_NAME = 'BicHok'

# Sent as binaryVersion. SkookumLogger only sends a peer the log it is missing when this matches
# its own identifier exactly, down to the UUID of its binary, so nothing we can honestly call
# ourselves will earn a fill. We say who we are anyway: it is better than a blank in its peer
# table, it costs nothing, and it is already the right answer if that check is ever relaxed.
BRIDGE_VERSION = 'BicHok-1.0'


def parse_arguments():
    """Read the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--name', default=DEFAULT_PEER_NAME,
                        help='name to show in SkookumLogger')
    parser.add_argument('--host', default=DEFAULT_HOST, help='address to serve the browser on')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help='port to serve the browser on')
    parser.add_argument('--service', default=protocol.SERVICE_TYPE,
                        help='Bonjour service type to advertise (both the final 5.x release and 6.0 use the default)')
    parser.add_argument('--listen-only', action='store_true',
                        help='never transmit anything, so nothing this bridge does can upset SkookumLogger')
    parser.add_argument('--binary-version', default=BRIDGE_VERSION, metavar='NAME',
                        help='how this client identifies itself to peers (see README)')
    parser.add_argument('--sync-existing-log', dest='sync_existing_log', action='store_true', default=True,
                        help='receive the log from before this bridge started (the default)')
    parser.add_argument('--no-sync-existing-log', dest='sync_existing_log', action='store_false',
                        help='do not fetch the log from before this bridge started (see README)')
    parser.add_argument('--dump-peer-info', action='store_true',
                        help='print the raw archive of the first PeerInformation each peer sends')
    parser.add_argument('--logfile', default=DEFAULT_LOG, help='where to write the log')
    parser.add_argument('--debug', action='store_true', help='log every packet')
    return parser.parse_args()


def configure_logging(arguments):
    """Send logging to a fresh file and to the terminal."""
    handlers = [logging.FileHandler(arguments.logfile, mode='w', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)]
    logging.basicConfig(level=logging.DEBUG if arguments.debug else logging.INFO,
                        format='%(asctime)s %(levelname)s: %(message)s',
                        handlers=handlers)


def main():
    """Start the browser bridge and the SkookumNet peer, then run until interrupted."""
    arguments = parse_arguments()
    configure_logging(arguments)

    name = arguments.name
    logging.info("Joining SkookumNet as %s, advertising _%s._tcp", name, arguments.service)

    bridge = WebBridge(arguments.host, arguments.port)
    bridge.start()

    peerID = MCPeerID.alloc().initWithDisplayName_(name)
    session = MCSession.alloc().initWithPeer_securityIdentity_encryptionPreference_(
        peerID, None, MCEncryptionNone)
    peer = SkookumNetPeer.alloc().initWithSession_peerID_listener_(session, peerID, bridge)
    peer.listen_only = arguments.listen_only
    peer.dump_peer_info = arguments.dump_peer_info
    peer.binary_version = arguments.binary_version
    peer.sync_existing_log = arguments.sync_existing_log
    if not arguments.sync_existing_log:
        logging.info("Not fetching the log from before startup; only QSOs logged from now on will arrive.")
    if arguments.listen_only:
        logging.warning("Listening only: nothing will be transmitted, so this bridge will not appear "
                        "in SkookumLogger's peer table and a 5.x peer will not be asked for its log")
    session.setDelegate_(peer)

    advertiser = MCNearbyServiceAdvertiser.alloc().initWithPeer_discoveryInfo_serviceType_(
        peerID, None, arguments.service)
    advertiser.setDelegate_(peer)
    advertiser.startAdvertisingPeer()
    peer.start()

    try:
        AppHelper.runConsoleEventLoop(installInterrupt=True)
    finally:
        peer.stop()
        advertiser.stopAdvertisingPeer()
        session.disconnect()
        bridge.stop()
        logging.info("Left SkookumNet")


if __name__ == '__main__':
    main()
