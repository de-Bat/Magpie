import SwiftUI

/// Buttons for the server's integrations (Radarr, Sonarr, ...) that apply to this item, plus what they know about it
/// (in the library, downloading, next episode). Needs the server; shows nothing offline or when none applies.
struct PluginSection: View {
    let itemID: String
    let category: String?

    @State private var actions: [PluginAction] = []
    @State private var running: Set<String> = []
    @State private var message: String?

    var body: some View {
        Group {
            if !actions.isEmpty {
                ForEach(actions) { action in
                    Section(action.label) {
                        actionRow(action)
                        if let section = action.section { details(section) }
                    }
                }
            }
        }
        .task(id: itemID) { await load() }
    }

    @ViewBuilder
    private func actionRow(_ action: PluginAction) -> some View {
        HStack {
            PluginIcon(path: action.icon)
            if action.state == "added" {
                if let url = action.url.flatMap(URL.init(string:)) {
                    Link(destination: url) { Label("In \(action.label)", systemImage: "checkmark.circle.fill") }
                } else {
                    Label("In \(action.label)", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                }
            } else {
                Button {
                    Task { await run(action) }
                } label: {
                    Label(action.actionLabel ?? "Add to \(action.label)", systemImage: "plus.circle")
                }
                .disabled(action.state == "unavailable" || running.contains(action.id))
            }
            Spacer()
            if running.contains(action.id) { ProgressView() }
        }
        if let m = action.message, action.state != "added" || message != nil {
            Text(m).font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func details(_ section: PluginAction.Section) -> some View {
        if let d = section.download {
            VStack(alignment: .leading, spacing: 4) {
                Label(d.label ?? d.state.capitalized, systemImage: Self.symbol(d.state))
                    .font(.subheadline)
                if let p = d.percent { ProgressView(value: p, total: 100) }
                if let eta = d.eta { Text(eta).font(.caption).foregroundStyle(.secondary) }
            }
        }
        ForEach(section.rows ?? []) { row in
            LabeledContent(row.label, value: Self.text(row))
        }
    }

    private static func symbol(_ state: String) -> String {
        switch state {
        case "downloaded": return "checkmark.circle.fill"
        case "downloading": return "arrow.down.circle"
        case "pending": return "hourglass"
        default: return "exclamationmark.triangle"
        }
    }

    /// "Nov 2, 2026 · in 12 days" for date rows, the plain value otherwise.
    private static func text(_ row: PluginAction.Row) -> String {
        guard let day = row.date else { return row.value ?? "" }
        let parser = DateFormatter()
        parser.dateFormat = "yyyy-MM-dd"
        parser.locale = Locale(identifier: "en_US_POSIX")
        guard let date = parser.date(from: day) else { return day }
        let days = Calendar.current.dateComponents([.day], from: Calendar.current.startOfDay(for: .now), to: date).day ?? 0
        let relative = days == 0 ? "today" : days > 0 ? "in \(days) day\(days == 1 ? "" : "s")" : "\(-days) day\(days == -1 ? "" : "s") ago"
        return date.formatted(date: .abbreviated, time: .omitted) + " · " + relative
    }

    private func load() async {
        guard let api = ServerSettings.client else { actions = []; return }
        actions = (try? await api.itemPlugins(id: itemID)) ?? []
        // Keep an eye on downloads while this screen is open.
        while !Task.isCancelled, actions.contains(where: { $0.section?.download?.state == "downloading" }) {
            try? await Task.sleep(for: .seconds(8))
            if let fresh = try? await api.itemPlugins(id: itemID) { actions = fresh }
        }
    }

    private func run(_ action: PluginAction) async {
        guard let api = ServerSettings.client else { return }
        running.insert(action.id)
        defer { running.remove(action.id) }
        do {
            let result = try await api.runPlugin(itemID: itemID, pluginID: action.id)
            message = result.message
            if let i = actions.firstIndex(where: { $0.id == action.id }) {
                actions[i].state = result.state
                actions[i].url = result.url ?? actions[i].url
                actions[i].message = result.message
            }
            actions = (try? await api.itemPlugins(id: itemID)) ?? actions
        } catch {
            if let i = actions.firstIndex(where: { $0.id == action.id }) { actions[i].message = error.localizedDescription }
        }
    }
}

/// The plugin's own icon (for example Radarr's, fetched by the server from your instance).
private struct PluginIcon: View {
    let path: String?
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFit().frame(width: 22, height: 22).clipShape(RoundedRectangle(cornerRadius: 5))
            }
        }
        .task(id: path) {
            guard let path, let api = ServerSettings.client, let data = try? await api.data(path: path) else { return }
            image = UIImage(data: data)
        }
    }
}
