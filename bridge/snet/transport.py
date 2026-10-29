"""SkookumNet's transport: Bonjour discovery and one TCP connection to each other station.

Every station listens on a port the system picks and advertises it as a _skookumnet._tcp service,
whose TXT record names the station and a UUID made afresh on every join. Every station also
browses for the others. Of each pair, the station whose name sorts first opens the connection; the
other opens it itself when none exists FALLBACK_DELAY after it found the peer, which covers a pair
where only one side can see the other.

Each side starts a connection with a hello frame -- a JSON object carrying its UUID and station
name -- and the connection counts once the other side's hello is in. Should both sides connect at
once, both keep the same one of the two: the station with the larger UUID gives up its outgoing
connection and the other station its incoming one.

All sockets and Bonjour operations belong to one background thread, and the delegate's callbacks
run on it. send(), peers() and stop() may be called from any thread.
"""
import heapq
import itertools
import json
import logging
import queue
import selectors
import socket
import struct
import threading
import time
import uuid as uuidlib

from Foundation import NSString

from . import bonjour
from . import protocol

_HEADER = struct.Struct('>I')
_ORDERED_ASCENDING = -1
_TCP_RXT_CONNDROPTIME = getattr(socket, 'TCP_RXT_CONNDROPTIME', 0x80)  # from netinet/tcp.h on macOS


def frame(kind, payload):
    """Wrap a payload as one frame."""
    return _HEADER.pack(len(payload) + 1) + bytes((kind,)) + payload


def unframe(buffer):
    """Take the whole frames off the front of buffer, a bytearray, leaving a partial one in place.

    Returns ([(kind, payload), ...], intact). intact is False when a length of 0 or one above the
    limit turned up; the frames before it are still returned, and nothing after it can be trusted.
    """
    frames = []
    start = 0
    intact = True
    while len(buffer) - start >= _HEADER.size:
        (length,) = _HEADER.unpack_from(buffer, start)
        if length == 0 or length > protocol.FRAME_LIMIT:
            intact = False
            break
        body = start + _HEADER.size
        if body + length > len(buffer):
            break
        frames.append((buffer[body], bytes(buffer[body + 1:body + length])))
        start = body + length
    del buffer[:start]
    return frames, intact


def opens_connection(ours, theirs):
    """Whether the station named ours is the one to open the connection to the one named theirs.
    The name that sorts first does, compared the way SkookumLogger compares them."""
    return NSString.stringWithString_(ours).compare_(theirs) == _ORDERED_ASCENDING


def keeper(existing, arriving, our_uuid, their_uuid):
    """Pick which of two connections to the same peer survives, so that both stations pick the same.

    Two connections running the same way mean the peer reconnected before we noticed the old one
    die, so the arriving one is the live one. Running opposite ways, the station with the larger
    UUID gives up its outgoing connection and the other station its incoming one.
    """
    if existing.outgoing == arriving.outgoing:
        return arriving
    we_drop_outgoing = our_uuid.upper() > their_uuid.upper()
    return existing if arriving.outgoing == we_drop_outgoing else arriving


def read_hello(payload):
    """Return (UUID, name) from a hello, or None when it is not a JSON object carrying a valid
    UUID and a non-empty name. The UUID comes back uppercased."""
    try:
        hello = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(hello, dict):
        return None
    name = hello.get('name')
    if not isinstance(name, str) or not name:
        return None
    try:
        peer_uuid = str(uuidlib.UUID(str(hello.get('uuid')))).upper()
    except ValueError:
        return None
    return peer_uuid, name


class _Service:
    """Another station's advertisement, as last resolved."""

    def __init__(self, peer_uuid, name, host, port):
        self.uuid = peer_uuid
        self.name = name
        self.host = host
        self.port = port


class _Connection:
    """One TCP connection, from connect or accept until it closes."""

    def __init__(self, outgoing, service=None):
        self.outgoing = outgoing
        self.service = service    # where an outgoing connection is headed
        self.sock = None
        self.uuid = None          # who is on the other end, once its hello is in
        self.name = None
        self.inbox = bytearray()
        self.outbox = bytearray()
        self.closed = False

    @property
    def label(self):
        """Who this connection is with, as far as is known, for the log."""
        if self.name:
            return self.name
        if self.service is not None:
            return self.service.name
        return 'an incoming connection'


class Transport:
    """Finds the other stations, keeps a connection to each, and carries packets between them.

    The delegate receives transport_connected(name), transport_disconnected(name) and
    transport_received(name, data), all on the transport's own thread.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(self, name, delegate, service_type=protocol.SERVICE_TYPE, discover=True,
                 max_peers=protocol.MAX_PEERS):
        self.name = name
        self.uuid = str(uuidlib.uuid4()).upper()
        self.delegate = delegate
        self.service_type = service_type
        self.discover = discover      # False leaves Bonjour alone; peers come only through add_service
        self.max_peers = max_peers
        self.port = None

        self._selector = selectors.DefaultSelector()
        self._wake_read, self._wake_write = socket.socketpair()
        self._wake_read.setblocking(False)
        self._wake_write.setblocking(False)
        self._commands = queue.SimpleQueue()
        self._timers = []
        self._order = itertools.count()
        self._thread = None
        self._running = False

        self._listener = None
        self._operations = []       # our registration and our browse
        self._resolvers = {}        # service key -> resolve operation
        self._interfaces = {}       # service key -> interfaces the service is advertised on
        self._services = {}         # service key -> _Service, once resolved
        self._connections = set()   # every connection not yet closed
        self._pending = set()       # connections still waiting for the peer's hello
        self._live = {}             # peer UUID -> the connection that counts for it
        self._waiting_for = set()   # peer UUIDs with a fallback connection scheduled
        self._warned = set()        # UUIDs already warned about for sharing our name

        self._lock = threading.Lock()
        self._peer_names = []

    # --- callable from any thread ---

    def start(self):
        """Open the listener and start the transport's thread."""
        if self._thread is not None:
            return
        self._listener = self._listen()
        self.port = self._listener.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._run, name='skookumnet', daemon=True)
        self._thread.start()

    def stop(self):
        """Close every connection, withdraw our advertisement and end the thread."""
        if self._thread is None:
            return
        self._post(self._shut_down)
        self._thread.join(timeout=5)
        self._thread = None

    def send(self, data):
        """Send one packet to every connected peer."""
        if len(data) + 1 > protocol.FRAME_LIMIT:
            logging.error("SkookumNet: a packet of %d bytes does not fit in a frame; not sending it", len(data))
            return
        packet = frame(protocol.FRAME_DATA, bytes(data))
        self._post(lambda: self._send_to_live(packet))

    def peers(self):
        """The names of the stations connected right now."""
        with self._lock:
            return list(self._peer_names)

    def add_service(self, peer_uuid, name, host, port):
        """Treat a station as advertised at host:port, as if Bonjour had reported it."""
        txt = {protocol.TXT_UUID: peer_uuid, protocol.TXT_NAME: name}
        self._post(lambda: self._resolved(('added', name), host, port, txt))

    # --- the transport's thread ---

    def _post(self, command):
        self._commands.put(command)
        try:
            self._wake_write.send(b'\0')
        except (BlockingIOError, OSError):
            pass

    def _after(self, delay, callback):
        heapq.heappush(self._timers, (time.monotonic() + delay, next(self._order), callback))

    def _run(self):
        self._selector.register(self._wake_read, selectors.EVENT_READ, self._take_commands)
        self._selector.register(self._listener, selectors.EVENT_READ, self._accept)
        logging.info("SkookumNet: listening on port %d as %s", self.port, self.name)
        if self.discover:
            self._start_discovery()

        while self._running:
            timeout = max(0.0, self._timers[0][0] - time.monotonic()) if self._timers else None
            for key, mask in self._selector.select(timeout):
                self._guarded(key.data, mask)
                if not self._running:
                    break
            now = time.monotonic()
            while self._running and self._timers and self._timers[0][0] <= now:
                _, _, callback = heapq.heappop(self._timers)
                self._guarded(callback)
        self._tear_down()

    @staticmethod
    def _guarded(callback, *arguments):
        """Run one piece of work, so that a mistake in it cannot take the whole transport down."""
        try:
            callback(*arguments)
        except Exception:  # pylint: disable=broad-except
            logging.exception("SkookumNet: unexpected error")

    def _take_commands(self, _mask):
        try:
            while self._wake_read.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            self._guarded(command)

    def _shut_down(self):
        # Cleared first, so that the connections closed below neither report nor reconnect
        self._running = False

    def _tear_down(self):
        for connection in list(self._connections):
            self._close(connection)
        for operation in self._operations + list(self._resolvers.values()):
            operation.close()
        self._operations = []
        self._resolvers = {}
        self._listener.close()
        self._selector.close()
        self._wake_read.close()
        self._wake_write.close()

    def _publish_peers(self):
        with self._lock:
            self._peer_names = [connection.name for connection in self._live.values()]

    # --- Bonjour ---

    def _start_discovery(self):
        """Advertise our listener under our name and UUID, and look for everyone else."""
        txt = bonjour.txt_record({protocol.TXT_UUID: self.uuid, protocol.TXT_NAME: self.name})
        try:
            self._watch(bonjour.advertise(self.name, self.service_type, self.port, txt, self._registered))
            self._watch(bonjour.browse(self.service_type, self._browsed))
        except bonjour.BonjourError as error:
            logging.error("SkookumNet: %s", error)

    def _watch(self, operation, resolving=None):
        if resolving is None:
            self._operations.append(operation)
        else:
            self._resolvers[resolving] = operation
        self._selector.register(operation, selectors.EVENT_READ, lambda _mask: self._process(operation))

    def _unwatch(self, operation):
        try:
            self._selector.unregister(operation)
        except (KeyError, ValueError):
            pass
        operation.close()

    def _process(self, operation):
        try:
            operation.process()
        except bonjour.BonjourError as error:
            logging.error("SkookumNet: %s", error)
            self._unwatch(operation)
            if operation in self._operations:
                self._operations.remove(operation)
            for key, resolver in list(self._resolvers.items()):
                if resolver is operation:
                    del self._resolvers[key]

    def _registered(self, name):
        if name == self.name:
            logging.info("SkookumNet: advertising as %s", name)
        else:
            logging.warning("SkookumNet: another service on the network is already called %s, so "
                            "Bonjour advertises this one as %s. Station names have to be unique; "
                            "if another station uses %s, start this bridge with --name",
                            self.name, name, self.name)

    def _browsed(self, added, interface, name, domain):
        """Track a service across the interfaces it is advertised on, resolving it while it is anywhere."""
        key = (name, domain)
        interfaces = self._interfaces.setdefault(key, set())
        if added:
            interfaces.add(interface)
            if key not in self._resolvers:
                try:
                    operation = bonjour.resolve(name, self.service_type, domain,
                                                lambda host, port, txt: self._resolved(key, host, port, txt))
                except bonjour.BonjourError as error:
                    logging.error("SkookumNet: %s", error)
                    return
                self._watch(operation, resolving=key)
            return

        interfaces.discard(interface)
        if interfaces:
            return
        del self._interfaces[key]
        self._stop_resolving(key)
        gone = self._services.pop(key, None)
        if gone is not None:
            logging.info("SkookumNet: %s is no longer advertised", gone.name)

    def _stop_resolving(self, key):
        operation = self._resolvers.pop(key, None)
        if operation is not None:
            self._unwatch(operation)

    def _resolved(self, key, host, port, txt):
        """Record where a station is. The resolve stays open, so this also follows a station that
        joined again under the same name with a new UUID and port."""
        name = txt.get(protocol.TXT_NAME)
        try:
            peer_uuid = str(uuidlib.UUID(txt.get(protocol.TXT_UUID, ''))).upper()
        except ValueError:
            peer_uuid = None
        if not peer_uuid or not name:
            logging.debug("SkookumNet: ignoring %s, whose TXT record lacks a UUID or a name", key[0])
            self._services.pop(key, None)
            return
        if peer_uuid == self.uuid:
            self._stop_resolving(key)  # our own advertisement
            return
        if name == self.name and peer_uuid not in self._warned:
            self._warned.add(peer_uuid)
            logging.warning("SkookumNet: another station is also called %s. SkookumLogger tells "
                            "stations apart by name, so start this bridge with a different --name",
                            name)

        previous = self._services.get(key)
        self._services[key] = _Service(peer_uuid, name, host, port)
        if previous is None or previous.uuid != peer_uuid:
            logging.info("SkookumNet: found %s at %s:%d", name, host, port)
        self._reconsider()

    # --- who connects ---

    def _dialing(self, peer_uuid):
        return any(connection.outgoing and connection.service.uuid == peer_uuid
                   for connection in self._pending)

    def _reconsider(self):
        """Connect to each station in view that has no connection, and start the fallback clock
        for the ones whose turn it is to connect to us."""
        for service in list(self._services.values()):
            if service.uuid in self._live or self._dialing(service.uuid):
                continue
            if len(self._live) >= self.max_peers:
                logging.debug("SkookumNet: not connecting to %s; %d peers are connected already",
                              service.name, len(self._live))
            elif opens_connection(self.name, service.name):
                self._dial(service)
            elif service.uuid not in self._waiting_for:
                logging.info("SkookumNet: waiting for %s to connect", service.name)
                self._waiting_for.add(service.uuid)
                self._after(protocol.FALLBACK_DELAY, lambda peer_uuid=service.uuid: self._fall_back(peer_uuid))

    def _fall_back(self, peer_uuid):
        """Connect after all, if the station that should have connected to us has not."""
        self._waiting_for.discard(peer_uuid)
        if peer_uuid in self._live or self._dialing(peer_uuid) or len(self._live) >= self.max_peers:
            return
        for service in self._services.values():
            if service.uuid == peer_uuid:
                logging.info("SkookumNet: %s has not connected; connecting to it instead", service.name)
                self._dial(service)
                return

    def _dial(self, service):
        """Open a connection to a station. The lookup and the connect happen on a thread of their
        own so a slow host cannot hold up everything else."""
        connection = _Connection(outgoing=True, service=service)
        self._connections.add(connection)
        self._pending.add(connection)
        self._after(protocol.HELLO_TIMEOUT, lambda: self._hello_overdue(connection))
        logging.info("SkookumNet: connecting to %s", service.name)

        def connect():
            try:
                sock = socket.create_connection((service.host, service.port), timeout=protocol.HELLO_TIMEOUT)
            except OSError as error:
                # Bound now: Python unbinds the name once the except clause ends
                self._post(lambda error=error: self._unreachable(connection, error))
                return
            self._post(lambda: self._connected(connection, sock))

        threading.Thread(target=connect, name=f'connect to {service.name}', daemon=True).start()

    def _unreachable(self, connection, error):
        logging.info("SkookumNet: could not connect to %s: %s", connection.label, error)
        self._close(connection)

    def _connected(self, connection, sock):
        if connection.closed or not self._running:
            sock.close()
            return
        self._attach(connection, sock)

    def _accept(self, _mask):
        while True:
            try:
                sock, _ = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as error:
                logging.warning("SkookumNet: could not accept a connection: %s", error)
                return
            connection = _Connection(outgoing=False)
            self._connections.add(connection)
            self._pending.add(connection)
            self._after(protocol.HELLO_TIMEOUT, lambda connection=connection: self._hello_overdue(connection))
            self._attach(connection, sock)

    # --- connections ---

    @staticmethod
    def _listen():
        """A listener on a port the system picks, taking IPv6 and IPv4 alike where it can."""
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                sock.bind(('::', 0))
            except OSError:
                sock.close()
                raise
        except OSError:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('0.0.0.0', 0))
        sock.listen(16)
        sock.setblocking(False)
        return sock

    @staticmethod
    def _tune(sock):
        """Set the TCP options SkookumLogger uses: no delay, keepalives, and a drop time."""
        sock.setblocking(False)
        for level, option, value in (
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPALIVE', None), protocol.KEEPALIVE_IDLE),
                (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPINTVL', None), protocol.KEEPALIVE_INTERVAL),
                (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPCNT', None), protocol.KEEPALIVE_COUNT),
                (socket.IPPROTO_TCP, _TCP_RXT_CONNDROPTIME, protocol.CONNECTION_DROP_TIME)):
            if option is None:
                continue
            try:
                sock.setsockopt(level, option, value)
            except OSError as error:
                logging.debug("SkookumNet: could not set TCP option %s: %s", option, error)

    def _attach(self, connection, sock):
        self._tune(sock)
        connection.sock = sock
        self._selector.register(sock, selectors.EVENT_READ, lambda mask: self._io(connection, mask))
        hello = {'version': protocol.HELLO_VERSION, 'uuid': self.uuid, 'name': self.name}
        self._write(connection, frame(protocol.FRAME_HELLO, json.dumps(hello).encode('utf-8')))

    def _hello_overdue(self, connection):
        if connection.uuid is None and not connection.closed:
            logging.info("SkookumNet: no hello from %s within %.0f seconds; closing the connection",
                         connection.label, protocol.HELLO_TIMEOUT)
            self._close(connection)

    def _close(self, connection):
        """Close a connection and, when it was the one that counted for its peer, report the peer
        gone and look again in a moment for stations to connect to. Safe to call more than once."""
        if connection.closed:
            return
        connection.closed = True
        self._connections.discard(connection)
        self._pending.discard(connection)
        if connection.sock is not None:
            try:
                self._selector.unregister(connection.sock)
            except (KeyError, ValueError):
                pass
            connection.sock.close()
        if connection.uuid is not None and self._live.get(connection.uuid) is connection:
            del self._live[connection.uuid]
            self._publish_peers()
            if self._running:
                logging.info("SkookumNet: %s is disconnected; %d peer(s) connected",
                             connection.name, len(self._live))
                self.delegate.transport_disconnected(connection.name)
        if self._running:
            self._after(protocol.RECONNECT_DELAY, self._reconsider)

    def _io(self, connection, mask):
        if mask & selectors.EVENT_WRITE and not connection.closed:
            self._flush(connection)
        if mask & selectors.EVENT_READ and not connection.closed:
            self._receive(connection)

    def _receive(self, connection):
        try:
            chunk = connection.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as error:
            logging.info("SkookumNet: lost the connection to %s: %s", connection.label, error)
            self._close(connection)
            return
        if not chunk:
            logging.info("SkookumNet: %s closed the connection", connection.label)
            self._close(connection)
            return
        connection.inbox.extend(chunk)
        frames, intact = unframe(connection.inbox)
        for kind, payload in frames:
            if connection.closed:
                return
            self._take_frame(connection, kind, payload)
        if not intact and not connection.closed:
            logging.warning("SkookumNet: a malformed frame from %s; closing the connection", connection.label)
            self._close(connection)

    def _take_frame(self, connection, kind, payload):
        if kind == protocol.FRAME_HELLO:
            self._take_hello(connection, payload)
        elif kind == protocol.FRAME_DATA:
            # A connection that lost to a duplicate is still read until it closes: the peer sent
            # these before it settled on the other one
            if connection.uuid is None:
                logging.warning("SkookumNet: data from %s before its hello; dropped", connection.label)
                return
            self.delegate.transport_received(connection.name, payload)
        else:
            logging.debug("SkookumNet: ignoring a frame of kind %d from %s", kind, connection.label)

    def _take_hello(self, connection, payload):
        """Find out who is on the other end, and settle which connection counts for that peer."""
        if connection.uuid is not None:
            logging.warning("SkookumNet: a second hello from %s; ignored", connection.label)
            return
        identity = read_hello(payload)
        if identity is None:
            logging.warning("SkookumNet: a malformed hello from %s; closing the connection", connection.label)
            self._close(connection)
            return
        peer_uuid, name = identity
        if peer_uuid == self.uuid:
            self._close(connection)  # we reached our own listener through our own advertisement
            return
        connection.uuid = peer_uuid
        connection.name = name
        self._pending.discard(connection)

        existing = self._live.get(peer_uuid)
        if existing is not None:
            kept = keeper(existing, connection, self.uuid, peer_uuid)
            dropped = existing if kept is connection else connection
            self._live[peer_uuid] = kept
            logging.info("SkookumNet: two connections to %s; keeping the %s one",
                         name, 'outgoing' if kept.outgoing else 'incoming')
            self._after(protocol.DUPLICATE_LINGER, lambda: self._close(dropped))
            return
        if len(self._live) >= self.max_peers:
            logging.info("SkookumNet: closing the connection from %s; %d peers are connected already",
                         name, len(self._live))
            self._close(connection)
            return
        self._live[peer_uuid] = connection
        self._publish_peers()
        logging.info("SkookumNet: %s is connected; %d peer(s) connected", name, len(self._live))
        self.delegate.transport_connected(name)

    def _write(self, connection, data):
        if connection.closed or connection.sock is None:
            return
        connection.outbox.extend(data)
        self._flush(connection)

    def _flush(self, connection):
        """Send what the socket takes now, and watch for room while anything is left over."""
        try:
            sent = connection.sock.send(connection.outbox)
            del connection.outbox[:sent]
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as error:
            logging.info("SkookumNet: could not send to %s: %s", connection.label, error)
            self._close(connection)
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if connection.outbox else 0)
        self._selector.modify(connection.sock, events, lambda mask: self._io(connection, mask))

    def _send_to_live(self, packet):
        for connection in list(self._live.values()):
            self._write(connection, packet)
