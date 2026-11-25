#!/usr/bin/env python3
"""Bridges a SkookumLogger session to BicHok's analyser page.

Run this alongside SkookumLogger, join SkookumNet in SkookumLogger, and open the analyser page.
QSOs then appear in the page as they are logged.

This is a listening peer. It finds SkookumLogger on the network and connects to it, and
broadcasts its own state so it takes part in the sync properly, but it never sends anything that
could change another station's log.
"""
import argparse
import logging
import os
import re
import signal
import sys

from PyObjCTools import AppHelper, MachSignals

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from snet import protocol                                    # noqa: E402  pylint: disable=wrong-import-position
from snet.session import SkookumNetPeer                      # noqa: E402  pylint: disable=wrong-import-position
from snet.transport import Transport                         # noqa: E402  pylint: disable=wrong-import-position
from snet.webbridge import WebBridge, DEFAULT_HOST, DEFAULT_PORT, issue_token  # noqa: E402  pylint: disable=wrong-import-position

DEFAULT_LOG = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'bichok-bridge.log')

# Where the analyser page lives: the folder above this one. The browser token is left there.
PAGE_FOLDER = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# The page carries the release's version number, and nothing else does: the bridge reads it from
# there rather than keeping a copy that a release could forget to bump.
PAGE_FILE = 'contest_log_analyzer.html'
_VERSION_LINE = re.compile(r"\s*const APP_VERSION = '([^']+)';")

# The ways a bridge is told to stop: Ctrl-C in its window, start_skookumnet.command's pkill
# making room for another bridge, and its window being closed.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

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
                        help=f'Bonjour service type to advertise and look for (default: {protocol.SERVICE_TYPE})')
    parser.add_argument('--listen-only', action='store_true',
                        help='never send a packet, so nothing this bridge does can upset SkookumLogger')
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


def shipped_version(folder):
    """The BicHok release in `folder`, read from its page; None when the page is not there."""
    try:
        with open(os.path.join(folder, PAGE_FILE), encoding='utf-8') as page:
            for line in page:
                match = _VERSION_LINE.match(line)
                if match:
                    return match.group(1)
    except OSError:
        pass
    return None


def stop_on_signals():
    """Make every one of STOP_SIGNALS end the run loop the way Ctrl-C does, and log which came.

    SIGTERM and SIGHUP would otherwise end the process on the spot, leaving no word in the log
    that the bridge had stopped at all, let alone why -- and a bridge from another copy of BicHok
    can be stopped that way by the launcher without its operator noticing. Handled here, each
    goes out through main()'s cleanup like Ctrl-C. The handlers run on the Cocoa run loop (a
    plain Python handler does not reliably get to run while the loop sits in Cocoa), and a
    signal arriving during the cleanup that follows waits there unanswered instead of cutting
    it short.
    """
    stopping = []

    def stop(signum):
        if not stopping:
            stopping.append(signum)
            logging.info("Stopping on %s", signal.Signals(signum).name)
        AppHelper.stopEventLoop()

    for signum in STOP_SIGNALS:
        MachSignals.signal(signum, stop)


def main():
    """Start the browser bridge and the SkookumNet peer, then run until told to stop."""
    arguments = parse_arguments()
    configure_logging(arguments)
    # First, so that any log says which copy of BicHok wrote it. The folder is the one it was
    # started from; renaming it later does not change this line.
    logging.info("BicHok %s bridge starting in %s (pid %d)",
                 shipped_version(PAGE_FOLDER) or '(version unknown)', PAGE_FOLDER, os.getpid())

    name = arguments.name
    logging.info("Joining SkookumNet as %s on %s", name, arguments.service)

    bridge = WebBridge(arguments.host, arguments.port, issue_token(PAGE_FOLDER))
    bridge.start()

    peer = SkookumNetPeer.alloc().initWithStation_listener_(name, bridge)
    peer.listen_only = arguments.listen_only
    peer.dump_peer_info = arguments.dump_peer_info
    peer.binary_version = arguments.binary_version
    peer.sync_existing_log = arguments.sync_existing_log
    if not arguments.sync_existing_log:
        logging.info("Not fetching the log from before startup; only QSOs logged from now on will arrive.")
    if arguments.listen_only:
        logging.warning("Listening only: no packet will be sent, so the log from before startup will not arrive")

    transport = Transport(name, peer, service_type=arguments.service)
    peer.transport = transport
    transport.start()

    stop_on_signals()
    try:
        AppHelper.runConsoleEventLoop()
    finally:
        transport.stop()
        bridge.stop()
        logging.info("Left SkookumNet")


if __name__ == '__main__':
    main()
