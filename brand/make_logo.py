import os, sys

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)

INK = "#111111"
WHITE = "#FFFFFF"
AMBER = "#EF9F27"
DOT = "#F9A962"   # the logo dot: the painted sun's hue, a little deeper so it reads at small sizes
SW = 6.5  # wordmark stroke

# ---- monoline geometric lowercase, baseline y=100, x-height top y=60, ascender 36, descender 120
def s(x):
    return (f"M{x+28} 65 C{x+24} 60.5 {x+18} 60 {x+15} 60 C{x+6} 60 {x+2} 64 {x+2} 70 "
            f"C{x+2} 77 {x+10} 78.5 {x+15} 80 C{x+21} 81.5 {x+28} 83 {x+28} 90 "
            f"C{x+28} 96 {x+23} 100 {x+15} 100 C{x+10} 100 {x+5} 99 {x+2} 95")
def u(x):
    return f"M{x} 60 V82 a18 18 0 0 0 36 0 V60 M{x+36} 60 V100"
def b(x):
    return f"M{x} 36 V100 M{x} 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0"
def i_stem(x):
    return f"M{x} 60 V100"
def g(x):
    return (f"M{x} 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 "
            f"M{x+40} 60 V104 C{x+40} 115 {x+32} 120 {x+21} 120 C{x+12} 120 {x+6} 117 {x+3} 113")
def m(x):
    return f"M{x} 60 V100 M{x} 77 a17 17 0 0 1 34 0 V100 M{x+34} 77 a17 17 0 0 1 34 0 V100"
def o(x):
    return f"M{x} 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0"
def d(x):
    return f"M{x} 80 a20 20 0 1 0 40 0 a20 20 0 1 0 -40 0 M{x+40} 36 V100"

# optical spacing: round-round 10, round-straight 13, straight-straight 16
letters = [("s", s, 0), ("u", u, 43), ("b", b, 95), ("s", s, 145), ("i", i_stem, 188),
           ("g", g, 201), ("m", m, 257), ("o", o, 338), ("i", i_stem, 391), ("d", d, 404)]
WORD_W = 444

def word_paths(dx, color):
    d_all = " ".join(fn(x + dx) for _, fn, x in letters)
    dots = "".join(f'<circle cx="{x+dx}" cy="46" r="{SW*0.62:.2f}" fill="{color}"/>'
                   for name, _, x in letters if name == "i")
    return (f'<path d="{d_all}" fill="none" stroke="{color}" stroke-width="{SW}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>{dots}')

# ---- the mark: sigmoid with a point under it (120x120 box)
def mark(color, dx=0, dy=0, sw=10):
    return (f'<path d="M{14+dx} {92+dy} C{60+dx} {92+dy} {60+dx} {28+dy} {106+dx} {28+dy}" '
            f'fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<circle cx="{91+dx}" cy="{57+dy}" r="8" fill="{DOT}"/>')

def svg(vb, body, w, h, bg=None):
    bgr = f'<rect x="{vb[0]}" y="{vb[1]}" width="{vb[2]}" height="{vb[3]}" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(map(str, vb))}" '
            f'width="{w}" height="{h}">{bgr}{body}</svg>\n')

def write(name, text):
    with open(os.path.join(OUT, name), "w") as f:
        f.write(text)

# mark
write("subsigmoid-mark.svg", svg((0, 0, 120, 120), mark(INK), 512, 512))
write("subsigmoid-mark-white.svg", svg((0, 0, 120, 120), mark(WHITE), 512, 512))

# app icon tile
icon = (f'<rect width="120" height="120" rx="27" fill="{INK}"/>'
        f'<path d="M20 88 C60 88 60 32 100 32" fill="none" stroke="{WHITE}" stroke-width="10" stroke-linecap="round"/>'
        f'<circle cx="88" cy="59" r="8" fill="{DOT}"/>')
write("subsigmoid-icon.svg", svg((0, 0, 120, 120), icon, 512, 512))
icon_light = icon.replace(f'fill="{INK}"', 'fill="#FFFFFF"').replace(f'stroke="{WHITE}"', f'stroke="{INK}"')
write("subsigmoid-icon-light.svg", svg((0, 0, 120, 120), icon_light, 512, 512))

# wordmark only
write("subsigmoid-wordmark.svg", svg((-8, 26, WORD_W + 16, 104), word_paths(0, INK), (WORD_W + 16) * 2, 208))
write("subsigmoid-wordmark-white.svg", svg((-8, 26, WORD_W + 16, 104), word_paths(0, WHITE), (WORD_W + 16) * 2, 208))

# horizontal lockup: mark (shifted so the curve spans ascender..baseline) + wordmark
WX = 138
lock_vb = (0, 20, WX + WORD_W + 14, 116)
lock = lambda c: mark(c, dx=0, dy=8, sw=9) + word_paths(WX, c)
write("subsigmoid-lockup.svg", svg(lock_vb, lock(INK), lock_vb[2] * 2, lock_vb[3] * 2))
write("subsigmoid-lockup-white.svg", svg(lock_vb, lock(WHITE), lock_vb[2] * 2, lock_vb[3] * 2))

# preview sheet (light + dark) for checking
prev = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 1200" width="1200" height="1200">'
        f'<rect width="1200" height="600" fill="#FFFFFF"/><rect y="600" width="1200" height="600" fill="#111111"/>'
        f'<g transform="translate(60 40) scale(1.6)">{lock(INK)}</g>'
        f'<g transform="translate(60 330) scale(1.2)">{mark(INK)}</g>'
        f'<g transform="translate(260 330) scale(1.2)">{icon}</g>'
        f'<g transform="translate(60 640) scale(1.6)">{lock(WHITE)}</g>'
        f'<g transform="translate(60 930) scale(1.2)">{mark(WHITE)}</g>'
        f'<g transform="translate(260 930) scale(1.2)">{icon_light}</g>'
        f'</svg>')
write("preview.svg", prev)
print("ok")
