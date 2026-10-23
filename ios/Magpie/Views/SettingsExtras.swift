import SwiftUI

/// Refresh or re-identify the whole library on the server, with progress.
struct LibraryJobsView: View {
    private enum Scope: String, CaseIterable, Identifiable {
        case all, check, failed
        var id: String { rawValue }
        var label: String {
            switch self {
            case .all: return "All items"
            case .check: return "Only items to check"
            case .failed: return "Only failed"
            }
        }
    }

    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @State private var scope: Scope = .all
    @State private var skipConfirmed = true
    @State private var state: BulkState?
    @State private var error: String?
    @State private var confirm: Bool?   // true = re-analyze, false = refresh

    private var count: Int {
        switch scope {
        case .all: return store.items.count
        case .check: return store.items.filter(\.needsChecking).count
        case .failed: return store.items.filter { $0.status == "error" }.count
        }
    }

    var body: some View {
        Form {
            ThemedSection {
                Picker("Scope", selection: $scope) {
                    ForEach(Scope.allCases) { Text($0.label).tag($0) }
                }
                Toggle("Leave out items I corrected or confirmed", isOn: $skipConfirmed)
            } footer: {
                Text("About \(count) item\(count == 1 ? "" : "s"). The “leave out” switch only applies to re-analyzing.")
            }

            ThemedSection {
                if let state, state.running {
                    ProgressView(value: Double(state.done), total: Double(max(state.total, 1)))
                    Text("\(state.kind == "reanalyze" ? "Re-analyzing" : "Refreshing") \(state.done) of \(state.total)"
                         + (state.failed > 0 ? " · \(state.failed) failed" : ""))
                        .font(.footnote)
                    Button("Stop", role: .destructive) { Task { try? await ServerSettings.client?.cancelBulk(); await poll() } }
                } else {
                    Button("Refresh all metadata") { confirm = false }
                    Button("Re-analyze all", role: .destructive) { confirm = true }
                    if let state, state.total > 0 {
                        Text(state.cancelled ? "Stopped after \(state.done) of \(state.total)."
                             : "Done: \(state.done) of \(state.total)" + (state.failed > 0 ? " · \(state.failed) failed" : ""))
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if let error { Text(error).font(.footnote).foregroundStyle(.red) }
            } footer: {
                Text("Refreshing looks up posters, scores and links again, with no AI model and no cost. Re-analyzing identifies items again with your AI provider, which costs money.")
            }
        }
        .themedScreen()
        .navigationTitle("Library jobs")
        .navigationBarTitleDisplayMode(.inline)
        .task { await watch() }
        .confirmationDialog(confirm == true ? "Re-analyze \(count) items?" : "Refresh metadata for \(count) items?",
                            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
                            titleVisibility: .visible) {
            Button(confirm == true ? "Re-analyze (costs money)" : "Refresh", role: confirm == true ? .destructive : nil) {
                if let reanalyze = confirm { Task { await start(refresh: !reanalyze) } }
            }
        }
    }

    private func start(refresh: Bool) async {
        guard let api = ServerSettings.client else { error = "Connect a server first."; return }
        error = nil
        do {
            state = try await api.startBulk(refresh: refresh, scope: scope.rawValue, skipConfirmed: skipConfirmed)
            sync.requestSync()
        } catch APIError.http(409, _) {
            error = "A library job is already running."
        } catch APIError.http(403, _) {
            error = "The server wants its setup code or access token for this. Set the API token in Settings."
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func poll() async {
        if let api = ServerSettings.client { state = try? await api.bulkStatus() }
    }

    /// Check on the job every couple of seconds while this screen is open.
    private func watch() async {
        while !Task.isCancelled {
            await poll()
            if state?.running == true { sync.requestSync() }
            try? await Task.sleep(for: .seconds(state?.running == true ? 1.5 : 5))
        }
    }
}

/// System / Light / Dark, then every theme. Choosing a built-in theme also switches the app icon to its tinted variant.
struct ThemePickerView: View {
    @EnvironmentObject private var themes: ThemeStore

    var body: some View {
        List {
            ThemedSection {
                row("System", detail: "Follows the iPhone's light or dark setting", choice: "system", colors: nil)
                row("Light", detail: nil, choice: "light", colors: nil)
                row("Dark", detail: nil, choice: "dark", colors: nil)
            }
            ThemedSection("Built in") {
                ForEach(themes.themes.filter { $0.builtin == true }) { t in
                    row(t.name, detail: t.isDark ? "Dark" : "Light", choice: "custom:" + t.id, colors: t)
                }
            }
            if themes.themes.contains(where: { $0.builtin != true }) {
                ThemedSection("Uploaded to your server") {
                    ForEach(themes.themes.filter { $0.builtin != true }) { t in
                        row(t.name, detail: t.isDark ? "Dark" : "Light", choice: "custom:" + t.id, colors: t)
                    }
                }
            }
        }
        .themedScreen()
        .navigationTitle("Theme")
        .navigationBarTitleDisplayMode(.inline)
        .task { await themes.refresh() }
    }

    private func row(_ title: String, detail: String?, choice: String, colors: AppTheme?) -> some View {
        Button {
            themes.select(choice)
        } label: {
            HStack(spacing: 12) {
                if let colors {
                    HStack(spacing: -6) {
                        ForEach(["bg", "surface", "text", "accent"], id: \.self) { key in
                            Circle().fill(colors.color(key) ?? .gray).frame(width: 22, height: 22)
                                .overlay(Circle().stroke(Color.gray.opacity(0.4), lineWidth: 0.5))
                        }
                    }
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                    if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                if themes.choice == choice { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) }
            }
        }
        .foregroundStyle(.primary)
    }
}

/// The server's release notes, newest first.
struct WhatsNewView: View {
    @State private var releases: [ReleaseNote] = []
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        List {
            ForEach(releases) { release in
                ThemedSection(release.version == "next" ? "Coming next" : "\(release.version)" + (release.date.map { " · \($0)" } ?? "")) {
                    ForEach(release.features) { feature in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(feature.title).font(.subheadline.weight(.semibold))
                            if let text = feature.text { Text(text).font(.footnote).foregroundStyle(.secondary) }
                        }
                    }
                }
            }
        }
        .overlay {
            if !loaded { ProgressView() }
            else if releases.isEmpty { ContentUnavailableView("No release notes", systemImage: "sparkles", description: Text(error ?? "Connect a server to see what's new.")) }
        }
        .themedScreen()
        .navigationTitle("What's new")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            defer { loaded = true }
            guard let api = ServerSettings.client else { return }
            do { releases = try await api.releases() } catch { self.error = error.localizedDescription }
        }
    }
}
