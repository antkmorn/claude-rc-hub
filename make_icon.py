#!/usr/bin/env python3
"""Рисует иконку ✳︎ для Klod remoteHub.app и собирает из неё .icns.

Использование: make_icon.py <путь/к/AppIcon.icns>
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSMakeRect,
    NSPNGFileType,
    NSString,
)


def render(size, path):
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    )

    # скруглённый квадрат в стиле macOS с отступом, как у системных иконок
    inset = size * 0.1
    box = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = box.size.width * 0.225
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.47, 0.34, 1).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, radius, radius).fill()

    glyph = NSString.stringWithString_("✳")
    attrs = {
        NSFontAttributeName: NSFont.systemFontOfSize_(size * 0.55),
        NSForegroundColorAttributeName: NSColor.whiteColor(),
    }
    ts = glyph.sizeWithAttributes_(attrs)
    glyph.drawAtPoint_withAttributes_(
        ((size - ts.width) / 2, (size - ts.height) / 2), attrs
    )

    NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(
        str(path), True
    )


def main(out):
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            render(base, iconset / f"icon_{base}x{base}.png")
            render(base * 2, iconset / f"icon_{base}x{base}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", out], check=True)


if __name__ == "__main__":
    main(sys.argv[1])
