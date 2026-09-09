"""ZED M camera diagnostic web server."""
import glob, io, json, os, re, threading, time
from collections import deque

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_file

import v4l2probe

app = Flask(__name__)

# Stereolabs USB product IDs. Video (UVC) interface and HID (IMU) interface
# enumerate as two separate USB devices; the video one is what streams images.
ZED_VENDOR = '2b03'
ZED_PIDS = {
    'f580': ('ZED', 'video'),      'f581': ('ZED', 'hid'),
    'f680': ('ZED-M', 'video'),    'f681': ('ZED-M', 'hid'),
    'f780': ('ZED 2', 'video'),    'f781': ('ZED 2', 'hid'),
    'f880': ('ZED 2i', 'video'),   'f881': ('ZED 2i', 'hid'),
    'f582': ('ZED X', 'video'),
}

# Side-by-side stereo modes of the ZED family (full frame = both eyes).
ZED_MODES = [
    {'name': '2.2K',  'width': 4416, 'height': 1242, 'fps': [15]},
    {'name': 'FHD',   'width': 3840, 'height': 1080, 'fps': [30, 15]},
    {'name': 'HD',    'width': 2560, 'height': 720,  'fps': [60, 30, 15]},
    {'name': 'VGA',   'width': 1344, 'height': 376,  'fps': [100, 60, 30, 15]},
]
_ZED_RES = {(m['width'], m['height']) for m in ZED_MODES}


def scan_usb():
    """Enumerate USB devices straight from sysfs (no lsusb needed)."""
    devices = []
    for path in sorted(glob.glob('/sys/bus/usb/devices/*')):
        def rd(f):
            try:
                with open(os.path.join(path, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return None
        vid = rd('idVendor')
        if not vid:
            continue
        devices.append({
            'vid': vid, 'pid': rd('idProduct'), 'product': rd('product'),
            'manufacturer': rd('manufacturer'), 'serial': rd('serial'),
            'speed_mbps': rd('speed'), 'usb_version': rd('version'),
            'busnum': rd('busnum'), 'devnum': rd('devnum'),
            'maxpower': rd('bMaxPower'), 'bus_id': os.path.basename(path),
        })
    return devices


def scan_buses():
    """Root hubs (sysfs names them usbN) and the max link speed each one offers."""
    buses = []
    for path in sorted(glob.glob('/sys/bus/usb/devices/usb[0-9]*')):
        if not re.fullmatch(r'usb\d+', os.path.basename(path)):
            continue

        def rd(f, _p=path):
            try:
                with open(os.path.join(_p, f)) as fh:
                    return fh.read().strip()
            except OSError:
                return None

        busnum = rd('busnum')
        # Devices sitting on this bus, by port
        used = []
        for child in sorted(glob.glob(f'{path}/{busnum}-*')):
            name = os.path.basename(child)
            if not re.fullmatch(rf'{busnum}-\d+', name):
                continue  # skip interface dirs like "3-5:1.0" and sub-hub paths
            try:
                with open(os.path.join(child, 'product')) as fh:
                    prod = fh.read().strip()
            except OSError:
                prod = '?'
            used.append({'port': name.split('-')[1], 'product': prod})
        buses.append({'busnum': busnum, 'speed_mbps': rd('speed'),
                      'usb_version': rd('version'), 'product': rd('product'),
                      'ports': rd('maxchild'), 'used': used})
    return sorted(buses, key=lambda b: int(b['busnum'] or 0))


def zed_status():
    """Work out what part of the camera the host can actually see."""
    usb = scan_usb()
    found = []
    for d in usb:
        if d['vid'] == ZED_VENDOR:
            model, kind = ZED_PIDS.get(d['pid'], ('Unknown Stereolabs device', 'unknown'))
            found.append({**d, 'model': model, 'interface': kind})

    video_ifaces = [d for d in found if d['interface'] == 'video']
    hid_ifaces = [d for d in found if d['interface'] == 'hid']

    # Which /dev/video* nodes (if any) belong to a ZED
    nodes = []
    for dev in v4l2probe.probe_all():
        u = dev.get('usb') or {}
        if u.get('vid') == ZED_VENDOR:
            nodes.append(dev['device'])

    buses = scan_buses()
    ss_buses = [b for b in buses if int(b['speed_mbps'] or 0) >= 5000]
    has_ss_bus = bool(ss_buses)
    on_bus = hid_ifaces[0]['busnum'] if hid_ifaces else (video_ifaces[0]['busnum'] if video_ifaces else None)
    bus_speed = next((int(b['speed_mbps'] or 0) for b in buses if b['busnum'] == on_bus), None)
    # Free SuperSpeed ports the user can physically move the cable to
    free_ss = []
    for b in ss_buses:
        used = {u['port'] for u in b['used']}
        total = int(b['ports'] or 0)
        free_ss.append({'busnum': b['busnum'], 'speed_mbps': b['speed_mbps'],
                        'total_ports': total, 'occupied': sorted(used, key=int),
                        'occupied_by': b['used'], 'free_count': max(0, total - len(used))})

    if video_ifaces and nodes:
        state, verdict = 'ok', 'Видеоинтерфейс ZED присутствует и отдал V4L2-узел.'
    elif video_ifaces and not nodes:
        state = 'partial'
        verdict = ('Видеоинтерфейс ZED виден на USB, но /dev/video* для него не создан — '
                   'вероятно, не загружен модуль uvcvideo или не хватает прав.')
    elif hid_ifaces and not video_ifaces:
        state = 'usb2'
        verdict = (f'Найден ТОЛЬКО HID-интерфейс {hid_ifaces[0]["model"]} '
                   f'(IMU, {ZED_VENDOR}:{hid_ifaces[0]["pid"]}). UVC-видеоинтерфейс '
                   f'({ZED_VENDOR}:{hid_ifaces[0]["pid"][:-1]}0) на шине отсутствует. '
                   f'Камера требует USB 3.0 (SuperSpeed); на USB 2.0 поднимается только HID, '
                   f'видео не энумерируется.')
    elif not found:
        state, verdict = 'absent', 'Ни одного устройства Stereolabs на USB не найдено.'
    else:
        state, verdict = 'unknown', 'Устройство Stereolabs найдено, но интерфейс не опознан.'

    return {
        'state': state, 'verdict': verdict, 'devices': found,
        'video_interfaces': video_ifaces, 'hid_interfaces': hid_ifaces,
        'video_nodes': nodes, 'buses': buses,
        'current_bus_speed_mbps': bus_speed,
        'current_bus': on_bus,
        'superspeed_bus_available': has_ss_bus,
        'free_superspeed_buses': [b['busnum'] for b in ss_buses],
        'superspeed_ports': free_ss,
        'hid_ifaces_expected_video': (
            f"{ZED_VENDOR}:{hid_ifaces[0]['pid'][:-1]}0" if hid_ifaces and not video_ifaces else None),
    }


class Capture:
    """Single open camera, guarded by a lock, with rolling FPS stats."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cap = None
        self.key = None
        self.info = {}
        self.stamps = deque(maxlen=120)
        self.frames = 0
        self.errors = 0
        self.last_error = None
        self.opened_at = None

    def open(self, device, width, height, fps, fourcc):
        key = (device, width, height, fps, fourcc)
        if self.cap is not None and self.key == key and self.cap.isOpened():
            return self.info
        self.close()
        t0 = time.time()
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.last_error = f'Не удалось открыть {device}'
            raise RuntimeError(self.last_error)
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        if width and height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fps:
            cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            self.last_error = (f'Устройство открылось, но кадр не пришёл '
                               f'({width}x{height} {fourcc or "default"}).')
            raise RuntimeError(self.last_error)

        got_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.cap, self.key = cap, key
        self.stamps.clear()
        self.frames, self.errors, self.last_error = 0, 0, None
        self.opened_at = time.time()
        h, w = frame.shape[:2]
        self.info = {
            'device': device,
            'requested': {'width': width, 'height': height, 'fps': fps, 'fourcc': fourcc},
            'actual': {
                'width': w, 'height': h,
                'fps': round(cap.get(cv2.CAP_PROP_FPS), 3),
                'fourcc': ''.join(chr((got_fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip(),
            },
            'open_time_ms': round((time.time() - t0) * 1000, 1),
            'stereo': self.is_stereo(w, h),
            'granted_as_requested': (not width or w == width) and (not height or h == height),
        }
        return self.info

    @staticmethod
    def is_stereo(w, h):
        return (w, h) in _ZED_RES or (h and w / h > 2.4)

    def read(self):
        with self.lock:
            if self.cap is None:
                raise RuntimeError('Камера не открыта')
            ok, frame = self.cap.read()
            if not ok or frame is None:
                self.errors += 1
                self.last_error = 'Пустой кадр от драйвера'
                raise RuntimeError(self.last_error)
            self.frames += 1
            self.stamps.append(time.time())
            return frame

    def measured_fps(self):
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return round((len(self.stamps) - 1) / span, 2) if span > 0 else 0.0

    def stats(self):
        return {**self.info, 'frames': self.frames, 'errors': self.errors,
                'last_error': self.last_error, 'measured_fps': self.measured_fps(),
                'uptime_s': round(time.time() - self.opened_at, 1) if self.opened_at else 0,
                'open': self.cap is not None and self.cap.isOpened()}

    def close(self):
        if self.cap is not None:
            self.cap.release()
        self.cap, self.key, self.info = None, None, {}
        self.stamps.clear()
        self.opened_at = None


CAP = Capture()


def slice_eye(frame, eye):
    h, w = frame.shape[:2]
    if not Capture.is_stereo(w, h) or eye == 'both':
        return frame
    half = w // 2
    if eye == 'left':
        return frame[:, :half]
    if eye == 'right':
        return frame[:, half:]
    if eye == 'anaglyph':
        left, right = frame[:, :half], frame[:, half:half * 2]
        out = np.zeros_like(left)
        out[:, :, 2] = left[:, :, 2]           # red from left
        out[:, :, 0] = right[:, :, 0]          # blue from right
        out[:, :, 1] = right[:, :, 1]          # green from right
        return out
    if eye == 'diff':
        left, right = frame[:, :half], frame[:, half:half * 2]
        return cv2.absdiff(left, right)
    return frame


def annotate(img, stats, eye):
    h, w = img.shape[:2]
    lines = [
        f'{stats["actual"]["width"]}x{stats["actual"]["height"]} {stats["actual"]["fourcc"]}  eye={eye}',
        f'measured {stats["measured_fps"]} fps / driver {stats["actual"]["fps"]}',
        f'frames {stats["frames"]}  errors {stats["errors"]}',
    ]
    scale = max(0.45, min(1.2, w / 1600))
    y = int(24 * scale) + 6
    pad = int(8 * scale)
    box_h = int(len(lines) * 26 * scale) + pad
    cv2.rectangle(img, (0, 0), (int(560 * scale), box_h), (0, 0, 0), -1)
    for line in lines:
        cv2.putText(img, line, (pad, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, (0, 255, 120),
                    max(1, int(1.6 * scale)), cv2.LINE_AA)
        y += int(26 * scale)
    return img


def _params():
    return (request.args.get('device', '/dev/video0'),
            int(request.args.get('width', 0) or 0),
            int(request.args.get('height', 0) or 0),
            float(request.args.get('fps', 0) or 0),
            request.args.get('fourcc', '') or None,
            request.args.get('eye', 'both'),
            int(request.args.get('maxw', 1280) or 1280),
            request.args.get('overlay', '1') == '1')


@app.route('/api/diag')
def api_diag():
    return jsonify({
        'timestamp': time.time(),
        'zed': zed_status(),
        'v4l2': v4l2probe.probe_all(),
        'usb': scan_usb(),
        'zed_modes': ZED_MODES,
        'capture': CAP.stats(),
        'opencv': cv2.__version__,
    })


@app.route('/api/stats')
def api_stats():
    return jsonify(CAP.stats())


@app.route('/api/open')
def api_open():
    device, w, h, fps, fourcc, *_ = _params()
    try:
        with CAP.lock:
            info = CAP.open(device, w, h, fps, fourcc)
        return jsonify({'ok': True, 'info': info})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@app.route('/api/close')
def api_close():
    with CAP.lock:
        CAP.close()
    return jsonify({'ok': True})


@app.route('/api/scan')
def api_scan():
    """Try every ZED stereo mode on a device and report which ones actually stream."""
    device = request.args.get('device', '/dev/video0')
    fourcc = request.args.get('fourcc', '') or None
    results = []
    with CAP.lock:
        CAP.close()
        for mode in ZED_MODES:
            entry = {'mode': mode['name'], 'width': mode['width'], 'height': mode['height']}
            try:
                info = CAP.open(device, mode['width'], mode['height'], mode['fps'][0], fourcc)
                for _ in range(10):
                    CAP.cap.read()
                entry.update({'ok': True, 'actual': info['actual'],
                              'exact': info['granted_as_requested'],
                              'open_time_ms': info['open_time_ms']})
            except Exception as e:
                entry.update({'ok': False, 'error': str(e)})
            CAP.close()
            results.append(entry)
    return jsonify({'device': device, 'results': results})


def mjpeg(device, w, h, fps, fourcc, eye, maxw, overlay):
    try:
        with CAP.lock:
            CAP.open(device, w, h, fps, fourcc)
    except Exception as e:
        img = np.zeros((240, 640, 3), np.uint8)
        cv2.putText(img, str(e)[:70], (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode('.jpg', img)
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
        return

    while True:
        try:
            frame = CAP.read()
        except Exception:
            time.sleep(0.05)
            if CAP.errors > 50:
                return
            continue
        img = slice_eye(frame, eye)
        if maxw and img.shape[1] > maxw:
            scale = maxw / img.shape[1]
            img = cv2.resize(img, (maxw, int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        if overlay:
            img = annotate(img, CAP.stats(), eye)
        ok, buf = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ok:
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'


@app.route('/stream')
def stream():
    args = _params()
    return Response(mjpeg(*args), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/snapshot')
def snapshot():
    device, w, h, fps, fourcc, eye, maxw, overlay = _params()
    try:
        with CAP.lock:
            CAP.open(device, w, h, fps, fourcc)
        frame = CAP.read()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    img = slice_eye(frame, eye)
    ok, buf = cv2.imencode('.png', img)
    return send_file(io.BytesIO(buf.tobytes()), mimetype='image/png')


@app.route('/')
def index():
    with open(os.path.join(os.path.dirname(__file__), 'index.html')) as f:
        return f.read()


if __name__ == '__main__' and not os.environ.get('ZEDCHECK_NO_SERVER'):
    app.run(host='127.0.0.1', port=8420, threaded=True, debug=False)
