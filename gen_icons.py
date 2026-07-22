from PIL import Image, ImageDraw
import os

PINE = (35, 83, 71)
PAPER = (250, 246, 238)
SAFFRON = (224, 145, 47)
LEAF = (107, 142, 78)
INK = (34, 30, 26)


def rr(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def draw_icon(size, maskable=False):
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if maskable:
        d.rectangle([0, 0, S, S], fill=PINE)
    else:
        rr(d, [0, 0, S, S], int(S * 0.22), PINE)

    pad = int(S * 0.20) if maskable else int(S * 0.16)
    x0, y0 = pad, pad
    w = h = S - 2 * pad

    cw = int(w * 0.86)
    ch = int(h * 0.98)
    cx0 = x0 + (w - cw) // 2
    cy0 = y0 + (h - ch) // 2
    rr(d, [cx0, cy0, cx0 + cw, cy0 + ch], int(cw * 0.10), PAPER)

    rows = 4
    lx = cx0 + int(cw * 0.14)
    row_h = ch / (rows + 1.4)
    ry = cy0 + row_h * 0.9
    br = int(cw * 0.055)
    lh = int(cw * 0.055)

    for i in range(rows):
        cy = int(ry + i * row_h)
        done = (i == rows - 1)
        col = LEAF if done else SAFFRON
        d.ellipse([lx, cy - br, lx + 2 * br, cy + br], fill=col)
        if done:
            cxc = lx + br
            d.line(
                [(cxc - br * 0.45, cy + br * 0.02),
                 (cxc - br * 0.08, cy + br * 0.45),
                 (cxc + br * 0.55, cy - br * 0.45)],
                fill=PAPER, width=max(2, int(br * 0.35)), joint="curve")
        tx = lx + 2 * br + int(cw * 0.06)
        lw = int(cw * (0.52 if i % 2 == 0 else 0.42))
        rr(d, [tx, cy - lh // 2, tx + lw, cy + lh // 2], lh // 2,
           INK if not done else (150, 150, 145))

    lr = int(cw * 0.16)
    lcx = cx0 + cw - int(cw * 0.14)
    lcy = cy0 + int(cw * 0.02)
    d.polygon([(lcx, lcy - lr), (lcx + lr, lcy), (lcx, lcy + lr), (lcx - lr, lcy)], fill=LEAF)
    d.line([(lcx, lcy - lr * 0.7), (lcx, lcy + lr * 0.7)], fill=PAPER,
           width=max(2, int(lr * 0.14)))

    return img.resize((size, size), Image.LANCZOS)


os.makedirs("icons", exist_ok=True)
draw_icon(192).save("icons/icon-192.png")
draw_icon(512).save("icons/icon-512.png")
draw_icon(512, maskable=True).save("icons/icon-maskable-512.png")
draw_icon(64).save("icons/favicon-64.png")

at = Image.new("RGBA", (180, 180), PINE)
at.alpha_composite(draw_icon(180))
at.convert("RGB").save("icons/apple-touch-icon.png")

print("icons generated")
