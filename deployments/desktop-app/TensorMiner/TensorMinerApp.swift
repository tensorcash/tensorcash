import SwiftUI
import AppKit

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

@main
struct TensorMinerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var minerService = MinerService()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(minerService)
                .task {
                    // defaults write io.tensorcash.TensorMiner autoStartMining -bool YES
                    // to start mining at launch without clicking the UI button.
                    if UserDefaults.standard.bool(forKey: "autoStartMining") {
                        try? await Task.sleep(nanoseconds: 500_000_000)
                        if minerService.canStart && !minerService.isRunning {
                            minerService.start()
                        }
                    }
                }
        }
        .windowResizability(.contentMinSize)

        Settings {
            SettingsView()
                .environmentObject(minerService)
        }
    }
}
