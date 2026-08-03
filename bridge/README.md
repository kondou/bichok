# BicHok SkookumNet bridge

Feeds a live SkookumLogger log into BicHok's analyser page. Run it next to SkookumLogger, join
SkookumNet there, and open the page: QSOs appear as they are logged.

This is a listening peer. It advertises itself so SkookumLogger can invite it, and broadcasts its
own state so it takes part in the sync properly, but it never sends anything that can change
another station's log.

## Running it

    uv run --python 3.14 \
      --with pyobjc-framework-MultipeerConnectivity --with websockets \
      python bichok_bridge.py

Then point the analyser page at `ws://localhost:2237`.

`uv` is the easy route because the Python that ships with macOS is too old for current PyObjC
wheels and tries to build them from source. Any Python 3.13 or newer with
`pyobjc-framework-MultipeerConnectivity` and `websockets` installed will do just as well, in which
case `python3 bichok_bridge.py` is enough.

    --name NAME             how the bridge appears in SkookumLogger (default: BicHok)
    --host HOST             address to serve the browser on (default: localhost)
    --port PORT             port to serve the browser on (default: 2237)
    --service TYPE          Bonjour service type to advertise (default: skookumnetwork)
    --binary-version NAME    how the bridge identifies itself to peers
    --no-sync-existing-log   do not fetch the log from before the bridge started (see below)
    --logfile PATH          where to write the log
    --debug                 log every packet

SkookumLogger has to be told to join: open its SkookumNet window and click Join. Until then it
advertises nothing and the bridge has nobody to talk to.

## Which protocol it speaks

Two generations of SkookumNet exist and both are handled.

SkookumLogger 5.x announces each change as it happens, and a client asks for the log with
`RequestAllQsos`. SkookumNet-2, in SkookumLogger 6.x, replaces that with vector clocks: each
station stamps its QSOs with a count of the events it knows about, peers exchange those counts,
and whichever side is behind gets sent what it is missing. Joining is enough to receive everything,
so there is nothing to request. Deletions became a flag on the QSO rather than a removal, and a
QSO is identified by its station name and sequence number rather than by a UUID.

The two generations frame their packets differently — `[tag, payload]` against
`[tag, epoch, payload]` — and tag numbers 5 and 6 changed meaning between them, so the tag alone
cannot say which arrived. Everything here identifies a packet by the class of its payload and
treats the tag as a hint.

**The bridge stays silent until a peer has shown it which generation to answer in.** A SkookumNet-2
peer expects three elements and takes the payload from the third, so a two-element packet is not
something it can act on, and guessing wrong is worth avoiding. Waiting costs at most five seconds,
since that is how often SkookumLogger broadcasts its own state, and it removes the guesswork
entirely. For the same reason a packet sent to a SkookumNet-2 peer always carries three elements,
with `NSNull` standing in for an epoch not yet known.

## Catching up on a log already in progress

The bridge sees every QSO logged from the moment it connects, and not the ones logged before that.
SkookumLogger sends a peer the events it is missing only when that peer's `binaryVersion` matches
its own exactly, down to the UUID of the SkookumLogger binary, which is not something a client from
outside can produce.

So, by default, the bridge does what the example client published by the SkookumNet author does:
it adopts the first peer's identifier and returns it. That is enough to be sent the whole log
immediately, and it keeps working across SkookumLogger updates because the identifier is read from
the peer at connection time rather than written down here. The bridge also computes the same log
hash SkookumLogger compares peers with, so once the log has arrived, SkookumLogger lists this
bridge as a peer in good standing — a white row, no warnings. What remains:

* SkookumLogger records this bridge in its log's vector clock, as an entry at zero. It affects
  nothing and the reset button in the SkookumNet window clears it.
* The Station column reads BicHok throughout. Only the hidden version field, visible in the row's
  tooltip, carries the peer's identifier.

`--no-sync-existing-log` turns this off; the bridge then identifies itself as itself and sees only
the QSOs logged after it connected. Either way, a log exported from SkookumLogger can be loaded
into the analyser page at any time — the page merges a loaded file and the live feed in either
order.

## Epochs

An epoch marks a generation of the log. Only SkookumLogger creates one, through the reset button
in its SkookumNet window, and that is needed once per log. The bridge never invents an epoch: it
adopts whichever it observes and echoes that back. A packet stamped with an older epoch is stale
and ignored; a newer one means somebody reset, so the bridge clears its log and starts again.

## When the session goes quiet

A session can lose its peer without a disconnect event ever arriving. PeerInformation comes every
five seconds while a session is alive, so once thirty seconds pass with a peer connected and
nothing received, the bridge concludes the session is dead and replaces it. Advertising never
stopped, so SkookumLogger invites the new session on its own; the log is kept across the swap,
and whatever was missed in the gap arrives as a fill.

## Licence

MIT — see LICENSE. Copyright (c) 2026 Katsuhiro Kondou, JH5GHM (JE6RPM).

Written from the SkookumNet wire format as observed on the network, and from the published
SkookumNetAutomerge article. It shares no code with any other SkookumNet client.

## Layout

    bichok_bridge.py   start-up and command line
    selftest.py        checks the merge rules against the published examples
    snet/protocol.py   tags, flag bits, packet framing
    snet/objects.py    the objects SkookumLogger puts on the wire
    snet/clock.py      vector clocks
    snet/log.py        the QSO log and the merge rules
    snet/session.py    discovery, decoding, and the sync state machine
    snet/webbridge.py  the WebSocket server the page connects to

`python3 selftest.py` needs no network and no SkookumLogger.
