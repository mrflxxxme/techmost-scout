"""Считает контраст по снятым кадрам. Декодер PNG написан здесь потому,
что в среде нет ни одной библиотеки изображений, а число обязано быть
измеренным, а не выведенным из токенов."""
import json, os, struct, sys, zlib
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))


def read_png(path):
    d = open(path, 'rb').read()
    assert d[:8] == b'\x89PNG\r\n\x1a\n', path
    pos, idat, w, h, bd, ct = 8, b'', 0, 0, 0, 0
    while pos < len(d):
        ln = struct.unpack('>I', d[pos:pos + 4])[0]
        typ, body = d[pos + 4:pos + 8], d[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b'IHDR':
            w, h, bd, ct = struct.unpack('>IIBB', body[:10])
        elif typ == b'IDAT':
            idat += body
        elif typ == b'IEND':
            break
    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ct]
    bpp, stride = ch * bd // 8, w * ch * bd // 8
    out, prev, i = bytearray(), bytearray(stride), 0
    for _ in range(h):
        ft = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ft == 1: line[x] = (line[x] + a) & 255
            elif ft == 2: line[x] = (line[x] + b) & 255
            elif ft == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif ft == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out += line
        prev = line
    px = []
    for y in range(h):
        for x in range(w):
            o = y * stride + x * bpp
            px.append((out[o], out[o + 1], out[o + 2]) if ch >= 3 else (out[o],) * 3)
    return px


def lum(c):
    f = []
    for v in c:
        v /= 255
        f.append(v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4)
    return .2126 * f[0] + .7152 * f[1] + .0722 * f[2]


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


best = {}
for name, f in json.load(open(os.path.join(HERE, 'clips.json'))):
    px = read_png(os.path.join(HERE, f))
    cnt = Counter(px)
    bg = cnt.most_common(1)[0][0]
    lbg = lum(bg)
    # Мелкий текст сглажен: ни один его оттенок в одиночку не набирает
    # заметной доли кадра. Поэтому цвет строки берётся не как самый частый,
    # а как медиана хвоста пикселей, наиболее далёких от фона по светлоте.
    far = sorted(px, key=lambda c: -abs(lum(c) - lbg))
    tail = far[:max(4, len(px) // 120)]   # ядро глифа, а не его сглаженная кромка
    ink = sorted(tail, key=lum)[len(tail) // 2]
    if ratio(ink, bg) < 1.6:
        best.setdefault(name, None)
        continue
    fg = ink
    # На градиентной поверхности самого частого цвета нет: её оттенки размазаны
    # по сотне корзин, и плоский белый текст обходит любую из них поштучно.
    # Кто из двух подложка, решает не частота одного значения, а доля пикселей
    # рядом с ним по светлоте.
    def share(c):
        lc = lum(c)
        return sum(1 for q in px if abs(lum(q) - lc) < .02)
    if share(fg) > share(bg):
        bg, fg = fg, bg
    cr = ratio(fg, bg)
    prev = best.get(name)
    # из трёх полос берётся худшая по контрасту: замер должен ловить провал,
    # а не лучший из кадров
    if prev is None or cr < prev[2]:
        best[name] = (bg, fg, cr)

rows, bad = [], []
for name, v in best.items():
    if v is None:
        rows.append((name, None, None, None, 'текста в кадре нет'))
        continue
    bg, fg, cr = v
    verd = 'AA' if cr >= 4.5 else ('AA только крупный' if cr >= 3 else 'НИЖЕ AA')
    if cr < 4.5:
        bad.append((name, round(cr, 2)))
    rows.append((name, bg, fg, cr, verd))

print('контраст текста на живых пикселях, 1440×900:')
for name, bg, fg, cr, verd in rows:
    if fg is None:
        print(f'  {name:<24} {verd}')
    else:
        print(f'  {name:<24} фон {bg}  текст {fg}  {cr:5.2f}:1  {verd}')
print('ниже AA:', bad if bad else 'нет')
sys.exit(1 if bad else 0)
