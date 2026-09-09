"""Pure-python V4L2 enumeration (no v4l-utils / no root needed)."""
import ctypes, fcntl, glob, os, struct

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE, _IOC_READ = 1, 2


def _IOC(d, t, nr, size):
    return (d << _IOC_DIRSHIFT) | (ord(t) << _IOC_TYPESHIFT) | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT)


VIDIOC_QUERYCAP = _IOC(_IOC_READ, 'V', 0, 104)
VIDIOC_ENUM_FMT = _IOC(_IOC_READ | _IOC_WRITE, 'V', 2, 64)
VIDIOC_ENUM_FRAMESIZES = _IOC(_IOC_READ | _IOC_WRITE, 'V', 74, 44)
VIDIOC_ENUM_FRAMEINTERVALS = _IOC(_IOC_READ | _IOC_WRITE, 'V', 75, 52)

BUF_TYPE_VIDEO_CAPTURE = 1
CAP_VIDEO_CAPTURE = 0x00000001
CAP_DEVICE_CAPS = 0x80000000


def _fourcc_str(v):
    return ''.join(chr((v >> (8 * i)) & 0xFF) for i in range(4)).strip('\x00 ')


def _cstr(b):
    return b.split(b'\x00')[0].decode('utf-8', 'replace')


def query_cap(fd):
    buf = bytearray(104)
    fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf, True)
    driver, card, bus_info, version, caps, device_caps = struct.unpack_from('<16s32s32sIII', buf, 0)
    return {
        'driver': _cstr(driver), 'card': _cstr(card), 'bus_info': _cstr(bus_info),
        'version': f'{(version >> 16) & 0xFF}.{(version >> 8) & 0xFF}.{version & 0xFF}',
        'capabilities': caps, 'device_caps': device_caps,
    }


def enum_frame_intervals(fd, pixfmt, w, h, limit=12):
    out = []
    for i in range(limit):
        buf = bytearray(52)
        struct.pack_into('<IIII', buf, 0, i, pixfmt, w, h)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMEINTERVALS, buf, True)
        except OSError:
            break
        ftype = struct.unpack_from('<I', buf, 16)[0]
        if ftype == 1:  # discrete
            num, den = struct.unpack_from('<II', buf, 20)
            if num:
                out.append(round(den / num, 3))
        else:
            break
    return out


def enum_frame_sizes(fd, pixfmt, limit=64):
    out = []
    for i in range(limit):
        buf = bytearray(44)
        struct.pack_into('<II', buf, 0, i, pixfmt)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, buf, True)
        except OSError:
            break
        ftype = struct.unpack_from('<I', buf, 8)[0]
        if ftype == 1:  # discrete
            w, h = struct.unpack_from('<II', buf, 12)
            out.append({'width': w, 'height': h, 'fps': enum_frame_intervals(fd, pixfmt, w, h)})
        else:  # stepwise / continuous
            mnw, mxw, stw, mnh, mxh, sth = struct.unpack_from('<IIIIII', buf, 12)
            out.append({'stepwise': {'min_width': mnw, 'max_width': mxw, 'step_width': stw,
                                     'min_height': mnh, 'max_height': mxh, 'step_height': sth}})
            break
    return out


def enum_formats(fd, limit=32):
    out = []
    for i in range(limit):
        buf = bytearray(64)
        struct.pack_into('<II', buf, 0, i, BUF_TYPE_VIDEO_CAPTURE)
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf, True)
        except OSError:
            break
        flags = struct.unpack_from('<I', buf, 8)[0]
        desc = _cstr(bytes(buf[12:44]))
        pixfmt = struct.unpack_from('<I', buf, 44)[0]
        out.append({'fourcc': _fourcc_str(pixfmt), 'description': desc,
                    'compressed': bool(flags & 1), 'sizes': enum_frame_sizes(fd, pixfmt)})
    return out


def usb_attrs(devnode):
    """Walk sysfs up from the video node to the owning USB device."""
    name = os.path.basename(devnode)
    path = os.path.realpath(f'/sys/class/video4linux/{name}/device')
    for _ in range(6):
        if os.path.exists(os.path.join(path, 'idVendor')):
            def rd(f):
                try:
                    with open(os.path.join(path, f)) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            return {'vid': rd('idVendor'), 'pid': rd('idProduct'), 'manufacturer': rd('manufacturer'),
                    'product': rd('product'), 'serial': rd('serial'), 'speed_mbps': rd('speed'),
                    'version': rd('version'), 'busnum': rd('busnum'), 'devnum': rd('devnum'),
                    'maxpower': rd('bMaxPower'), 'sysfs': path}
        parent = os.path.dirname(path)
        if parent == path or not parent.startswith('/sys'):
            break
        path = parent
    return None


def probe_all():
    devices = []
    for node in sorted(glob.glob('/dev/video*'), key=lambda p: int(''.join(c for c in p if c.isdigit()) or 0)):
        entry = {'device': node}
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            entry['error'] = f'{e.__class__.__name__}: {e}'
            entry['readable'] = False
            devices.append(entry)
            continue
        entry['readable'] = True
        try:
            cap = query_cap(fd)
            entry.update(cap)
            eff = cap['device_caps'] if cap['capabilities'] & CAP_DEVICE_CAPS else cap['capabilities']
            entry['is_capture'] = bool(eff & CAP_VIDEO_CAPTURE)
            entry['formats'] = enum_formats(fd) if entry['is_capture'] else []
        except OSError as e:
            entry['error'] = f'{e.__class__.__name__}: {e}'
        finally:
            os.close(fd)
        entry['usb'] = usb_attrs(node)
        devices.append(entry)
    return devices


if __name__ == '__main__':
    import json
    print(json.dumps(probe_all(), indent=2))
