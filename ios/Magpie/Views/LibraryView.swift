import PhotosUI
import SwiftUI

struct LibraryView: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    @Environment(\.horizontalSizeClass) private var sizeClass

    @State private var query = ""
    @State private var category: Category?
    @State private var tags: Set<String> = []
    @State private var show: ShowFilter = .all
    @State private var picked: [PhotosPickerItem] = []
    @State private var showSettings = false
    @State private var showTags = false
    @State private var addByName: CatalogKind?
    @State private var message: String?
    @AppStorage(Preferences.columnsKey) private var columns = 0
    @AppStorage(Preferences.layoutKey) private var layout = LibraryLayout.grid

    private var results: [Item] { store.filtered(query: query, category: category, tags: tags, show: show) }
    private var maxColumns: Int { sizeClass == .regular ? 8 : 4 }

    private var gridColumns: [GridItem] {
        let n = min(columns, maxColumns)
        return n == 0
            ? [GridItem(.adaptive(minimum: 150), spacing: 12)]
            : Array(repeating: GridItem(.flexible(), spacing: 12), count: n)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    SyncBanner(onOpenSettings: { showSettings = true })
                    filters
                    if results.isEmpty {
                        emptyState
                    } else if layout == .list {
                        LazyVStack(spacing: 8) {
                            ForEach(results) { item in
                                NavigationLink(value: item.id) { ItemRow(item: item) }
                                    .buttonStyle(.plain)
                            }
                        }
                    } else {
                        LazyVGrid(columns: gridColumns, spacing: 12) {
                            ForEach(results) { item in
                                NavigationLink(value: item.id) { ItemCard(item: item, dense: columns >= 3) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                }
                .padding(.horizontal)
            }
            .themedScreen()
            .navigationTitle("Magpie")
            .navigationDestination(for: String.self) { ItemDetailView(itemID: $0) }
            .searchable(text: $query, prompt: "Search everything you saved")
            .refreshable { await sync.sync() }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showSettings = true } label: { SyncStatusIcon() }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    viewMenu
                    Menu {
                        ForEach(CatalogKind.allCases) { kind in
                            Button { addByName = kind } label: { Label(kind.label, systemImage: kind.symbol) }
                        }
                    } label: { Image(systemName: "text.magnifyingglass") }
                        .accessibilityLabel("Add by name")
                    Button { pasteFromClipboard() } label: { Image(systemName: "doc.on.clipboard") }
                        .accessibilityLabel("Paste a screenshot or link")
                    // .current: the photo's own bytes, not a JPEG copy, so cleanup can later recognise it by its hash
                    PhotosPicker(selection: $picked, maxSelectionCount: 20, matching: .images, preferredItemEncoding: .current) {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Add screenshots")
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(isPresented: $showTags) { TagFilterSheet(selected: $tags) }
            .sheet(item: $addByName) { kind in
                AddByNameView(kind: kind) { id in
                    flash("Added")
                    sync.requestSync()
                    _ = id
                }
            }
            .onChange(of: picked) { _, newValue in addPicked(newValue) }
            .overlay(alignment: .bottom) {
                if let message {
                    Text(message)
                        .padding(.horizontal, 16).padding(.vertical, 10)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 24)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
        }
    }

    /// Layout and number of columns, remembered on this device.
    private var viewMenu: some View {
        Menu {
            Picker("Layout", selection: $layout) {
                ForEach(LibraryLayout.allCases) { Label($0.label, systemImage: $0.symbol).tag($0) }
            }
            if layout == .grid {
                Picker("Columns", selection: $columns) {
                    Text("Auto").tag(0)
                    ForEach(1...maxColumns, id: \.self) { Text("\($0)").tag($0) }
                }
            }
        } label: { Image(systemName: layout == .list ? "list.bullet" : "square.grid.2x2") }
            .accessibilityLabel("Layout and columns")
    }

    private var filters: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    Menu {
                        Picker("Show", selection: $show) {
                            ForEach(ShowFilter.allCases) { f in
                                Label("\(f.label) \(store.count(for: f))", systemImage: f.symbol).tag(f)
                            }
                        }
                    } label: {
                        Chip(label: show == .all ? "Show: All" : show.label, systemImage: show.symbol, active: show != .all) {}
                            .allowsHitTesting(false)
                    }
                    Chip(label: tags.isEmpty ? "# Tags" : "# Tags \(tags.count)", active: !tags.isEmpty) { showTags = true }
                    ForEach(tags.sorted(), id: \.self) { t in
                        Chip(label: "#\(t)  ✕", active: true) { tags.remove(t) }
                    }
                }
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(store.categoryCounts, id: \.0) { c, count in
                        Chip(label: "\(c.label) \(count)", systemImage: c.symbol, active: category == c) {
                            category = category == c ? nil : c
                        }
                    }
                }
            }
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(store.items.isEmpty ? "Nothing saved yet" : "No matches", systemImage: "pin")
        } description: {
            Text(store.items.isEmpty
                 ? "Tap + to pick a screenshot, copy a link and tap the clipboard, or add a film, show or book by name."
                 : "Clear the search or change the filters.")
        }
        .padding(.top, 60)
    }

    private func addPicked(_ selection: [PhotosPickerItem]) {
        guard !selection.isEmpty else { return }
        Task {
            var added = 0
            for pick in selection {
                if let data = try? await pick.loadTransferable(type: Data.self), store.addScreenshot(data) != nil {
                    added += 1
                }
            }
            picked = []
            flash(added == 1 ? "Screenshot saved" : "\(added) screenshots saved")
            sync.requestSync()
        }
    }

    /// Paste a screenshot, or a link copied from Safari, X, Instagram ("Copy link"), etc.
    private func pasteFromClipboard() {
        let board = UIPasteboard.general
        if board.hasImages, let data = board.image?.pngData() {
            store.addScreenshot(data)
            flash("Screenshot saved")
        } else if let url = board.url ?? board.string.flatMap(Self.link(from:)) {
            if store.items.contains(where: { $0.sourceUrl == url.absoluteString }) {
                flash("Already saved")
            } else {
                store.addLink(url)
                flash("Link saved")
            }
        } else {
            flash("Copy a screenshot or a link first")
            return
        }
        sync.requestSync()
    }

    private static func link(from text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.contains(" "), trimmed.contains(".") else { return nil }
        let url = URL(string: trimmed.contains("://") ? trimmed : "https://" + trimmed)
        return url?.scheme?.hasPrefix("http") == true && url?.host != nil ? url : nil
    }

    private func flash(_ text: String) {
        withAnimation { message = text }
        Task {
            try? await Task.sleep(for: .seconds(2.5))
            withAnimation { if message == text { message = nil } }
        }
    }
}

struct ItemCard: View {
    @Environment(\.palette) private var palette
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    let item: Item
    var dense = false

    private var category: Category? { Category(rawValue: item.category ?? "") }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // One shape for every card, so the grid stays even whatever the type (posters crop a little, screenshots too).
            ItemImage(item: item)
                .aspectRatio(2 / 3, contentMode: .fit)
                .overlay(alignment: .topLeading) { badge.padding(6) }
                .overlay(alignment: .topTrailing) { flag.padding(6) }
                .overlay(alignment: .bottomTrailing) { marks.padding(6) }
                .overlay(alignment: .bottomLeading) { verifyButton.padding(6) }
                .overlay {
                    if item.status == "processing" || item.status == "queued" {
                        Color.black.opacity(0.35)
                        VStack(spacing: 6) {
                            if item.status == "processing" { ProgressView().tint(.white) } else { Image(systemName: "icloud.and.arrow.up") }
                            Text(item.status == "processing" ? "Analyzing…" : "Waiting to upload").font(.caption.bold())
                        }
                        .foregroundStyle(.white)
                    }
                }
            // Lines are reserved even when empty, so every card has the same height.
            VStack(alignment: .leading, spacing: 4) {
                Text(item.displayTitle)
                    .font(dense ? .caption.weight(.semibold) : .subheadline.weight(.semibold))
                    .lineLimit(2, reservesSpace: true)
                if !dense {
                    Text(item.subtitle ?? " ").font(.caption).foregroundStyle(palette.muted).lineLimit(1, reservesSpace: true)
                    Text(cardFacts.isEmpty ? " " : cardFacts.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(palette.muted).lineLimit(1, reservesSpace: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(dense ? 6 : 10)
        }
        .background(palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(palette.border, lineWidth: 0.5))
    }

    @ViewBuilder private var badge: some View {
        if item.status == "error" {
            Label("Failed", systemImage: "exclamationmark.triangle.fill").badgeStyle(.red)
        } else if let category {
            TypeBadge(category: category)
        }
    }

    /// Nothing confirmed this identification: one tap says it is right.
    @ViewBuilder private var verifyButton: some View {
        if !dense, item.status == "ready", !item.verified {
            Button {
                store.update(item.id, ItemPatch(confirmed: true))
                sync.requestSync()
            } label: {
                Label("Verify", systemImage: "checkmark.seal.fill")
                    .font(.caption2.bold())
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(.ultraThinMaterial, in: Capsule())
            }
            .buttonStyle(.borderless)
        }
    }

    /// "!" when it failed or Magpie isn't sure, "○" when nothing confirmed it.
    @ViewBuilder private var flag: some View {
        if item.needsReview, let c = item.confidence {
            Label("\(c)%", systemImage: "questionmark.circle.fill").badgeStyle(.orange)
                .accessibilityLabel("Not sure: \(c) percent confident")
        } else if item.status == "ready", !item.verified {
            Image(systemName: "circle.dotted").badgeStyle(.gray)
                .accessibilityLabel("Not confirmed by any source")
        }
    }

    @ViewBuilder private var marks: some View {
        HStack(spacing: 4) {
            if item.reviewed { Image(systemName: "checkmark.circle.fill").badgeStyle(.green) }
            if item.favorite { Image(systemName: "star.fill").badgeStyle(.yellow) }
            if item.watchlist { Image(systemName: "eye.fill").badgeStyle(.blue) }
        }
    }

    private var cardFacts: [String] {
        var out: [String] = []
        if let r = item.meta("imdb_rating") ?? item.meta("tmdb_rating") { out.append("★ " + r.replacingOccurrences(of: "/10", with: "")) }
        if let rt = item.meta("rotten_tomatoes") { out.append("🍅 " + rt) }
        if let stars = item.meta("stars") { out.append("★ " + stars) }
        if let lang = item.meta("programming_language") { out.append(lang) }
        if let time = item.meta("total_time") { out.append(time) }
        if let year = item.meta("year"), category?.isPortrait == true { out.append(year) }
        return Array(out.prefix(3))
    }
}

/// The type as an icon on its colour. The name is still read out by VoiceOver.
struct TypeBadge: View {
    let category: Category
    var size: CGFloat = 28

    var body: some View {
        Image(systemName: category.symbol)
            .font(.system(size: size * 0.46, weight: .bold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(category.color, in: Circle())
            .overlay(Circle().stroke(.white.opacity(0.35), lineWidth: 1))
            .accessibilityElement()
            .accessibilityLabel(category.label)
    }
}

extension Category {
    /// Each type's colour, for its badge on a card.
    var color: Color {
        switch self {
        case .movie: return Color(red: 0.86, green: 0.25, blue: 0.27)
        case .tv_show: return Color(red: 0.55, green: 0.33, blue: 0.85)
        case .github_repo: return Color(red: 0.20, green: 0.23, blue: 0.29)
        case .recipe: return Color(red: 0.92, green: 0.53, blue: 0.16)
        case .book: return Color(red: 0.62, green: 0.40, blue: 0.24)
        case .music: return Color(red: 0.90, green: 0.30, blue: 0.58)
        case .podcast: return Color(red: 0.42, green: 0.35, blue: 0.80)
        case .video: return Color(red: 0.88, green: 0.18, blue: 0.18)
        case .article: return Color(red: 0.24, green: 0.45, blue: 0.75)
        case .product: return Color(red: 0.88, green: 0.66, blue: 0.10)
        case .place: return Color(red: 0.20, green: 0.62, blue: 0.40)
        case .event: return Color(red: 0.30, green: 0.36, blue: 0.82)
        case .app: return Color(red: 0.10, green: 0.62, blue: 0.75)
        case .course: return Color(red: 0.08, green: 0.58, blue: 0.55)
        case .other: return Color(red: 0.45, green: 0.47, blue: 0.52)
        }
    }
}

/// One line per item, for the list layout.
struct ItemRow: View {
    @Environment(\.palette) private var palette
    let item: Item

    private var category: Category? { Category(rawValue: item.category ?? "") }

    var body: some View {
        HStack(spacing: 12) {
            ItemImage(item: item)
                .frame(width: 56, height: 56)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay(alignment: .topLeading) {
                    if let category { TypeBadge(category: category, size: 22).padding(3) }
                }
            VStack(alignment: .leading, spacing: 2) {
                Text(item.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(2)
                Text([category?.label, item.meta("year"), item.subtitle].compactMap { $0 }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(palette.muted).lineLimit(1)
            }
            Spacer(minLength: 0)
            if item.needsChecking { Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
            if item.reviewed { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) }
            if item.favorite { Image(systemName: "star.fill").foregroundStyle(.yellow) }
            if item.watchlist { Image(systemName: "eye.fill").foregroundStyle(.blue) }
        }
        .padding(8)
        .background(palette.surface, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(palette.border, lineWidth: 0.5))
    }
}

/// Pick tags to filter by: several can be on at once and all must match.
struct TagFilterSheet: View {
    @EnvironmentObject private var store: LibraryStore
    @Environment(\.dismiss) private var dismiss
    @Binding var selected: Set<String>
    @State private var search = ""

    private var shown: [(String, Int)] {
        let all = Array(store.tagCounts.prefix(80))
        guard !search.isEmpty else { return all }
        let f = search.lowercased().replacingOccurrences(of: "#", with: "")
        return all.filter { $0.0.contains(f) }
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach(shown, id: \.0) { tag, count in
                    Button {
                        if selected.contains(tag) { selected.remove(tag) } else { selected.insert(tag) }
                    } label: {
                        HStack {
                            Text("#\(tag)")
                            Spacer()
                            Text("\(count)").foregroundStyle(.secondary)
                            if selected.contains(tag) { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) }
                        }
                    }
                    .foregroundStyle(.primary)
                }
            }
            .searchable(text: $search, prompt: "Filter tags")
            .overlay { if store.tagCounts.isEmpty { ContentUnavailableView("No tags yet", systemImage: "number") } }
            .themedScreen()
            .navigationTitle("Tags")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Clear") { selected = [] }.disabled(selected.isEmpty) }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

struct Chip: View {
    @Environment(\.palette) private var palette
    let label: String
    var systemImage: String?
    var active = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if let systemImage { Image(systemName: systemImage) }
                Text(label)
            }
            .font(.footnote)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(active ? Color.accentColor.opacity(0.18) : palette.surface2, in: Capsule())
            .overlay(Capsule().stroke(active ? Color.accentColor : .clear))
        }
        .buttonStyle(.plain)
    }
}

extension View {
    func badgeStyle(_ color: Color) -> some View {
        font(.caption2.bold())
            .labelStyle(.titleAndIcon)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(color, in: Capsule())
            .foregroundStyle(.white)
    }
}

struct SyncStatusIcon: View {
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine

    var body: some View {
        switch sync.status {
        case .syncing: ProgressView()
        case .offline: Image(systemName: "icloud.slash")
        case .failed: Image(systemName: "exclamationmark.icloud")
        case .notConfigured: Image(systemName: "gearshape")
        case .idle:
            Image(systemName: store.pending.isEmpty ? "checkmark.icloud" : "arrow.triangle.2.circlepath.icloud")
        }
    }
}

/// Explains why things aren't syncing, only when it matters.
struct SyncBanner: View {
    @Environment(\.palette) private var palette
    @EnvironmentObject private var store: LibraryStore
    @EnvironmentObject private var sync: SyncEngine
    let onOpenSettings: () -> Void

    var body: some View {
        switch sync.status {
        case .notConfigured:
            banner("Connect your Magpie server to identify screenshots. Everything you add is kept on this phone until then.",
                   icon: "server.rack", action: "Set up")
        case .offline where !store.pending.isEmpty:
            banner("Offline — \(store.pending.count) change\(store.pending.count == 1 ? "" : "s") will sync when your server is reachable.",
                   icon: "icloud.slash", action: nil)
        case .failed(let message):
            banner("Sync failed: \(message)", icon: "exclamationmark.icloud", action: "Settings")
        default:
            EmptyView()
        }
    }

    private func banner(_ text: String, icon: String, action: String?) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
            Text(text).font(.footnote).frame(maxWidth: .infinity, alignment: .leading)
            if let action { Button(action, action: onOpenSettings).font(.footnote.bold()) }
        }
        .padding(12)
        .background(palette.surface2, in: RoundedRectangle(cornerRadius: 12))
    }
}
