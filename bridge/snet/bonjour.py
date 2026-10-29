"""Bonjour through the system DNS-SD library: advertise our listener and find everyone else's.

SkookumLogger advertises and browses with the Network framework. That framework and the DNS-SD
calls in libSystem both talk to the same mDNSResponder daemon, so a service registered here is
seen by SkookumLogger and the other way round.

Each operation holds a socket connected to the daemon. Whoever runs the event loop watches that
socket and calls process() once it turns readable; the operation's callback runs inside that
call, on the caller's thread.
"""
import ctypes
import ctypes.util
import logging
import socket

_system = ctypes.CDLL(ctypes.util.find_library('System'))

_FLAG_ADD = 0x2
_NO_ERROR = 0
_POLICY_DENIED = -65570  # kDNSServiceErr_PolicyDenied: macOS refused local network access

_Ref = ctypes.c_void_p
_c_str = ctypes.c_char_p

_RegisterReply = ctypes.CFUNCTYPE(None, _Ref, ctypes.c_uint32, ctypes.c_int32,
                                  _c_str, _c_str, _c_str, ctypes.c_void_p)
_BrowseReply = ctypes.CFUNCTYPE(None, _Ref, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
                                _c_str, _c_str, _c_str, ctypes.c_void_p)
_ResolveReply = ctypes.CFUNCTYPE(None, _Ref, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
                                 _c_str, _c_str, ctypes.c_uint16, ctypes.c_uint16,
                                 ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p)


def _declare(name, restype, *argtypes):
    function = getattr(_system, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


_register = _declare('DNSServiceRegister', ctypes.c_int32,
                     ctypes.POINTER(_Ref), ctypes.c_uint32, ctypes.c_uint32, _c_str, _c_str, _c_str,
                     _c_str, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_void_p, _RegisterReply,
                     ctypes.c_void_p)
_browse = _declare('DNSServiceBrowse', ctypes.c_int32,
                   ctypes.POINTER(_Ref), ctypes.c_uint32, ctypes.c_uint32, _c_str, _c_str,
                   _BrowseReply, ctypes.c_void_p)
_resolve = _declare('DNSServiceResolve', ctypes.c_int32,
                    ctypes.POINTER(_Ref), ctypes.c_uint32, ctypes.c_uint32, _c_str, _c_str, _c_str,
                    _ResolveReply, ctypes.c_void_p)
_socket_of = _declare('DNSServiceRefSockFD', ctypes.c_int, _Ref)
_process = _declare('DNSServiceProcessResult', ctypes.c_int32, _Ref)
_release = _declare('DNSServiceRefDeallocate', None, _Ref)


class BonjourError(Exception):
    """A DNS-SD call returned an error code."""

    def __init__(self, call, code):
        super().__init__(describe(call, code))
        self.code = code


def describe(call, code):
    """Say what went wrong, naming the setting to change when macOS is the one refusing."""
    if code == _POLICY_DENIED:
        return (f"{call}: local network access was denied. Allow the app this runs in (Terminal, "
                f"for instance) under System Settings > Privacy & Security > Local Network")
    return f"{call} failed with DNS-SD error {code}"


def _text(raw):
    return raw.decode('utf-8', errors='replace') if raw else ''


def txt_record(values):
    """Pack a dict of strings into a TXT record: each entry is a length byte, then key=value."""
    record = bytearray()
    for key, value in values.items():
        entry = f'{key}={value}'.encode('utf-8')
        if len(entry) > 255:
            raise ValueError(f'the TXT entry for {key} does not fit in 255 bytes')
        record.append(len(entry))
        record.extend(entry)
    return bytes(record)


def txt_values(record):
    """Unpack a TXT record into a dict. Keys are case-insensitive, so they come back lowercased;
    an entry without '=' or that is not UTF-8 is skipped."""
    values = {}
    position = 0
    while position < len(record):
        length = record[position]
        entry = bytes(record[position + 1:position + 1 + length])
        position += 1 + length
        key, equals, value = entry.partition(b'=')
        if not equals:
            continue
        try:
            values[key.decode('utf-8').lower()] = value.decode('utf-8')
        except UnicodeDecodeError:
            continue
    return values


class Operation:
    """One running DNS-SD operation. Close it to stop; closing a registration withdraws the service."""

    def __init__(self, call, reply_type, reply, start, *arguments):
        self._ref = _Ref()
        self._reply = reply_type(reply)  # the ctypes callback has to outlive the operation
        code = start(ctypes.byref(self._ref), *arguments, self._reply, None)
        if code != _NO_ERROR:
            raise BonjourError(call, code)

    def fileno(self):
        """The daemon socket, so a selector can watch this object directly."""
        return _socket_of(self._ref)

    def process(self):
        """Read what the daemon sent and run the callback. Call when fileno() is readable."""
        code = _process(self._ref)
        if code != _NO_ERROR:
            raise BonjourError('DNSServiceProcessResult', code)

    def close(self):
        """Stop the operation. Safe to call more than once."""
        if self._ref:
            _release(self._ref)
            self._ref = _Ref()


def advertise(name, service_type, port, txt, on_registered):
    """Register a service. on_registered(name) gets the name the daemon settled on, which differs
    from the one asked for when another service already holds it."""
    def reply(_ref, _flags, code, registered, _type, _domain, _context):
        if code != _NO_ERROR:
            logging.error("%s", describe('DNSServiceRegister', code))
        else:
            on_registered(_text(registered))

    return Operation('DNSServiceRegister', _RegisterReply, reply, _register,
                     0, 0, name.encode('utf-8'), service_type.encode('utf-8'), None, None,
                     socket.htons(port), len(txt), txt)


def browse(service_type, on_change):
    """Watch for services of a type. on_change(added, interface, name, domain) is called once per
    service per network interface, as it appears or goes away there."""
    def reply(_ref, flags, interface, code, name, _type, domain, _context):
        if code != _NO_ERROR:
            logging.error("%s", describe('DNSServiceBrowse', code))
        else:
            on_change(bool(flags & _FLAG_ADD), interface, _text(name), _text(domain))

    return Operation('DNSServiceBrowse', _BrowseReply, reply, _browse,
                     0, 0, service_type.encode('utf-8'), None)


def resolve(name, service_type, domain, on_resolved):
    """Look up where a service lives. on_resolved(host, port, txt) is called with the host name,
    the port and the TXT record as a dict -- and again whenever any of them changes, for as long
    as the operation stays open."""
    def reply(_ref, _flags, _interface, code, _fullname, host, port, length, txt, _context):
        if code != _NO_ERROR:
            logging.error("%s", describe('DNSServiceResolve', code))
            return
        record = bytes(txt[:length]) if length else b''
        on_resolved(_text(host), socket.ntohs(port), txt_values(record))

    return Operation('DNSServiceResolve', _ResolveReply, reply, _resolve,
                     0, 0, name.encode('utf-8'), service_type.encode('utf-8'), domain.encode('utf-8'))
