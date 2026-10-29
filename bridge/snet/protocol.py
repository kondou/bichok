"""Wire-level constants and packet framing for SkookumNet.

SkookumLogger 6.1(16) and later carry SkookumNet over plain TCP: each station advertises a
listener over Bonjour, and every pair of stations keeps one connection between them, divided into
frames. The transport module deals with all of that. What travels inside a data frame is one
packet: an NSKeyedArchiver archive of [tag, epoch, payload], where the epoch is NSNull while the
sender has none.
"""
import plistlib

from Foundation import NSDate, NSNumber, NSNull

# Bonjour service type. The service name is the station name; the TXT record carries the station
# name again under 'name' and a UUID made afresh on every join under 'uuid'.
SERVICE_TYPE = '_skookumnet._tcp'
TXT_UUID = 'uuid'
TXT_NAME = 'name'

# Frames: a 4-byte big-endian length that counts the kind byte and the payload, the kind, and the
# payload. A length of 0 or above the limit closes the connection.
FRAME_HELLO = 0
FRAME_DATA = 1
FRAME_LIMIT = 64 * 1024 * 1024
HELLO_VERSION = 1

MAX_PEERS = 8
RECONNECT_DELAY = 2.0     # after a connection ends, before the station that opens connections tries again
FALLBACK_DELAY = 10.0     # how long the station that waits gives the other one to connect first
HELLO_TIMEOUT = 10.0      # a connection that has not said hello by then is closed
DUPLICATE_LINGER = 2.0    # how long the losing one of two connections to a peer stays open

# TCP options SkookumLogger sets. With them a peer that vanishes without closing its connection
# is noticed after about sixteen seconds.
KEEPALIVE_IDLE = 10
KEEPALIVE_INTERVAL = 2
KEEPALIVE_COUNT = 3
CONNECTION_DROP_TIME = 30  # seconds before unacknowledged data gives the connection up

# SkookumLogger broadcasts its PeerInformation on this interval.
PEER_INFORMATION_INTERVAL = 5.0

# How soon after one announcement the next may follow. Announcements are sent in reply to the
# advertisements of others, so two passive peers with no log owner in sight would otherwise answer
# each other forever, each reply prompting the next. The floor breaks that loop and nothing else:
# the peers whose replies matter advertise on PEER_INFORMATION_INTERVAL, well above it.
ANNOUNCE_FLOOR = 1.0

# Packet tags. They are consecutive values of an enum on SkookumLogger's side; SerialNumber is
# pinned at 7 by the serial-server protocol.
GAB = 0
PEER_INFORMATION = 1
ADD_ACTIVITY = 2
CHANGE_ACTIVITY = 3
DELETE_ACTIVITY = 4
PARTNER_PROMPT = 5
SYNC_QSO = 6
SERIAL_NUMBER = 7

# Keys of the sync dictionary. At least one of qsos and info is present.
SYNC_QSOS = 'qsos'          # array of QSOs: additions, edits and tombstones alike
SYNC_INFO = 'info'          # PeerContestInfo -- contest settings, unused here
SYNC_CLOCK = 'vc'           # the sender's vector clock, present when the packet is a fill

# QSO flag bits (QSOFlagsMask). Everything up to Practice dates from SkookumLogger 5.1; the
# automerge generation appended the last two. Deleted matters most: QSOs are never removed from a
# log any more, so anything counting worked stations that ignores this bit keeps counting QSOs the
# operator deleted.
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
    """Split a decoded packet into (tag, epoch, payload), or return None when it is malformed.

    The epoch comes back as None when the sender put NSNull there, meaning it has none yet. Any
    other non-date in that slot makes the packet malformed, as it does on SkookumLogger's side.
    """
    if packet is None or len(packet) < 3:
        return None
    tag, epoch, payload = packet[0], packet[1], packet[2]
    if isinstance(tag, bool) or not isinstance(tag, (int, NSNumber)):
        return None
    if epoch is None or isinstance(epoch, NSNull):
        epoch = None  # PyObjC hands NSNull back as None once it has passed through a Python list
    elif not isinstance(epoch, NSDate):
        return None
    return int(tag), epoch, payload


def peek_packet(data):
    """Say what a packet that would not unarchive contained: its tag and the classes it names.

    The archive is read as a plain property list, so none of the objects in it are created --
    which is the point, since one of them is what the unarchiver refused.
    """
    try:
        archive = plistlib.loads(data)
        objects = archive['$objects']
        names = sorted({entry['$classname'] for entry in objects
                        if isinstance(entry, dict) and '$classname' in entry})
        root = objects[archive['$top']['root'].data]
        tag = objects[root['NS.objects'][0].data]
        return f'tag {tag}, classes {", ".join(names)}'
    except (plistlib.InvalidFileException, KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
        return f'not a SkookumNet packet: {error}'
