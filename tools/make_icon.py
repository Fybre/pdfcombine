"""Generate the app icon: assets/icon.png, assets/icon.ico and (on macOS) assets/icon.icns.

Run with: python tools/make_icon.py
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
S = 1024  # master canvas size


def page_path(w, h, fold):
    """A page outline with a folded top-right corner, origin at its top-left."""
    path = QPainterPath()
    r = 18
    path.moveTo(r, 0)
    path.lineTo(w - fold, 0)
    path.lineTo(w, fold)
    path.lineTo(w, h - r)
    path.quadTo(w, h, w - r, h)
    path.lineTo(r, h)
    path.quadTo(0, h, 0, h - r)
    path.lineTo(0, r)
    path.quadTo(0, 0, r, 0)
    return path


def draw_page(p, cx, cy, angle, fill, lines_color, shadow=True):
    w, h, fold = 400, 520, 110
    p.save()
    p.translate(cx, cy)
    p.rotate(angle)
    p.translate(-w / 2, -h / 2)
    outline = page_path(w, h, fold)
    if shadow:
        p.save()
        p.translate(0, 14)
        p.fillPath(outline, QColor(0, 0, 0, 55))
        p.restore()
    p.fillPath(outline, fill)
    # folded corner
    corner = QPainterPath()
    corner.moveTo(w - fold, 0)
    corner.lineTo(w - fold, fold - 14)
    corner.quadTo(w - fold, fold, w - fold + 14, fold)
    corner.lineTo(w, fold)
    corner.closeSubpath()
    p.fillPath(corner, QColor(0, 0, 0, 40))
    # text lines
    if lines_color is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(lines_color)
        y = 175
        for frac in (0.72, 0.62, 0.72, 0.5):
            p.drawRoundedRect(QRectF(52, y, (w - 104) * frac, 26), 13, 13)
            y += 62
    p.restore()


def render(size):
    img = QImage(S, S, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # rounded-square background (macOS grid: 824px body inside a 1024 canvas)
    body = QRectF(100, 100, 824, 824)
    p.save()
    p.translate(0, 12)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 70))
    p.drawRoundedRect(body, 185, 185)
    p.restore()
    grad = QLinearGradient(body.topLeft(), body.bottomLeft())
    grad.setColorAt(0, QColor("#F2564F"))
    grad.setColorAt(1, QColor("#B5202A"))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(body, 185, 185)

    # three fanned pages being combined
    draw_page(p, 430, 485, -13, QColor("#F6D3D1"), None)
    draw_page(p, 594, 485, 13, QColor("#F6D3D1"), None)
    draw_page(p, 512, 470, 0, QColor("#FFFFFF"), QColor("#E9B4B1"))

    # merge badge: circle with a plus
    c = QPointF(512, 735)
    p.setPen(QPen(QColor("#FFFFFF"), 18))
    p.setBrush(QColor("#2E7D5B"))
    p.drawEllipse(c, 112, 112)
    p.setPen(QPen(QColor("#FFFFFF"), 40, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(c.x() - 52, c.y()), QPointF(c.x() + 52, c.y()))
    p.drawLine(QPointF(c.x(), c.y() - 52), QPointF(c.x(), c.y() + 52))
    p.end()

    if size != S:
        img = img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img


def png_bytes(img):
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(path, sizes):
    """ICO with PNG-compressed entries (supported since Windows Vista)."""
    blobs = [png_bytes(render(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, blob in zip(sizes, blobs):
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as fh:
        fh.write(header + entries + b"".join(blobs))


def write_icns(path):
    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("iconutil not found (not macOS) - skipping .icns")
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, "icon.iconset")
        os.mkdir(iconset)
        for base in (16, 32, 128, 256, 512):
            render(base).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
            render(base * 2).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
        subprocess.run([iconutil, "-c", "icns", iconset, "-o", path], check=True)


def main():
    QGuiApplication(sys.argv)
    os.makedirs(ASSETS, exist_ok=True)
    render(512).save(os.path.join(ASSETS, "icon.png"))
    write_ico(os.path.join(ASSETS, "icon.ico"), [16, 24, 32, 48, 64, 128, 256])
    write_icns(os.path.join(ASSETS, "icon.icns"))
    print("Wrote icons to", ASSETS)


if __name__ == "__main__":
    main()
