"""The QSO log this client keeps, and the automerge rules that maintain it.

SkookumNet-2 identifies a QSO by the (stationName, sequenceID) pair rather than by its UUID, and
never removes one: a deletion arrives as a flag on the QSO itself, so it flows through the same
merge path as any other edit. Merging follows the rules in the SkookumNetAutomerge article.

This client only ever reads. It never originates an event, so its own vector clock entry never
advances -- it reports the element-wise maximum of what it has been told and nothing more.
"""
import logging

from . import clock as vclock
from . import protocol

_HASH_MASK = 0xFFFFFFFFFFFFFFFF

ADDED = 'added'
UPDATED = 'updated'
DELETED = 'deleted'
CONFLICTED = 'conflicted'

# Fields compared to tell a real conflict from two peers reaching the same answer independently
_COMPARED_FIELDS = ('timeStamp', 'mainReceiveFrequency', 'subReceiveFrequency', 'transmitFrequency',
                    'mode', 'operatorCall', 'notes', 'flags')


def qso_key(qso):
    """Return the identity of a QSO.

    SkookumNet-2 pairs the station name with a sequence ID. A SkookumLogger 5.x QSO has no
    sequence ID, so its UUID stands in -- the two generations never share a log, so the two
    key shapes never collide.
    """
    sequence = getattr(qso, 'sequenceID', None)
    if sequence is not None:
        return (str(getattr(qso, 'stationName', '') or ''), int(sequence))
    return ('uuid', str(getattr(qso, 'identifier', '') or ''))


def is_deleted(qso):
    """Return whether this QSO is a tombstone."""
    return bool(int(getattr(qso, 'flags', 0) or 0) & protocol.FLAG_DELETED)


def _fields_of(qso):
    """Return the comparable content of a QSO, exchanges included."""
    values = [str(getattr(qso, name, '') or '') for name in _COMPARED_FIELDS]
    for side in ('sentExchange', 'receivedExchange'):
        exchange = getattr(qso, side, None)
        values.append(str(exchange.as_dict()) if exchange is not None else '')
    return values


class QsoLog:
    """Holds the merged QSOs and reports what changed as packets arrive."""

    def __init__(self):
        self.qsos = {}
        self.clocks = {}
        self.node_clock = {}

    def log_hash(self, include_deleted=True):
        """The whole-log hash a peer advertises so others can tell whether their copies agree.

        Constants follow the example client, read with its author's permission. The result is
        order-dependent; we run over the QSOs in the order they entered the log, which for a fill
        is sequence order. An empty log hashes to 1, not 0.
        """
        result = 1
        for qso in self.qsos.values():
            if not include_deleted and is_deleted(qso):
                continue
            mixed = (result + qso.qso_hash()) & _HASH_MASK
            mixed = (31 * mixed) & _HASH_MASK
            result = (result + mixed) & _HASH_MASK
        return result

    def clear(self):
        """Drop everything. Used on an epoch reset and when a 5.x peer replaces the log."""
        self.qsos.clear()
        self.clocks.clear()
        self.node_clock = {}

    def snapshot(self):
        """Return the live QSOs, tombstones excluded."""
        return [qso for key, qso in self.qsos.items() if not is_deleted(qso)]

    def observe_clock(self, incoming):
        """Fold a peer's vector clock into ours. We only ever raise our view, never claim an event."""
        self.node_clock = vclock.merge(self.node_clock, vclock.to_dict(incoming))

    def merge_qso(self, qso):
        """Merge one QSO and return what changed: ADDED, UPDATED, DELETED, CONFLICTED or None."""
        key = qso_key(qso)
        theirs = vclock.to_dict(getattr(qso, 'vectorClock', None))

        if key not in self.qsos:
            self.qsos[key] = qso
            self.clocks[key] = theirs
            return DELETED if is_deleted(qso) else ADDED

        ours = self.clocks.get(key, {})
        relation = vclock.compare(ours, theirs)

        if relation in (vclock.IDENTICAL, vclock.DOMINATES):
            # We already hold everything this copy carries.
            return None

        if relation == vclock.DOMINATED:
            return self._replace(key, qso, theirs)

        # Concurrent. Only a difference in the content makes it a real conflict.
        if _fields_of(self.qsos[key]) == _fields_of(qso):
            self.clocks[key] = vclock.merge(ours, theirs)
            return None

        logging.warning("QSO %s is in conflict between peers; keeping the copy we already had", key)
        return CONFLICTED

    def _replace(self, key, qso, theirs):
        """Take the incoming copy, and report whether that made the QSO disappear."""
        was_deleted = is_deleted(self.qsos[key])
        self.qsos[key] = qso
        self.clocks[key] = theirs

        if is_deleted(qso):
            return None if was_deleted else DELETED
        return ADDED if was_deleted else UPDATED

    # --- SkookumLogger 5.x paths, where the packet itself names the operation ---

    def legacy_add(self, qso):
        """Record a QSO announced by a 5.x peer."""
        key = qso_key(qso)
        existed = key in self.qsos
        self.qsos[key] = qso
        return UPDATED if existed else ADDED

    def legacy_delete(self, qso):
        """Forget a QSO a 5.x peer deleted. Tombstones do not exist in that generation."""
        return DELETED if self.qsos.pop(qso_key(qso), None) is not None else None
