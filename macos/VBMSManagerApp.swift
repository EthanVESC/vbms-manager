// VBMS Manager.app launcher: a menu-bar item that keeps the bundled Python
// server (Resources/app/vbms_manager.py) running in the background.
//
// Built by macos/build.sh with swiftc; there is no Xcode project.

import AppKit
import ServiceManagement

let env = ProcessInfo.processInfo.environment
let port = Int(env["VBMS_MANAGER_PORT"] ?? "") ?? 8642          // override for testing only
let baseURL = URL(string: "http://127.0.0.1:\(port)/")!
let restartDelays: [TimeInterval] = [2, 5, 15, 60]
let logDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/VBMS Manager")
let logURL = logDir.appendingPathComponent("vbms_manager.log")
let dataDir = env["VBMS_MANAGER_HOME"].map { URL(fileURLWithPath: $0) }
    ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".vbms_manager")

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    let lanItem = NSMenuItem(title: "Use from Phones on This Network", action: #selector(toggleLAN), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")

    var proc: Process?
    let ble = BLEBridge()
    var bleOK = false
    var stopping = false            // we stopped the server on purpose (quit / restart)
    var quitting = false
    var failures = 0
    var startedAt = Date()
    var nextLanFlag: String?        // "--lan" / "--local-only" for the next start only
    var openWhenReady = false
    var external = false            // another copy of VBMS Manager already owns the port
    var lan = false
    var activity: NSObjectProtocol?
    var timer: Timer?

    var version: String { Bundle.main.object(forInfoDictionaryKey: "VBMSVersion") as? String ?? "dev" }

    // MARK: launch

    func applicationDidFinishLaunching(_ note: Notification) {
        let me = NSRunningApplication.current
        if let id = Bundle.main.bundleIdentifier,
           NSRunningApplication.runningApplications(withBundleIdentifier: id).contains(where: { $0 != me }) {
            NSWorkspace.shared.open(baseURL)     // already running: just show it
            NSApp.terminate(nil)
            return
        }
        if offerMoveToApplications() { return }

        buildMenu()
        // Keep App Nap from throttling the scheduler. This doesn't stop the Mac sleeping.
        activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                                                         reason: "Runs charge schedules and recording")
        let defaults = UserDefaults.standard
        if !defaults.bool(forKey: "didFirstLaunch") {
            defaults.set(true, forKey: "didFirstLaunch")
            try? SMAppService.mainApp.register()
        }
        openWhenReady = !launchedAtLogin()
        bleOK = ble.start()
        startServer()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openUI()
        return false
    }

    func launchedAtLogin() -> Bool {
        guard let ev = NSAppleEventManager.shared().currentAppleEvent else { return false }
        return ev.eventID == kAEOpenApplication
            && ev.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    // MARK: menu

    func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let img = NSImage(systemSymbolName: "bolt.batteryblock", accessibilityDescription: "VBMS Manager")
        img?.isTemplate = true
        statusItem.button?.image = img
        statusItem.button?.toolTip = "VBMS Manager"

        let m = NSMenu()
        m.delegate = self
        m.autoenablesItems = false
        statusLine.isEnabled = false
        m.addItem(statusLine)
        m.addItem(.separator())
        m.addItem(item("Open VBMS Manager", #selector(openUI), "o"))
        m.addItem(.separator())
        lanItem.target = self
        m.addItem(lanItem)
        loginItem.target = self
        m.addItem(loginItem)
        m.addItem(.separator())
        m.addItem(item("Show Data Folder", #selector(showData)))
        m.addItem(item("Show Log", #selector(showLog)))
        m.addItem(.separator())
        let v = NSMenuItem(title: "Version \(version)", action: nil, keyEquivalent: "")
        v.isEnabled = false
        m.addItem(v)
        m.addItem(item("Quit VBMS Manager", #selector(confirmQuit), "q"))
        statusItem.menu = m
        refreshLogin()
    }

    func item(_ title: String, _ sel: Selector, _ key: String = "") -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        i.target = self
        return i
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshLogin()
        pollState()
    }

    func setStatus(_ s: String) { statusLine.title = s }

    // MARK: server process

    func startServer() {
        timer?.invalidate()
        setStatus("Starting…")
        // Another copy (e.g. one started from a terminal) already serving? Use it.
        probe { [self] ours in
            if ours {
                external = true
                setStatus("Running (another copy of VBMS Manager)")
                lanItem.isEnabled = false
                if openWhenReady { openWhenReady = false; NSWorkspace.shared.open(baseURL) }
                // Take over once that copy goes away.
                timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: false) { [weak self] _ in self?.startServer() }
                return
            }
            external = false
            lanItem.isEnabled = true
            spawn()
        }
    }

    func spawn() {
        let res = Bundle.main.resourceURL!
        #if arch(arm64)
        let py = res.appendingPathComponent("python-arm64/bin/python3")
        #else
        let py = res.appendingPathComponent("python-x86_64/bin/python3")
        #endif
        // -I: ignore PYTHON* variables and user site-packages; -B: never write
        // __pycache__ into the signed bundle; -u: unbuffered log.
        var args = ["-I", "-B", "-u", res.appendingPathComponent("app/vbms_manager.py").path,
                    "--no-browser", "--port", String(port)]
        if let f = nextLanFlag { args.append(f); nextLanFlag = nil }

        let p = Process()
        p.executableURL = py
        p.arguments = args
        var e = env
        e["VBMS_MANAGER_APP"] = "1"     // the server exits if this launcher dies
        if bleOK { e["VBMS_BLE_PORT"] = String(ble.port); e["VBMS_BLE_TOKEN"] = ble.token }
        p.environment = e
        if let h = openLog() { p.standardOutput = h; p.standardError = h }
        p.terminationHandler = { [weak self] t in
            DispatchQueue.main.async { self?.exited(t.terminationStatus) }
        }
        do {
            try p.run()
        } catch {
            setStatus("Couldn't start: \(error.localizedDescription)")
            return
        }
        proc = p
        stopping = false
        startedAt = Date()
        waitUntilReady(tries: 60)
    }

    func waitUntilReady(tries: Int) {
        guard let p = proc, p.isRunning else { return }
        probe { [self] ours in
            guard proc === p else { return }
            if ours {
                setStatus("Running")
                pollState()
                if openWhenReady { openWhenReady = false; NSWorkspace.shared.open(baseURL) }
            } else if tries > 0 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitUntilReady(tries: tries - 1) }
            } else {
                setStatus("Started, but not answering yet")
            }
        }
    }

    func exited(_ code: Int32) {
        proc = nil
        if quitting { NSApp.reply(toApplicationShouldTerminate: true); return }
        if stopping { stopping = false; startServer(); return }   // a restart we asked for
        if Date().timeIntervalSince(startedAt) > 120 { failures = 0 }
        let delay = restartDelays[min(failures, restartDelays.count - 1)]
        failures += 1
        let why = logTail().contains("Could not start on port") ? "Port \(port) is in use by another program"
                                                                : "Stopped unexpectedly"
        setStatus("\(why) · retrying in \(Int(delay)) s")
        timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in self?.startServer() }
    }

    /// Stop the server (SIGINT, so it disconnects boards cleanly), SIGKILL after 5 s.
    func stopServer() {
        guard let p = proc, p.isRunning else { return }
        stopping = true
        p.interrupt()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
    }

    func restartServer(flag: String?) {
        nextLanFlag = flag
        if proc?.isRunning == true { stopServer() } else { startServer() }
    }

    /// Calls back true if a VBMS Manager is answering on our port.
    func probe(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/state"))
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let server = (resp as? HTTPURLResponse)?.value(forHTTPHeaderField: "Server") ?? ""
            DispatchQueue.main.async { done(server.hasPrefix("VBMSManager")) }
        }.resume()
    }

    /// Board count and the LAN setting for the menu.
    func pollState() {
        guard proc?.isRunning == true || external else { return }
        var req = URLRequest(url: baseURL.appendingPathComponent("api/state"))
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            let boards = j["boards"] as? [[String: Any]] ?? []
            let online = boards.filter { ($0["state"] as? String) == "online" }.count
            let lan = (j["access"] as? [String: Any])?["lan"] as? Bool ?? false
            DispatchQueue.main.async { [self] in
                self.lan = lan
                lanItem.state = lan ? .on : .off
                let prefix = external ? "Running (another copy)" : "Running"
                setStatus(boards.isEmpty ? "\(prefix) · no boards yet"
                                         : "\(prefix) · \(online) of \(boards.count) board\(boards.count == 1 ? "" : "s") online")
            }
        }.resume()
    }

    // MARK: log

    func openLog() -> FileHandle? {
        let fm = FileManager.default
        try? fm.createDirectory(at: logDir, withIntermediateDirectories: true)
        if let size = (try? fm.attributesOfItem(atPath: logURL.path))?[.size] as? Int, size > 5_000_000 {
            try? fm.removeItem(at: logURL)
        }
        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
        guard let h = try? FileHandle(forWritingTo: logURL) else { return nil }
        h.seekToEndOfFile()
        let stamp = ISO8601DateFormatter().string(from: Date())
        h.write("\n--- \(stamp) VBMS Manager \(version) starting\n".data(using: .utf8)!)
        return h
    }

    func logTail() -> String {
        guard let h = try? FileHandle(forReadingFrom: logURL) else { return "" }
        let end = h.seekToEndOfFile()
        h.seek(toFileOffset: end > 2000 ? end - 2000 : 0)
        return String(decoding: h.readDataToEndOfFile(), as: UTF8.self)
    }

    // MARK: actions

    @objc func openUI() {
        if proc?.isRunning == true || external {
            NSWorkspace.shared.open(baseURL)
        } else {
            openWhenReady = true
            timer?.invalidate()
            startServer()
        }
    }

    @objc func toggleLAN() {
        if !lan {
            let a = NSAlert()
            a.messageText = "Use VBMS Manager from your phone?"
            a.informativeText = "Other devices on this network will be able to open VBMS Manager. They can only watch until you pair them with the code shown under Devices in VBMS Manager on this Mac.\n\nmacOS may ask whether to allow incoming connections. Choose Allow."
            a.addButton(withTitle: "Allow Phones")
            a.addButton(withTitle: "Cancel")
            NSApp.activate(ignoringOtherApps: true)
            guard a.runModal() == .alertFirstButtonReturn else { return }
        }
        lan.toggle()
        lanItem.state = lan ? .on : .off
        restartServer(flag: lan ? "--lan" : "--local-only")
    }

    @objc func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            switch svc.status {
            case .enabled: try svc.unregister()
            case .requiresApproval: SMAppService.openSystemSettingsLoginItems()
            default: try svc.register()
            }
        } catch {
            let a = NSAlert(error: error)
            NSApp.activate(ignoringOtherApps: true)
            a.runModal()
        }
        refreshLogin()
    }

    func refreshLogin() {
        switch SMAppService.mainApp.status {
        case .enabled:
            loginItem.state = .on; loginItem.title = "Open at Login"
        case .requiresApproval:
            loginItem.state = .mixed; loginItem.title = "Open at Login (allow in System Settings…)"
        default:
            loginItem.state = .off; loginItem.title = "Open at Login"
        }
    }

    @objc func showData() {
        if FileManager.default.fileExists(atPath: dataDir.path) {
            NSWorkspace.shared.activateFileViewerSelecting([dataDir])
        } else {
            NSWorkspace.shared.open(dataDir.deletingLastPathComponent())
        }
    }

    @objc func showLog() {
        if FileManager.default.fileExists(atPath: logURL.path) {
            NSWorkspace.shared.activateFileViewerSelecting([logURL])
        }
    }

    @objc func confirmQuit() {
        let a = NSAlert()
        a.messageText = "Quit VBMS Manager?"
        a.informativeText = "Charge schedules, timers, recording and alerts stop until you open it again. Settings already written to your boards stay as they are."
        a.addButton(withTitle: "Quit")
        a.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        if a.runModal() == .alertFirstButtonReturn { NSApp.terminate(nil) }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        timer?.invalidate()
        guard let p = proc, p.isRunning else { return .terminateNow }
        quitting = true
        stopServer()
        return .terminateLater
    }

    // MARK: move to /Applications

    /// Offers to copy the app into /Applications (login items need a stable path).
    /// Returns true if it's relaunching from there.
    func offerMoveToApplications() -> Bool {
        let fm = FileManager.default
        let src = Bundle.main.bundleURL
        let path = src.path
        let home = fm.homeDirectoryForCurrentUser.path
        if path.hasPrefix("/Applications/") || path.hasPrefix(home + "/Applications/") { return false }
        if UserDefaults.standard.bool(forKey: "dontOfferMove") { return false }

        let a = NSAlert()
        a.messageText = "Move VBMS Manager to your Applications folder?"
        a.informativeText = "It needs to live in Applications to start when you log in and keep your schedules running."
        a.addButton(withTitle: "Move to Applications")
        a.addButton(withTitle: "Not Now")
        a.showsSuppressionButton = true
        NSApp.activate(ignoringOtherApps: true)
        let r = a.runModal()
        if a.suppressionButton?.state == .on { UserDefaults.standard.set(true, forKey: "dontOfferMove") }
        guard r == .alertFirstButtonReturn else { return false }

        let dest = URL(fileURLWithPath: "/Applications").appendingPathComponent(src.lastPathComponent)
        do {
            if fm.fileExists(atPath: dest.path) { try fm.trashItem(at: dest, resultingItemURL: nil) }
            try fm.copyItem(at: src, to: dest)
        } catch {
            let e = NSAlert()
            e.messageText = "Couldn't move VBMS Manager"
            e.informativeText = "\(error.localizedDescription)\n\nDrag it into Applications in Finder instead."
            e.runModal()
            return false
        }
        // Remove the original unless it's on the disk image (read-only) or translocated.
        if !path.hasPrefix("/Volumes/") && !path.contains("/AppTranslocation/") {
            try? fm.trashItem(at: src, resultingItemURL: nil)
        }
        let relaunch = Process()
        relaunch.executableURL = URL(fileURLWithPath: "/bin/sh")
        relaunch.arguments = ["-c", "sleep 1; /usr/bin/open \"$0\"", dest.path]
        try? relaunch.run()
        NSApp.terminate(nil)
        return true
    }
}

@main
enum Main {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
