// Bluetooth for the Python server, which can't use CoreBluetooth itself.
//
// Listens on 127.0.0.1 (random port, passed to the server as VBMS_BLE_PORT with
// a per-run VBMS_BLE_TOKEN). Each connection starts with one JSON line:
//
//   {"token": …, "op": "state"}                      -> {"ok": true, "state": "on"}
//   {"token": …, "op": "scan", "secs": 4, "names": […]} -> {"ok": true, "found": [{id, name, rssi, nus}]}
//   {"token": …, "op": "connect", "id": UUID, "timeout": 10}
//        -> {"ok": true, "name": …} and from then on the socket is a raw byte pipe to the
//           board's VESC UART service (the same packets as over TCP), until either side closes.
//
// Errors reply {"ok": false, "error": …} and close. CoreBluetooth only starts (and macOS
// only asks for Bluetooth permission) on the first request.

import CoreBluetooth
import Foundation

let nusService = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
let nusRX = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")   // we write
let nusTX = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")   // board notifies

final class BLEBridge: NSObject, CBCentralManagerDelegate {
    let token = (0..<24).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
    private(set) var port: UInt16 = 0
    let q = DispatchQueue(label: "vbms.ble")
    private var listenFD: Int32 = -1
    private var acceptSrc: DispatchSourceRead?
    private var central: CBCentralManager?
    private var waitingForState: [() -> Void] = []
    private var seen: [UUID: (p: CBPeripheral, name: String, rssi: Int, nus: Bool, at: Date)] = [:]
    private var scanners = 0
    fileprivate var links: [UUID: Link] = [:]

    /// Binds the listener. Returns false if it couldn't (Bluetooth then just isn't offered).
    func start() -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let ok = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, len) == 0 && listen(fd, 16) == 0 && getsockname(fd, $0, &len) == 0
            }
        }
        guard ok else { close(fd); return false }
        port = UInt16(bigEndian: addr.sin_port)
        listenFD = fd
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)   // the Python child must not inherit it
        let src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: q)
        src.setEventHandler { [weak self] in
            let c = accept(fd, nil, nil)
            // Read the request line off q, so a slow client can't stall Bluetooth.
            if c >= 0 { DispatchQueue.global().async { self?.serve(c) } }
        }
        src.resume()
        acceptSrc = src
        return true
    }

    // MARK: requests

    private func serve(_ fd: Int32) {
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        var one: Int32 = 1
        setsockopt(fd, Int32(IPPROTO_TCP), TCP_NODELAY, &one, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        // The request line is short; read it with a deadline so a stuck client can't hold a thread.
        var tv = timeval(tv_sec: 5, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var line = Data()
        var b = [UInt8](repeating: 0, count: 1)
        while line.count < 4096 {
            if read(fd, &b, 1) != 1 { close(fd); return }
            if b[0] == 10 { break }
            line.append(b[0])
        }
        tv = timeval(tv_sec: 0, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        guard let req = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              req["token"] as? String == token else { close(fd); return }
        q.async { [self] in withCentral { [self] in
            switch req["op"] as? String {
            case "state":
                reply(fd, ["ok": true, "state": stateName()], close: true)
            case "scan":
                guard central?.state == .poweredOn else { return fail(fd, stateProblem()) }
                scan(fd, secs: min(max(req["secs"] as? Double ?? 4, 1), 15), names: req["names"] as? [String] ?? [])
            case "connect":
                guard central?.state == .poweredOn else { return fail(fd, stateProblem()) }
                guard let s = req["id"] as? String, let id = UUID(uuidString: s) else { return fail(fd, "bad id") }
                connect(fd, id: id, timeout: min(max(req["timeout"] as? Double ?? 10, 2), 30))
            default:
                fail(fd, "unknown op")
            }
        } }
    }

    /// Runs `f` on q once CoreBluetooth knows whether Bluetooth is usable.
    private func withCentral(_ f: @escaping () -> Void) {
        if central == nil { central = CBCentralManager(delegate: self, queue: q) }
        if central!.state == .unknown || central!.state == .resetting { waitingForState.append(f) } else { f() }
    }

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        if c.state == .unknown || c.state == .resetting { return }
        let w = waitingForState
        waitingForState = []
        w.forEach { $0() }
        if c.state != .poweredOn {
            for l in links.values { l.finish("Bluetooth turned off") }
        }
    }

    private func stateName() -> String {
        switch central?.state {
        case .poweredOn: return "on"
        case .poweredOff: return "off"
        case .unauthorized: return "denied"
        case .unsupported: return "unsupported"
        default: return "unknown"
        }
    }

    private func stateProblem() -> String {
        switch central?.state {
        case .poweredOff: return "Bluetooth is turned off on this Mac."
        case .unauthorized: return "VBMS Manager isn't allowed to use Bluetooth. Allow it in System Settings → Privacy & Security → Bluetooth."
        case .unsupported: return "This Mac has no Bluetooth LE."
        default: return "Bluetooth isn't ready."
        }
    }

    fileprivate func reply(_ fd: Int32, _ obj: [String: Any], close c: Bool) {
        var d = (try? JSONSerialization.data(withJSONObject: obj)) ?? Data("{}".utf8)
        d.append(10)
        _ = writeAll(fd, d)
        if c { close(fd) }
    }

    fileprivate func fail(_ fd: Int32, _ msg: String) { reply(fd, ["ok": false, "error": msg], close: true) }

    // MARK: scan

    private func scan(_ fd: Int32, secs: Double, names: [String]) {
        let started = Date()
        scanners += 1
        if scanners == 1 {
            central!.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        }
        q.asyncAfter(deadline: .now() + secs) { [self] in
            scanners -= 1
            if scanners == 0 { central?.stopScan() }
            let want = Set(names.map { $0.lowercased() })
            let found = seen.values.filter { e in
                e.at >= started && (e.nus || e.name.lowercased().contains("vesc") || want.contains(e.name.lowercased()))
            }.map { e -> [String: Any] in
                ["id": e.p.identifier.uuidString, "name": e.name, "rssi": e.rssi, "nus": e.nus]
            }
            reply(fd, ["ok": true, "found": found], close: true)
        }
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData ad: [String: Any],
                        rssi: NSNumber) {
        let uuids = (ad[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID] ?? [])
            + (ad[CBAdvertisementDataOverflowServiceUUIDsKey] as? [CBUUID] ?? [])
        let old = seen[p.identifier]
        let name = ad[CBAdvertisementDataLocalNameKey] as? String ?? p.name ?? old?.name ?? ""
        let r = rssi.intValue == 127 ? (old?.rssi ?? 0) : rssi.intValue   // 127 = not available
        seen[p.identifier] = (p, name, r, uuids.contains(nusService) || (old?.nus ?? false), Date())
    }

    // MARK: connect

    private func connect(_ fd: Int32, id: UUID, timeout: Double) {
        if links[id] != nil { return fail(fd, "already connected to this board over Bluetooth") }
        guard let p = seen[id]?.p ?? central!.retrievePeripherals(withIdentifiers: [id]).first else {
            return fail(fd, "This Mac hasn't seen that board over Bluetooth. Search for it first.")
        }
        let l = Link(bridge: self, fd: fd, p: p)
        links[id] = l
        p.delegate = l
        central!.connect(p)
        q.asyncAfter(deadline: .now() + timeout) {
            if !l.ready { l.finish("no answer over Bluetooth (out of range, asleep, or connected to another app)") }
        }
    }

    fileprivate func ended(_ l: Link) {
        if links[l.p.identifier] === l { links[l.p.identifier] = nil }
        central?.cancelPeripheralConnection(l.p)
    }

    func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
        links[p.identifier]?.connected()
    }

    func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        links[p.identifier]?.finish("couldn't connect over Bluetooth: \(error?.localizedDescription ?? "unknown error")")
    }

    func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        links[p.identifier]?.finish("Bluetooth link lost\(error.map { " (\($0.localizedDescription))" } ?? "")")
    }
}

/// One board's Bluetooth connection, relayed to one socket. Everything runs on bridge.q.
private final class Link: NSObject, CBPeripheralDelegate {
    unowned let bridge: BLEBridge
    let fd: Int32
    let p: CBPeripheral
    var rx: CBCharacteristic?
    var writeType = CBCharacteristicWriteType.withoutResponse
    var ready = false
    var done = false
    var out = Data()            // socket -> board, not yet written
    var inFlight = false        // a with-response write is waiting for its ack
    var readSrc: DispatchSourceRead?

    init(bridge: BLEBridge, fd: Int32, p: CBPeripheral) {
        self.bridge = bridge
        self.fd = fd
        self.p = p
    }

    func connected() { p.discoverServices([nusService]) }

    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        guard let s = p.services?.first(where: { $0.uuid == nusService }) else {
            return finish("that Bluetooth device isn't a VESC device (no VESC UART service)")
        }
        p.discoverCharacteristics([nusRX, nusTX], for: s)
    }

    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
        let cs = s.characteristics ?? []
        guard let rx = cs.first(where: { $0.uuid == nusRX }), let tx = cs.first(where: { $0.uuid == nusTX }) else {
            return finish("the VESC UART service is incomplete")
        }
        self.rx = rx
        writeType = rx.properties.contains(.writeWithoutResponse) ? .withoutResponse : .withResponse
        p.setNotifyValue(true, for: tx)
    }

    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        guard !ready, !done else { return }
        if let e = error { return finish("couldn't subscribe to the board: \(e.localizedDescription)") }
        ready = true
        bridge.reply(fd, ["ok": true, "name": p.name ?? ""], close: false)
        let flags = fcntl(fd, F_GETFL)
        _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
        let src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: bridge.q)
        src.setEventHandler { [weak self] in self?.readSocket() }
        src.setCancelHandler { [fd] in close(fd) }
        src.resume()
        readSrc = src
    }

    func readSocket() {
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(fd, &buf, buf.count)
            if n > 0 { out.append(contentsOf: buf[0..<n]); continue }
            if n == 0 { return finish(nil) }    // the server closed its end
            if errno == EAGAIN || errno == EWOULDBLOCK { break }
            return finish(nil)
        }
        pump()
    }

    /// Sends queued bytes in chunks the link accepts, respecting flow control.
    func pump() {
        guard let rx = rx, !done else { return }
        let max = p.maximumWriteValueLength(for: writeType)
        while !out.isEmpty {
            if writeType == .withoutResponse {
                guard p.canSendWriteWithoutResponse else { return }   // peripheralIsReady calls back
            } else if inFlight { return }
            let n = min(max, out.count)
            p.writeValue(out.prefix(n), for: rx, type: writeType)
            out.removeFirst(n)
            if writeType == .withResponse { inFlight = true }
        }
    }

    func peripheralIsReady(toSendWriteWithoutResponse p: CBPeripheral) { pump() }

    func peripheral(_ p: CBPeripheral, didWriteValueFor c: CBCharacteristic, error: Error?) {
        inFlight = false
        if let e = error { return finish("Bluetooth write failed: \(e.localizedDescription)") }
        pump()
    }

    func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        guard c.uuid == nusTX, ready, let v = c.value, !v.isEmpty else { return }
        // Blocking write (the socket is non-blocking, so retry on EAGAIN): the server reads constantly.
        if !writeAll(fd, v) { finish(nil) }
    }

    func finish(_ why: String?) {
        guard !done else { return }
        done = true
        if let src = readSrc { src.cancel() }            // its cancel handler closes fd
        else if let why = why { bridge.fail(fd, why) }   // still waiting for the reply line
        else { close(fd) }
        bridge.ended(self)
    }
}

/// Writes all of d to fd, waiting out a full send buffer. False if the socket is gone.
func writeAll(_ fd: Int32, _ d: Data) -> Bool {
    d.withUnsafeBytes { (raw: UnsafeRawBufferPointer) -> Bool in
        var off = 0
        while off < raw.count {
            let n = write(fd, raw.baseAddress! + off, raw.count - off)
            if n > 0 { off += n; continue }
            if n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) { usleep(1000); continue }
            return false
        }
        return true
    }
}
