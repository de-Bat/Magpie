import BackgroundTasks
import SwiftUI

@main
struct MagpieApp: App {
    @StateObject private var store: LibraryStore
    @StateObject private var sync: SyncEngine
    @StateObject private var themes = ThemeStore()
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage(Preferences.textSizeKey) private var textSize = TextSizeChoice.standard

    static let backgroundTaskID = (Bundle.main.bundleIdentifier ?? "magpie") + ".sync"

    init() {
        let store = LibraryStore()
        _store = StateObject(wrappedValue: store)
        _sync = StateObject(wrappedValue: SyncEngine(store: store))
    }

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environmentObject(store)
                .environmentObject(sync)
                .themedRoot()                   // reads ThemeStore from the environment, so it goes inside this:
                .environmentObject(themes)
                .dynamicTypeSize(textSize.dynamicType)
                .task(id: scenePhase) {
                    // While the app is open, pick up captures other devices add (and their progress).
                    guard scenePhase == .active else { return }
                    await themes.refresh()
                    while !Task.isCancelled {
                        try? await Task.sleep(for: .seconds(10))
                        if Task.isCancelled { break }
                        await sync.sync()
                    }
                }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active: sync.requestSync()   // also picks up screenshots from the share extension
            case .background: scheduleBackgroundSync()
            default: break
            }
        }
        .backgroundTask(.appRefresh(Self.backgroundTaskID)) {
            await sync.sync()
            await scheduleBackgroundSync()
        }
    }

    /// Ask iOS to wake the app later so queued uploads go out even if it isn't opened.
    @MainActor
    private func scheduleBackgroundSync() {
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}
