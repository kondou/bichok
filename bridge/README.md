# BicHok SkookumNet bridge

Feeds a live SkookumLogger log into BicHok's analyser page. Run it next to SkookumLogger, join
SkookumNet there, and open the page: QSOs appear as they are logged.

This is a listening peer. It finds SkookumLogger on the network and connects to it, and broadcasts
its own state so it takes part in the sync properly, but it never sends anything that can change
another station's log.

It needs SkookumLogger 6.1(16) or later. Earlier releases ran SkookumNet over Multipeer
Connectivity, which this bridge no longer speaks; BicHok 1.0.18 is the last release that does.

## Running it

    uv run --python 3.14 \
      --with pyobjc-framework-Cocoa --with websockets \
      python bichok_bridge.py

Then point the analyser page at `ws://localhost:2237`.

The bridge serves only a browser that presents its token. Each start writes a fresh one to
`bridge_token.js` in the folder above this one, where the analyser page sits; the page loads it
from there and connects to `ws://localhost:2237/?t=TOKEN`. A web site open in the same browser can
reach the port but cannot read that file, so it is turned away. Keep the page and the `bridge`
folder together for this to work.

`uv` is the easy route because the Python that ships with macOS is too old for current PyObjC
wheels and tries to build them from source. Any Python 3.13 or newer with
`pyobjc-framework-Cocoa` and `websockets` installed will do just as well, in which
case `python3 bichok_bridge.py` is enough.

    --name NAME             how the bridge appears in SkookumLogger (default: BicHok)
    --host HOST             address to serve the browser on (default: localhost)
    --port PORT             port to serve the browser on (default: 2237)
    --service TYPE          Bonjour service type to advertise and look for (default: _skookumnet._tcp)
    --binary-version NAME    how the bridge identifies itself to peers
    --no-sync-existing-log   do not fetch the log from before the bridge started (see below)
    --logfile PATH          where to write the log
    --debug                 log every packet

SkookumLogger has to be told to join: open its SkookumNet window and click Join. Until then it
advertises nothing and the bridge has nobody to talk to.

## How it connects

SkookumNet runs over plain TCP. Each station advertises a listener over Bonjour as
`_skookumnet._tcp`, with its station name and a UUID in the TXT record, and looks for everyone
else's. Of each pair of stations, the one whose name sorts first opens the connection; the other
opens it itself if none exists ten seconds after it found the peer, which covers a pair where only
one side can see the other. The bridge plays both parts, so it reaches SkookumLogger whichever way
the names sort.

Every message on a connection is a frame — a length, a kind and a payload — and each side opens
with a hello naming its UUID and station name. Should both sides connect at once, both keep the
same one of the two connections. TCP keepalives notice a station that vanished without closing its
connection after about sixteen seconds, and once a connection ends the bridge looks again a couple
of seconds later. Inside the frames travels a packet, `[tag, epoch, payload]`, with `NSNull`
standing in for an epoch not yet known.

Station names have to be unique on the network, because SkookumLogger tells its peers apart by
name. If another station is already called BicHok, the bridge says so in its log; start it with
`--name`.

macOS asks before an app may use the local network. If the app the bridge runs in — Terminal, for
instance — has been refused, the log says that local network access was denied; allow it under
System Settings › Privacy & Security › Local Network.

A session starts once a peer's PeerInformation names the contest, and ends when the last peer
leaves. Each start sends the page everything the bridge holds again, since the page clears its live
QSOs on a start: a SkookumLogger that restarts, or a connection that drops and comes back, leaves
the page as it was.

## Catching up on a log already in progress

The bridge sees every QSO logged from the moment it connects, and not the ones logged before that.
SkookumLogger sends a peer the events it is missing only when that peer's `binaryVersion` matches
its own exactly, down to the UUID of the SkookumLogger binary, which is not something a client from
outside can produce.

So, by default, the bridge does what the example client published by the SkookumNet author does:
it adopts the peer's identifier and returns it — the log owner's once that is known, the first
peer's until then. That is enough to be sent the whole log
immediately, and it keeps working across SkookumLogger updates because the identifier is read from
the peer at connection time rather than written down here. The bridge also computes the same log
hash SkookumLogger compares peers with, so once the log has arrived, SkookumLogger lists this
bridge as a peer in good standing — a white row, no warnings. What remains:

* SkookumLogger records this bridge in its log's vector clock, as an entry at zero. It affects
  nothing and the ◁ button at the top right of the SkookumNet window clears it.
* The Station column reads BicHok throughout. Only the hidden version field, visible in the row's
  tooltip, carries the peer's identifier.

`--no-sync-existing-log` turns this off; the bridge then identifies itself as itself and sees only
the QSOs logged after it connected. Either way, a log exported from SkookumLogger can be loaded
into the analyser page at any time — the page merges a loaded file and the live feed in either
order.

## Epochs

An epoch marks a generation of the log. Only SkookumLogger creates one, through the ◁ button at the
top right of its SkookumNet window, and that is needed once per log. The bridge never invents an
epoch: it adopts what it observes and echoes that back.

Whose observation counts is the fine print. The epoch follows the log's *owner* — the peer that
sends sync packets, which only a logger ever does, QSOs aboard or not — and follows it in both
directions, because replacing the log file on the owner rolls its epoch back. A peer that keeps echoing the newest epoch it ever saw reads to
SkookumLogger as a request to reset the log, and confirming that dialog erases the log for real.
Until an owner is known, newer epochs are adopted from anyone — a freshly reset SkookumLogger has
no QSOs to send yet, so insisting on an owner there would wait forever. Once one is known, other
peers' epochs are echoes of the past and are ignored. If the owner stops advertising an epoch
altogether — a brand-new log has no reset in its history — the bridge drops its own epoch and its
QSOs and starts over with it. Packets whose epoch was not accepted by these rules are dropped,
and a packet with no epoch at all is acted on only if it is a PeerInformation.

For the same reason the bridge announces itself as a *reply* to the owner's advertisement rather
than on a timer of its own: each announcement then leaves moments after taking in the owner's
current state, so a stale epoch of ours is on the wire for milliseconds rather than a five-second
window. While no owner is known every peer's advertisement is answered, since announcing our clock
is what makes somebody send a fill. SkookumLogger advertises every five seconds, so the effective
rate is unchanged.

## Licence

MIT — see LICENSE. Copyright (c) 2026 Katsuhiro Kondou, JH5GHM (JE6RPM).

Written from the SkookumNet wire format as observed on the network, and from the SkookumNetWireProtocol
and SkookumNetAutomerge articles published by the SkookumNet author. It shares no code with any
other SkookumNet client.

## Layout

    bichok_bridge.py    start-up and command line
    selftest.py         checks the merge rules against the published examples, and the transport
    snet/protocol.py    constants, tags, flag bits, packet layout
    snet/bonjour.py     advertising and finding stations through the system DNS-SD library
    snet/transport.py   the TCP connections, their frames and hellos
    snet/objects.py     the objects SkookumLogger puts on the wire
    snet/clock.py       vector clocks
    snet/log.py         the QSO log and the merge rules
    snet/session.py     decoding, and the sync state machine
    snet/webbridge.py   the WebSocket server the page connects to

`python3 selftest.py` needs no SkookumLogger and no network beyond the loopback interface.
