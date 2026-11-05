// Draws the VBMS Manager app icon (1024 px PNG): three cells filled to a
// charge-limit line, on a dark rounded square.
//
//     swift macos/make_icon.swift out.png

import AppKit

let S: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

func linear(_ ctx: CGContext, _ colors: [CGColor], from: CGPoint, to: CGPoint) {
    let g = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB), colors: colors as CFArray, locations: nil)!
    ctx.drawLinearGradient(g, start: from, end: to, options: [])
}

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(S), pixelsHigh: Int(S), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// macOS icon grid: 824 px rounded square centred on the 1024 canvas, soft shadow.
let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: rgb(0x000000, 0.35))
ctx.addPath(tilePath); ctx.setFillColor(rgb(0x101820)); ctx.fillPath()
ctx.restoreGState()
ctx.saveGState()
ctx.addPath(tilePath); ctx.clip()
linear(ctx, [rgb(0x24364A), rgb(0x0C131B)], from: CGPoint(x: 0, y: tile.maxY), to: CGPoint(x: 0, y: tile.minY))
ctx.restoreGState()

// Three cells.
let cellW: CGFloat = 168, gap: CGFloat = 54, cellH: CGFloat = 500
let x0 = (S - (3 * cellW + 2 * gap)) / 2, y0: CGFloat = 230
let limitY = y0 + cellH * 0.78
for i in 0..<3 {
    let r = CGRect(x: x0 + CGFloat(i) * (cellW + gap), y: y0, width: cellW, height: cellH)
    let body = CGPath(roundedRect: r, cornerWidth: 34, cornerHeight: 34, transform: nil)
    // positive terminal
    let cap = CGRect(x: r.midX - 34, y: r.maxY - 6, width: 68, height: 34)
    ctx.addPath(CGPath(roundedRect: cap, cornerWidth: 10, cornerHeight: 10, transform: nil))
    ctx.setFillColor(rgb(0x5B6B7C)); ctx.fillPath()
    // empty shell
    ctx.addPath(body); ctx.setFillColor(rgb(0x1B2733)); ctx.fillPath()
    // charge, up to the limit line
    ctx.saveGState()
    ctx.addPath(body); ctx.clip()
    ctx.clip(to: CGRect(x: r.minX, y: r.minY, width: r.width, height: limitY - r.minY))
    linear(ctx, [rgb(0x6EE7A8), rgb(0x16A36A)], from: CGPoint(x: 0, y: limitY), to: CGPoint(x: 0, y: r.minY))
    // a soft highlight down the left of each cell
    ctx.setFillColor(rgb(0xFFFFFF, 0.14))
    ctx.fill(CGRect(x: r.minX + 22, y: r.minY, width: 26, height: cellH))
    ctx.restoreGState()
    ctx.addPath(body); ctx.setStrokeColor(rgb(0x3A4B5C)); ctx.setLineWidth(8); ctx.strokePath()
}

// Charge-limit line with its drag handle, like the line on the Cells chart.
let lx0 = x0 - 36, lx1 = x0 + 3 * cellW + 2 * gap + 14
ctx.setStrokeColor(rgb(0xFBBF24)); ctx.setLineWidth(16); ctx.setLineCap(.round)
ctx.setLineDash(phase: 0, lengths: [44, 30])
ctx.move(to: CGPoint(x: lx0, y: limitY)); ctx.addLine(to: CGPoint(x: lx1, y: limitY)); ctx.strokePath()
ctx.setLineDash(phase: 0, lengths: [])
let knob = CGRect(x: lx1 - 6, y: limitY - 30, width: 60, height: 60)
ctx.setFillColor(rgb(0xFBBF24)); ctx.fillEllipse(in: knob)
ctx.setFillColor(rgb(0x0C131B)); ctx.fillEllipse(in: knob.insetBy(dx: 19, dy: 19))

NSGraphicsContext.current = nil
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
