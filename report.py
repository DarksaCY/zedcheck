#!/usr/bin/env python3
"""Текстовый отчёт по состоянию камеры ZED — то же, что показывает веб-панель."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('ZEDCHECK_NO_SERVER', '1')
import app  # noqa: E402

C = {'ok': '\033[32m', 'warn': '\033[33m', 'err': '\033[31m', 'dim': '\033[90m', 'b': '\033[1m', '0': '\033[0m'}


def c(k, s):
    return f"{C[k]}{s}{C['0']}" if sys.stdout.isatty() else str(s)


def speed(mbps):
    v = int(mbps or 0)
    name = ('USB 3.1 SS+' if v >= 10000 else 'USB 3.0 SS' if v >= 5000 else
            'USB 2.0 HS' if v >= 480 else 'USB 1.1 FS' if v >= 12 else '?')
    return c('ok' if v >= 5000 else 'warn' if v >= 480 else 'dim', f'{v:>5} Mb/s ({name})')


def main():
    z = app.zed_status()
    print(c('b', '\n╔═ ДИАГНОСТИКА КАМЕРЫ ZED ═══════════════════════════════════════════'))

    tone = {'ok': 'ok', 'partial': 'warn', 'usb2': 'err', 'absent': 'err'}.get(z['state'], 'warn')
    print(f"\n  Статус: {c(tone, z['state'].upper())}")
    for line in z['verdict'].split('. '):
        if line.strip():
            print(f"  {line.strip().rstrip('.')}.")

    print(c('b', '\n── Устройства Stereolabs на USB ─────────────────────────────────────'))
    if z['devices']:
        for d in z['devices']:
            mark = c('ok', '✓ ВИДЕО') if d['interface'] == 'video' else c('dim', '· HID  ')
            print(f"  {mark}  {d['vid']}:{d['pid']}  {d['model']:<10} bus {d['busnum']}/{d['devnum']}  {speed(d['speed_mbps'])}")
    else:
        print(c('err', '  ничего не найдено'))
    if z['hid_ifaces_expected_video'] :
        print(c('err', f"  ✗ ОТСУТСТВУЕТ  {z['hid_ifaces_expected_video']}  (UVC-видеоинтерфейс — именно он даёт картинку)"))

    print(c('b', '\n── Шины USB хоста ───────────────────────────────────────────────────'))
    for b in z['buses']:
        used = ', '.join(f"порт {u['port']}: {u['product']}" for u in b['used']) or c('dim', 'пусто')
        here = c('warn', '  ← здесь ZED') if b['busnum'] == z['current_bus'] else ''
        print(f"  bus {b['busnum']}  {speed(b['speed_mbps'])}  USB{b['usb_version']}  портов: {b['ports']:>2}{here}")
        print(f"        {used}")

    if z['state'] == 'usb2':
        print(c('b', '\n── Что делать ───────────────────────────────────────────────────────'))
        free = z['superspeed_ports']
        if free:
            print("  1. Переткнуть камеру в порт USB 3.0 (синий разъём / маркировка SS).")
            for p in free:
                print(f"     bus {p['busnum']}: свободно {p['free_count']} из {p['total_ports']} портов")
            print("  2. Если уже в синем порту — проверить кабель/удлинитель/хаб:")
            print("     USB 2.0-удлинитель даёт ровно такую же картину (HID виден, видео нет).")
            print("  3. После переподключения перезапустить этот отчёт — должен появиться 2b03:f680.")
        else:
            print(c('err', "  На хосте нет SuperSpeed-шин — камеру здесь запустить не получится."))

    print(c('b', '\n── Узлы V4L2 ────────────────────────────────────────────────────────'))
    for d in app.v4l2probe.probe_all():
        u = d.get('usb') or {}
        is_zed = u.get('vid') == app.ZED_VENDOR
        tag = c('ok', '[ZED]') if is_zed else c('dim', '[не ZED]')
        print(f"  {d['device']}  {tag}  {d.get('card', '?')}  ({u.get('vid')}:{u.get('pid')})")
        if not d.get('is_capture'):
            print(c('dim', '      не capture-устройство'))
        for f in d.get('formats', []):
            sizes = ', '.join(f"{s['width']}×{s['height']}" + (f"@{'/'.join(map(str, s['fps']))}" if s.get('fps') else '')
                              for s in f['sizes'] if s.get('width'))
            print(f"      {f['fourcc']:<5} {sizes}")

    print()


if __name__ == '__main__':
    main()
