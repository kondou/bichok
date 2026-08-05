"""Wire-level constants and packet framing for SkookumNet.

Two protocol generations share the network. SkookumLogger 5.x frames a packet as [tag, payload];
SkookumNet-2 (SkookumLogger 6.x) adds the sender's sync epoch, giving [tag, epoch, payload].

Tag numbers 5 and 6 changed meaning between the generations -- 5 went from AddQso to
PartnerPrompt, 6 from ChangeQso to SyncQso -- so a tag number on its own cannot say which
generation sent it, and reading a QSO as a string is a very confident way to be wrong. Everything
here therefore identifies a packet by the *class* of its payload and treats the tag as a hint.
"""
from Foundation import NSDate, NSNumber, NSNull

# Bonjour service type. SkookumLogger 6.0 advertises this, measured with dns-sd; note that the
# SkookumLogger 5.1 sources say "skookum-network" instead, which nothing on the wire answers to.
SERVICE_TYPE = 'skookumnetwork'
LEGACY_SERVICE_TYPE = 'skookum-network'

# SkookumLogger broadcasts its PeerInformation on this interval, and so do we.
PEER_INFORMATION_INTERVAL = 5.0

# A session can lose its peer without a disconnect event ever arriving (seen after the peer was
# torn down and recreated on the other side). PeerInformation comes every five seconds while a
# session is alive, so this much silence from a connected peer is not something a live session
# produces -- at that point the session is dead and gets rebuilt. One caveat, observed by
# ContestPan: SkookumLogger stops advertising and sending while a modal dialog is open, so a
# long dialog trips this too. That costs nothing but a rebuild and a fresh fill.
SILENCE_TIMEOUT = 30.0

# Advertising can stall at the OS level: a browser that has been running for a long time on the
# other side stops seeing advertisements that a freshly started dns-sd sees fine (ContestPan
# reproduced the stall three times, the longest lasting thirteen hours). A brand-new advertisement
# is seen as a new discovery even by a stalled browser, so when nobody has connected for this long
# the advertiser is torn down and recreated. Recovery after re-advertising took anywhere from five
# seconds to three and a half minutes in the field, which is why this repeats instead of firing
# once.
READVERTISE_TIMEOUT = 60.0

# How soon after one announcement the next may follow. Announcements are sent in reply to the
# advertisements of others, so two passive peers with no log owner in sight would otherwise answer
# each other forever, each reply prompting the next. The floor breaks that loop and nothing else:
# the peers whose replies matter advertise on PEER_INFORMATION_INTERVAL, well above it.
ANNOUNCE_FLOOR = 1.0

# --- SkookumLogger 5.x tags. Packet is [tag, payload]. ---
LEGACY_GAB = 0
LEGACY_PEER_INFORMATION = 1
LEGACY_ADD_ACTIVITY = 2
LEGACY_CHANGE_ACTIVITY = 3
LEGACY_DELETE_ACTIVITY = 4
LEGACY_ADD_QSO = 5
LEGACY_CHANGE_QSO = 6
LEGACY_DELETE_QSOS = 7
LEGACY_PARTNER_PROMPT = 8
LEGACY_REQUEST_ALL_QSOS = 9
LEGACY_ALL_QSOS_RESPONSE = 10
LEGACY_REPLACE_LOG = 11

# --- SkookumNet-2 tags. Packet is [tag, epoch, payload]. ---
GAB = 0
PEER_INFORMATION = 1
ADD_ACTIVITY = 2
CHANGE_ACTIVITY = 3
DELETE_ACTIVITY = 4
PARTNER_PROMPT = 5
SYNC_QSO = 6

# Keys of the SkookumNet-2 sync dictionary. Any of them may be absent.
SYNC_QSOS = 'qsos'          # array of QSOs: additions, edits and tombstones alike
SYNC_INFO = 'info'          # PeerContestInfo -- contest settings, unused here
SYNC_CLOCK = 'vc'           # the sender's vector clock

# QSO flag bits. Everything up to Practice comes from SkookumLogger 5.1; SkookumNet-2 appends the
# last two. Deleted matters most: QSOs are never removed from a log any more, so anything counting
# worked stations that ignores this bit keeps counting QSOs the operator deleted.
FLAG_VIABLE = 0x00000001
FLAG_ZERO_POINT = 0x00000002
FLAG_DUPLICATE = 0x00000004
FLAG_SUSPECT = 0x00000008
FLAG_DID_QTC = 0x00000010
FLAG_COUNTRY_MULTIPLIER = 0x00000020
FLAG_LOCATOR_MULTIPLIER = 0x00000040
FLAG_OTHER_MULTIPLIER = 0x00000080
FLAG_PREFIX_MULTIPLIER = 0x00000100
FLAG_REGION_MULTIPLIER = 0x00000200
FLAG_ZONE_MULTIPLIER = 0x00000400
FLAG_STATION_NUMBER = 0x00000800
FLAG_POSSIBLE_COUNTRY_MULTIPLIER = 0x00001000
FLAG_POSSIBLE_LOCATOR_MULTIPLIER = 0x00002000
FLAG_POSSIBLE_OTHER_MULTIPLIER = 0x00004000
FLAG_POSSIBLE_PREFIX_MULTIPLIER = 0x00008000
FLAG_POSSIBLE_REGION_MULTIPLIER = 0x00010000
FLAG_POSSIBLE_ZONE_MULTIPLIER = 0x00020000
FLAG_POUNCE = 0x00040000
FLAG_SO2R = 0x00080000
FLAG_RADIO2 = 0x00100000
FLAG_ROVER = 0x00200000
FLAG_CAN_DO_QTCS = 0x00400000
FLAG_PRACTICE = 0x00800000
FLAG_DELETED = 0x01000000
FLAG_CONFLICTING = 0x02000000

FLAG_NAMES = {
    FLAG_VIABLE: 'Viable',
    FLAG_ZERO_POINT: 'ZeroPoint',
    FLAG_DUPLICATE: 'Duplicate',
    FLAG_SUSPECT: 'Suspect',
    FLAG_DID_QTC: 'DidQTC',
    FLAG_COUNTRY_MULTIPLIER: 'CountryMultiplier',
    FLAG_LOCATOR_MULTIPLIER: 'LocatorMultiplier',
    FLAG_OTHER_MULTIPLIER: 'OtherMultiplier',
    FLAG_PREFIX_MULTIPLIER: 'PrefixMultiplier',
    FLAG_REGION_MULTIPLIER: 'RegionMultiplier',
    FLAG_ZONE_MULTIPLIER: 'ZoneMultiplier',
    FLAG_STATION_NUMBER: 'StationNumber',
    FLAG_POSSIBLE_COUNTRY_MULTIPLIER: 'PossibleCountryMultiplier',
    FLAG_POSSIBLE_LOCATOR_MULTIPLIER: 'PossibleLocatorMultiplier',
    FLAG_POSSIBLE_OTHER_MULTIPLIER: 'PossibleOtherMultiplier',
    FLAG_POSSIBLE_PREFIX_MULTIPLIER: 'PossiblePrefixMultiplier',
    FLAG_POSSIBLE_REGION_MULTIPLIER: 'PossibleRegionMultiplier',
    FLAG_POSSIBLE_ZONE_MULTIPLIER: 'PossibleZoneMultiplier',
    FLAG_POUNCE: 'Pounce',
    FLAG_SO2R: 'SO2R',
    FLAG_RADIO2: 'Radio2',
    FLAG_ROVER: 'Rover',
    FLAG_CAN_DO_QTCS: 'CanDoQTCs',
    FLAG_PRACTICE: 'Practice',
    FLAG_DELETED: 'Deleted',
    FLAG_CONFLICTING: 'Conflicting',
}


def describe_flags(flags):
    """Render a flags bitmask as a readable list, keeping unknown bits visible rather than silent."""
    if not flags:
        return '[]'
    names = []
    for bit in range(32):
        mask = 1 << bit
        if flags & mask:
            names.append(FLAG_NAMES.get(mask, f'0x{mask:08x}'))
    return '[' + '|'.join(names) + ']'


def split_packet(packet):
    """Split a decoded packet into (tag, epoch, payload), identifying each element by its type.

    Position is deliberately not trusted. A SkookumLogger 5.x packet has no epoch element at all,
    and a SkookumNet-2 packet whose sender has no epoch yet carries NSNull in its place. Picking
    elements out by type handles every shape without having to know which one arrived.
    """
    tag = None
    epoch = None
    payload = None

    for element in packet:
        if isinstance(element, NSNull):
            continue  # an epoch the sender does not have yet
        if tag is None and isinstance(element, (int, float, NSNumber)) and not isinstance(element, bool):
            tag = int(element)
        elif epoch is None and isinstance(element, NSDate):
            epoch = element
        elif payload is None:
            payload = element

    return tag, epoch, payload


def is_sync2_packet(packet):
    """Return whether a packet came from a SkookumNet-2 peer.

    The generation shows in the shape: SkookumNet-2 always sends three elements, using NSNull for
    an epoch it does not have yet, while 5.x sends two. Knowing which is which matters more for
    sending than for receiving -- see the warning on the sending side.
    """
    return len(packet) >= 3
