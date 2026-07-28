"""Python stand-ins for the Objective-C objects SkookumLogger puts on the wire.

SkookumNet archives its packets with NSKeyedArchiver, so every object travelling over it has to be
matched here by a class of the same name implementing NSSecureCoding. The archive keys and field
layouts below are the wire format -- they have to match SkookumLogger exactly or nothing decodes.

Every field is read defensively. Only a handful of keys are guaranteed across SkookumLogger
versions, and a peer whose Run/S&P display goes blank is a far better outcome than one that throws
away a packet full of QSOs because a single field changed shape.
"""
import objc
from Foundation import NSObject, NSSet, NSArray, NSString, NSNumber, NSDate, NSUUID, NSDictionary

NSSecureCoding = objc.protocolNamed('NSSecureCoding')

_STRING = NSSet.setWithArray_([NSString])
_NUMBER = NSSet.setWithArray_([NSNumber])
_DATE = NSSet.setWithArray_([NSDate])
_UUID = NSSet.setWithArray_([NSUUID])
_STRING_ARRAY = NSSet.setWithArray_([NSArray, NSString])
_CLOCK = NSSet.setWithArray_([NSDictionary, NSString, NSNumber])

# Exchange packs its twelve fields into one array, in this order.
EXCHANGE_FIELDS = ('call', 'check', 'clubName', 'clubStatus', 'grid', 'info',
                   'name', 'power', 'precedence', 'report', 'serial', 'zone')


def _decode(decoder, classes, key, default=None):
    """Read one object, falling back to default rather than failing the whole packet."""
    try:
        value = decoder.decodeObjectOfClasses_forKey_(classes, key)
    except Exception:  # pylint: disable=broad-except
        return default
    return default if value is None else value


def _decode_int(decoder, key, default=0):
    """Read one primitive integer, falling back to default if the key holds something else."""
    try:
        return int(decoder.decodeIntegerForKey_(key))
    except Exception:  # pylint: disable=broad-except
        return default


class Exchange(NSObject, protocols=[NSSecureCoding]):
    """One side of a contest exchange: twelve optional strings under a single 'fields' key."""

    @classmethod
    def supportsSecureCoding(cls):
        """Required by NSSecureCoding."""
        return True

    def initWithCoder_(self, decoder):
        """Unpack the twelve-element fields array by position."""
        fields = _decode(decoder, _STRING_ARRAY, 'fields') or []
        for index, name in enumerate(EXCHANGE_FIELDS):
            value = fields[index] if index < len(fields) else ''
            setattr(self, name, str(value) if value is not None else '')
        return self

    def encodeWithCoder_(self, coder):
        """Repack the twelve fields in the order SkookumLogger expects."""
        coder.encodeObject_forKey_([getattr(self, name, '') or '' for name in EXCHANGE_FIELDS], 'fields')

    def as_dict(self):
        """Return the exchange as a plain dict of strings."""
        return {name: getattr(self, name, '') or '' for name in EXCHANGE_FIELDS}

    def __str__(self):
        filled = {name: value for name, value in self.as_dict().items() if value}
        return f'Exchange({filled})'


class TransientQso(NSObject, protocols=[NSSecureCoding]):
    """A single QSO as it travels over SkookumNet.

    SkookumNet-2 adds the last four fields. Identity moved with them: a QSO is now identified by
    its (stationName, sequenceID) pair rather than by its UUID, and deletions arrive as a flag on
    the QSO instead of as a separate packet.
    """

    @classmethod
    def supportsSecureCoding(cls):
        """Required by NSSecureCoding."""
        return True

    def initWithCoder_(self, decoder):
        """Decode the QSO. Fields absent from an older peer simply stay empty."""
        self.identifier = _decode(decoder, _UUID, 'identifier')
        self.timeStamp = _decode(decoder, _DATE, 'timeStamp')
        self.mainReceiveFrequency = _decode(decoder, _NUMBER, 'mainReceiveFrequency')
        self.subReceiveFrequency = _decode(decoder, _NUMBER, 'subReceiveFrequency')
        self.transmitFrequency = _decode(decoder, _NUMBER, 'transmitFrequency')
        self.mode = _decode(decoder, _STRING, 'mode', '')
        self.sentExchange = _decode(decoder, NSSet.setWithArray_([Exchange]), 'sentExchange')
        self.receivedExchange = _decode(decoder, NSSet.setWithArray_([Exchange]), 'receivedExchange')
        self.operatorCall = _decode(decoder, _STRING, 'operatorCall', '')
        self.stationName = _decode(decoder, _STRING, 'stationName', '')
        self.notes = _decode(decoder, _STRING, 'notes', '')
        self.flags = _decode(decoder, _NUMBER, 'flags', 0)

        # SkookumNet-2 additions
        self.sequenceID = _decode(decoder, _NUMBER, 'sequenceID')
        self.vectorClock = _decode(decoder, _CLOCK, 'vectorClock')
        self.conflictInfo = _decode(decoder, _CLOCK, 'conflictInfo')
        self.lastModifiedBy = _decode(decoder, _STRING, 'lastModifiedBy', '')
        return self

    def encodeWithCoder_(self, coder):
        """Write the QSO back out. This client never originates one, but Activity inherits this."""
        coder.encodeObject_forKey_(self.identifier, 'identifier')
        coder.encodeObject_forKey_(self.timeStamp, 'timeStamp')
        coder.encodeObject_forKey_(self.mainReceiveFrequency, 'mainReceiveFrequency')
        coder.encodeObject_forKey_(self.subReceiveFrequency, 'subReceiveFrequency')
        coder.encodeObject_forKey_(self.transmitFrequency, 'transmitFrequency')
        coder.encodeObject_forKey_(self.mode, 'mode')
        coder.encodeObject_forKey_(self.sentExchange, 'sentExchange')
        coder.encodeObject_forKey_(self.receivedExchange, 'receivedExchange')
        coder.encodeObject_forKey_(self.operatorCall, 'operatorCall')
        coder.encodeObject_forKey_(self.stationName, 'stationName')
        coder.encodeObject_forKey_(self.notes, 'notes')
        coder.encodeObject_forKey_(self.flags, 'flags')
        coder.encodeObject_forKey_(self.sequenceID, 'sequenceID')
        coder.encodeObject_forKey_(self.vectorClock, 'vectorClock')
        coder.encodeObject_forKey_(self.conflictInfo, 'conflictInfo')
        coder.encodeObject_forKey_(self.lastModifiedBy, 'lastModifiedBy')

    def __str__(self):
        call = self.receivedExchange.call if self.receivedExchange else ''
        return f'Qso({call} {self.mode} {self.transmitFrequency} seq={self.sequenceID})'


class Activity(TransientQso):
    """A spot. SkookumLogger derives Activity from TransientQso, and so must this."""

    @classmethod
    def supportsSecureCoding(cls):
        """Required by NSSecureCoding."""
        return True

    def initWithCoder_(self, decoder):
        """Decode the QSO half through the superclass, then the spot-specific fields."""
        this = objc.super(Activity, self).initWithCoder_(decoder)
        if this is None:
            return None
        this.info = _decode(decoder, _STRING, 'info', '')
        this.signalLevel = _decode(decoder, _NUMBER, 'signalLevel')
        this.sourceCall = _decode(decoder, _STRING, 'sourceCall', '')
        this.bearing = _decode(decoder, _NUMBER, 'bearing')
        try:
            this.spottedByMe = bool(decoder.decodeBoolForKey_('spottedByMe'))
        except Exception:  # pylint: disable=broad-except
            this.spottedByMe = False
        return this

    def encodeWithCoder_(self, coder):
        """Write the QSO half through the superclass, then the spot-specific fields."""
        objc.super(Activity, self).encodeWithCoder_(coder)
        coder.encodeObject_forKey_(self.info, 'info')
        coder.encodeObject_forKey_(self.signalLevel, 'signalLevel')
        coder.encodeObject_forKey_(self.sourceCall, 'sourceCall')
        coder.encodeBool_forKey_(bool(self.spottedByMe), 'spottedByMe')
        coder.encodeObject_forKey_(self.bearing, 'bearing')

    def __str__(self):
        return f'Activity({self.sourceCall} spotted {self.info} at {self.transmitFrequency})'


class PeerInformation(NSObject, protocols=[NSSecureCoding]):
    """What each peer broadcasts about itself every few seconds.

    Under SkookumNet-2 this doubles as a fill request: the vector clock it carries tells the other
    peers what this station has seen, and anything they hold beyond that they push back.
    """

    @classmethod
    def supportsSecureCoding(cls):
        """Required by NSSecureCoding."""
        return True

    def initWithStationName_(self, name):
        """Build the record this client broadcasts about itself."""
        self.peerHostName = name
        self.operatingMode = 0
        self.keyboardFocus = 0
        self.transmitFocus = 0
        self.runReceiveFrequency = 0
        self.runTransmitFrequency = 0
        self.runMode = ''
        self.pounceReceiveFrequency = 0
        self.pounceTransmitFrequency = 0
        self.pounceMode = ''
        self.lastTenRate = 0
        self.lastTenQsoPointRate = 0
        self.vectorClock = {}
        self.syncEpoch = None
        self.binaryVersion = None
        self.contestName = ''
        self.contestStartTime = None
        self.contestEndTime = None
        self.hashOfQSOs = 0
        self.timestamp = None
        return self

    def initWithCoder_(self, decoder):
        """Decode a peer record. A SkookumLogger 5.x peer has no value for the last two keys."""
        self.peerHostName = _decode(decoder, _STRING, 'peerHostName', '')
        self.operatingMode = _decode_int(decoder, 'operatingMode')
        self.keyboardFocus = _decode_int(decoder, 'keyboardFocus')
        self.transmitFocus = _decode_int(decoder, 'transmitFocus')
        self.runReceiveFrequency = _decode_int(decoder, 'runReceiveFrequency')
        self.runTransmitFrequency = _decode_int(decoder, 'runTransmitFrequency')
        self.runMode = _decode(decoder, _STRING, 'runMode', '')
        self.pounceReceiveFrequency = _decode_int(decoder, 'pounceReceiveFrequency')
        self.pounceTransmitFrequency = _decode_int(decoder, 'pounceTransmitFrequency')
        self.pounceMode = _decode(decoder, _STRING, 'pounceMode', '')
        self.lastTenRate = _decode(decoder, _NUMBER, 'lastTenRate', 0)
        self.lastTenQsoPointRate = _decode(decoder, _NUMBER, 'lastTenQsoPointRate', 0)
        self.vectorClock = _decode(decoder, _CLOCK, 'vectorClock')
        self.syncEpoch = _decode(decoder, _DATE, 'syncEpoch')

        # The rest of what SkookumNet-2 added. binaryVersion identifies the sending build, and a
        # peer whose value does not match is flagged as a version mismatch by SkookumLogger.
        self.binaryVersion = _decode(decoder, _STRING, 'binaryVersion')
        self.contestName = _decode(decoder, _STRING, 'contestName', '')
        self.contestStartTime = _decode(decoder, _DATE, 'contestStartTime')
        self.contestEndTime = _decode(decoder, _DATE, 'contestEndTime')
        self.timestamp = _decode(decoder, _DATE, 'timestamp')

        # hashOfQSOs is deliberately left alone. Reading it back as an integer is rejected by the
        # unarchiver, and a rejected key does not fail quietly -- it fails the whole packet, which
        # then never reaches the code that decides which protocol to answer in. Nothing here uses
        # the value, so the safest thing to do with it is nothing.
        self.hashOfQSOs = 0
        return self

    def encodeWithCoder_(self, coder):
        """Write every field, including the two SkookumNet-2 keys. A 5.x peer ignores what it does not know."""
        coder.encodeObject_forKey_(self.peerHostName, 'peerHostName')
        coder.encodeInteger_forKey_(self.operatingMode, 'operatingMode')
        coder.encodeInteger_forKey_(self.keyboardFocus, 'keyboardFocus')
        coder.encodeInteger_forKey_(self.transmitFocus, 'transmitFocus')
        coder.encodeInteger_forKey_(self.runReceiveFrequency, 'runReceiveFrequency')
        coder.encodeInteger_forKey_(self.runTransmitFrequency, 'runTransmitFrequency')
        coder.encodeObject_forKey_(self.runMode, 'runMode')
        coder.encodeInteger_forKey_(self.pounceReceiveFrequency, 'pounceReceiveFrequency')
        coder.encodeInteger_forKey_(self.pounceTransmitFrequency, 'pounceTransmitFrequency')
        coder.encodeObject_forKey_(self.pounceMode, 'pounceMode')
        coder.encodeObject_forKey_(self.lastTenRate, 'lastTenRate')
        coder.encodeObject_forKey_(self.lastTenQsoPointRate, 'lastTenQsoPointRate')
        coder.encodeObject_forKey_(self.vectorClock, 'vectorClock')
        coder.encodeObject_forKey_(self.syncEpoch, 'syncEpoch')
        coder.encodeObject_forKey_(self.binaryVersion, 'binaryVersion')
        coder.encodeObject_forKey_(self.contestName, 'contestName')
        coder.encodeObject_forKey_(self.contestStartTime, 'contestStartTime')
        coder.encodeObject_forKey_(self.contestEndTime, 'contestEndTime')
        coder.encodeObject_forKey_(self.timestamp, 'timestamp')

        # hashOfQSOs has to be a real NSNumber: it is compared with -[NSNumber isEqualToNumber:],
        # which needs an object on both sides, and both a primitive and a missing key arrive as nil.
        # We cannot compute SkookumLogger's hash, so the number we send will not match it. Being
        # told we are out of sync is the honest answer anyway.
        coder.encodeObject_forKey_(NSNumber.numberWithLongLong_(int(self.hashOfQSOs or 0)), 'hashOfQSOs')

    def __str__(self):
        return (f'Peer({self.peerHostName} mode={self.operatingMode} tx={self.transmitFocus} '
                f'run={self.runTransmitFrequency}/{self.runMode} '
                f'pounce={self.pounceTransmitFrequency}/{self.pounceMode} '
                f'clock={dict(self.vectorClock) if self.vectorClock else {}})')


class PeerContestInfo(NSObject, protocols=[NSSecureCoding]):
    """Contest settings, decoded and discarded.

    This travels alongside the QSOs in a sync packet. Nothing here needs it, but the class still
    has to exist: without it the secure unarchiver rejects the enclosing packet and the QSOs in it
    are lost too. Reading none of its fields also avoids depending on the classes it relates to.
    """

    @classmethod
    def supportsSecureCoding(cls):
        """Required by NSSecureCoding."""
        return True

    def initWithCoder_(self, decoder):
        """Accept the archive without reading any of it."""
        return self

    def encodeWithCoder_(self, coder):
        """Write nothing. This client never originates contest settings."""

    def __str__(self):
        return 'PeerContestInfo(ignored)'


def allowed_classes():
    """Every class that may appear in a packet, for the secure unarchiver.

    Collections propagate this set to what they contain, so the QSOs and the contest info nested
    inside a sync dictionary are covered by naming them here.
    """
    from MultipeerConnectivity import MCPeerID  # pylint: disable=import-outside-toplevel
    return NSSet.setWithArray_([
        NSArray, NSDictionary, NSString, NSNumber, NSDate, NSUUID,
        Exchange, TransientQso, Activity, PeerInformation, PeerContestInfo,
        MCPeerID,
    ])
