"""Render the source radio SVG into a multi-resolution Windows icon."""
from pathlib import Path
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
svg = (ROOT / 'assets' / 'radio.svg').read_bytes()
renderer = QSvgRenderer(QByteArray(svg))
images = []
for size in (16, 24, 32, 48, 64, 128, 256):
    qimage = QImage(size, size, QImage.Format.Format_RGBA8888)
    qimage.fill(Qt.GlobalColor.transparent)
    painter = QPainter(qimage)
    renderer.render(painter)
    painter.end()
    images.append(Image.frombytes('RGBA', (size, size), bytes(qimage.bits())))
images[-1].save(ROOT / 'radio.ico', format='ICO', append_images=images[:-1],
                sizes=[(n.width, n.height) for n in images])
