import SwiftUI

/// Saving something a capture lists as an item of its own.
@MainActor
enum MentionSaver {
    /// Films, shows and books are looked up by name on the server; anything with a link is saved as a link.
    static func byName(_ m: Mention) -> Bool {
        guard let c = m.category else { return false }
        return [.movie, .tv_show, .book].contains(c)
    }

    static func canSave(_ m: Mention) -> Bool { byName(m) || m.url != nil }

    static func isSaved(_ m: Mention, parentID: String, in items: [Item]) -> Bool {
        let title = m.title.lowercased()
        return items.contains { other in
            guard other.id != parentID else { return false }
            if let url = m.url, other.sourceUrl == url || other.canonicalUrl == url { return true }
            return other.title?.lowercased() == title
        }
    }

    /// Returns an error message, or nil when saved (or queued, for links).
    static func save(_ m: Mention, store: LibraryStore, sync: SyncEngine) async -> String? {
        if byName(m), let category = m.category {
            guard let api = ServerSettings.client else { return "Connect your server to add by name." }
            do {
                let item = try await api.addEntry(EntryBody(category: category.rawValue, title: m.title, year: m.year,
                                                            id: "ios-" + UUID().uuidString.lowercased()))
                store.adopt(item)
                sync.requestSync()
                return nil
            } catch {
                return error.localizedDescription
            }
        }
        if let url = m.url.flatMap(URL.init(string:)) {
            store.addLink(url)     // works offline: queued like any other link
            sync.requestSync()
        }
        return nil
    }
}

/// The films, shows, apps and repositories a capture lists: what is in it at a glance, each one's page, and Save.
struct CaptureList: View {
    let item: Item

    @Environment(\.palette) private var palette
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @State private var savingAll = false
    @State private var problem: String?

    private var unsaved: [Mention] {
        item.mentions.filter { MentionSaver.canSave($0) && !MentionSaver.isSaved($0, parentID: item.id, in: store.items) }
    }

    var body: some View {
        ThemedSection {
            breakdown
            ForEach(item.mentions) { CaptureRow(mention: $0, parentID: item.id) }
            if unsaved.count > 1 {
                Button {
                    Task { await saveAll() }
                } label: {
                    HStack {
                        Label("Save all \(unsaved.count) as items", systemImage: "square.and.arrow.down.on.square")
                        Spacer()
                        if savingAll { ProgressView() }
                    }
                }
                .disabled(savingAll)
            }
            if let problem { Text(problem).font(.footnote).foregroundStyle(palette.err) }
        } header: {
            Text("In this capture · \(item.multiCount)")
        } footer: {
            Text("This capture lists several things. Open any of them, or save them as items of their own.")
        }
    }

    /// "Movie ×3  TV show ×2": what kinds of things it holds.
    private var breakdown: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(item.mentionBreakdown, id: \.0) { category, count in
                    HStack(spacing: 6) {
                        TypeBadge(category: category, size: 22)
                        Text("\(category.label) ×\(count)").font(.footnote.weight(.medium))
                    }
                    .padding(.leading, 3).padding(.trailing, 10).padding(.vertical, 3)
                    .background(palette.surface2, in: Capsule())
                }
            }
        }
    }

    private func saveAll() async {
        savingAll = true
        problem = nil
        defer { savingAll = false }
        for m in unsaved {
            if let error = await MentionSaver.save(m, store: store, sync: sync) {
                problem = error
                break
            }
        }
    }
}

private struct CaptureRow: View {
    let mention: Mention
    let parentID: String

    @Environment(\.palette) private var palette
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @State private var working = false
    @State private var problem: String?

    private var saved: Bool { MentionSaver.isSaved(mention, parentID: parentID, in: store.items) }

    private var detail: String {
        [mention.category?.label, mention.year.map(String.init),
         mention.url.flatMap { URL(string: $0)?.host?.replacingOccurrences(of: "www.", with: "") }]
            .compactMap { $0 }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 12) {
                if let category = mention.category { TypeBadge(category: category, size: 34) }
                VStack(alignment: .leading, spacing: 2) {
                    Text(mention.title).font(.subheadline.weight(.semibold)).lineLimit(2)
                    if !detail.isEmpty { Text(detail).font(.caption).foregroundStyle(palette.muted).lineLimit(1) }
                }
                Spacer(minLength: 0)
                if let url = mention.url.flatMap(URL.init(string:)) {
                    Link(destination: url) {
                        Image(systemName: "arrow.up.right.square").font(.title3).frame(width: 36, height: 36)
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Open \(mention.title)")
                }
                if saved {
                    Image(systemName: "checkmark.circle.fill").font(.title3).foregroundStyle(palette.ok)
                        .frame(width: 36, height: 36).accessibilityLabel("Saved")
                } else if MentionSaver.canSave(mention) {
                    Button {
                        Task {
                            working = true
                            problem = await MentionSaver.save(mention, store: store, sync: sync)
                            working = false
                        }
                    } label: {
                        Group { if working { ProgressView() } else { Image(systemName: "plus.circle").font(.title3) } }
                            .frame(width: 36, height: 36)
                    }
                    .buttonStyle(.borderless)
                    .disabled(working)
                    .accessibilityLabel("Save \(mention.title)")
                }
            }
            if let problem { Text(problem).font(.caption).foregroundStyle(palette.err) }
        }
    }
}
