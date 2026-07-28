"""Relays SkookumNet QSOs to the browser over a WebSocket.

BicHoc's analyser page connects here and receives the log as JSON. The message shapes below are
the contract with that page, so they change only when the page changes.

The QSOs are cached as they arrive so a browser that connects late, or reloads, gets the current
state replayed to it. Nothing is ever asked of SkookumLogger to rebuild that state: SkookumNet-2
has no request for it, and a peer joining is sent everything anyway.
"""
import asyncio
import json
import logging
import threading

try:
    import websockets
except ImportError:
    websockets = None

DEFAULT_HOST = 'localhost'
DEFAULT_PORT = 2237

_BANDS = (
    (1800, 2000, '160'), (3500, 4000, '80'), (5250, 5450, '60'), (7000, 7300, '40'),
    (10100, 10150, '30'), (14000, 14350, '20'), (18068, 18168, '17'), (21000, 21450, '15'),
    (24890, 24990, '12'), (28000, 29700, '10'), (50000, 54000, '6'), (70000, 70500, '4'),
    (144000, 148000, '2'), (420000, 450000, '70cm'), (902000, 928000, '33cm'),
    (1240000, 1300000, '23cm'), (2300000, 2450000, '13cm'), (3300000, 3500000, '9cm'),
    (5650000, 5925000, '6cm'),
)

_EXCHANGE_KEYS = ('call', 'report', 'serial', 'zone', 'grid', 'name',
                  'check', 'precedence', 'power', 'info')


def band_for(frequency_hz):
    """Name the band a frequency falls in, using the labels the analyser page expects."""
    khz = frequency_hz / 1000.0
    for low, high, name in _BANDS:
        if low <= khz <= high:
            return name
    return 'other'


def _exchange_as_dict(exchange):
    """Flatten an Exchange to the subset of fields the page displays."""
    if exchange is None:
        return {}
    values = exchange.as_dict()
    return {key: values.get(key, '') for key in _EXCHANGE_KEYS}


def qso_as_dict(qso):
    """Turn a QSO into the JSON the analyser page reads.

    The 'id' is what the page uses to match an update or a deletion against a QSO it already has.
    SkookumNet-2 identifies a QSO by (stationName, sequenceID), but the UUID is still carried and
    is what the page has always keyed on, so it stays the id where one is present.
    """
    frequency = int(qso.transmitFrequency) if qso.transmitFrequency else 0
    station = str(qso.stationName or '')

    if qso.identifier:
        identity = str(qso.identifier)
    elif qso.sequenceID is not None:
        identity = f'{station}#{int(qso.sequenceID)}'
    else:
        identity = ''

    return {
        'id': identity,
        'timeMs': int(qso.timeStamp.timeIntervalSince1970() * 1000) if qso.timeStamp else 0,
        'freqHz': frequency,
        'freqKhz': frequency / 1000.0,
        'band': band_for(frequency),
        'mode': str(qso.mode or ''),
        'sent': _exchange_as_dict(qso.sentExchange),
        'received': _exchange_as_dict(qso.receivedExchange),
        'operator': str(qso.operatorCall or ''),
        'station': station,
        'notes': str(qso.notes or ''),
    }


class WebBridge:
    """Serves the log to browsers, and receives the SkookumNet callbacks that keep it current."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.contest = ''
        self.clients = set()
        self.cache = {}
        self.loop = None
        self.thread = None

    # --- lifecycle ---

    def start(self):
        """Run the server on its own thread, alongside the Cocoa run loop the peer needs."""
        if websockets is None:
            logging.error("The websockets package is missing; the browser bridge will not run")
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._serve_forever, daemon=True, name='webbridge')
            self.thread.start()

    def stop(self):
        """Stop serving."""
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    # --- SkookumNet callbacks ---

    def session_started(self, contest):
        """A peer joined the network."""
        self.contest = contest or ''
        self._broadcast({'type': 'startup', 'contest': self.contest})

    def session_ended(self):
        """The last peer left."""
        self._broadcast({'type': 'shutdown', 'contest': self.contest})

    def log_cleared(self):
        """The log was reset, either by an epoch change or by a 5.x peer replacing it."""
        logging.info("Clearing the log")
        self.cache.clear()
        self._broadcast({'type': 'clearQSOs', 'contest': self.contest})

    def qso_added(self, qso):
        """A QSO appeared."""
        self._store_and_send('addQSO', qso)

    def qso_updated(self, qso):
        """A QSO changed."""
        self._store_and_send('updateQSO', qso)

    def qso_deleted(self, qso):
        """A QSO was deleted. Under SkookumNet-2 it is still in the log, flagged as a tombstone."""
        payload = qso_as_dict(qso)
        self.cache.pop(payload['id'], None)
        self._broadcast({'type': 'deleteQSO', 'contest': self.contest, 'qso': payload})

    def qso_conflicted(self, qso):
        """Two peers edited the same QSO in ways that cannot be reconciled automatically."""
        logging.warning("Conflicting edits on %s; showing the copy we already had", qso_as_dict(qso)['id'])

    def peer_information(self, info):
        """A peer described itself. Nothing on the page uses this yet."""

    # --- internals ---

    def _store_and_send(self, kind, qso):
        """Cache a QSO under its id and push it to every browser."""
        payload = qso_as_dict(qso)
        if payload['id']:
            self.cache[payload['id']] = payload
        self._broadcast({'type': kind, 'contest': self.contest, 'qso': payload})

    def _broadcast(self, message):
        """Send a message to every browser. Safe to call from the Cocoa thread."""
        if self.loop is None or not self.loop.is_running() or not self.clients:
            return
        text = json.dumps(message, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self._send_to_all(text), self.loop)

    async def _send_to_all(self, text):
        """Write to every browser, dropping the ones that have gone away."""
        gone = set()
        for client in list(self.clients):
            try:
                await client.send(text)
            except Exception:  # pylint: disable=broad-except
                gone.add(client)
        self.clients -= gone

    async def _handle_client(self, websocket, path=None):
        """Greet a browser, replay what we have, then hold the connection open."""
        self.clients.add(websocket)
        logging.info("A browser connected; %d now attached", len(self.clients))
        try:
            await websocket.send(json.dumps({'type': 'connected', 'contest': self.contest}))
            if self.cache:
                await websocket.send(json.dumps({'type': 'clearQSOs', 'contest': self.contest}))
                for payload in list(self.cache.values()):
                    await websocket.send(json.dumps({'type': 'addQSO', 'contest': self.contest, 'qso': payload}))
                logging.info("Replayed %d QSO(s) to the new browser", len(self.cache))
            async for _ in websocket:
                pass  # the page never sends us anything
        except Exception:  # pylint: disable=broad-except
            pass
        finally:
            self.clients.discard(websocket)
            logging.info("A browser disconnected; %d still attached", len(self.clients))

    def _serve_forever(self):
        """Serve until stopped, retrying the bind so a restart does not need the port to be free yet."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def serve():
            delay = 2
            while True:
                try:
                    async with websockets.serve(self._handle_client, self.host, self.port, reuse_port=True):
                        logging.info("Serving the log on ws://%s:%d", self.host, self.port)
                        delay = 2
                        await asyncio.Future()
                except asyncio.CancelledError:
                    break
                except OSError as error:
                    logging.warning("Could not bind ws://%s:%d (%s); retrying in %ds",
                                    self.host, self.port, error, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
                except Exception as error:  # pylint: disable=broad-except
                    logging.error("The browser bridge stopped: %s", error)
                    break

        try:
            self.loop.run_until_complete(serve())
        except Exception as error:  # pylint: disable=broad-except
            logging.error("The browser bridge event loop failed: %s", error)
