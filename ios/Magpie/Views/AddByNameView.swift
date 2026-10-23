import SwiftUI

enum CatalogKind: String, CaseIterable, Identifiable {
    case movie, tv_show, book
    var id: String { rawValue }

    var label: String {
        switch self {
        case .movie: return "Movie"
        case .tv_show: return "TV show"
        case .book: return "Book"
        }
    }

    var symbol: String { Category(rawValue: rawValue)?.symbol ?? "pin" }
}

/// Add a movie, TV show or book by name: search, pick a match, and the server looks it up.
/// Needs the server (the search and the lookup both run there).
struct AddByNameView: View {
    let kind: CatalogKind
    let onAdded: (String) -> Void

    @EnvironmentObject private var store: LibraryStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var results: [CatalogResult] = []
    @State private var note: String?
    @State private var searching = false
    @State private var adding = false
    @State private var searched = false

    var body: some View {
        NavigationStack {
            List {
                if let note { Text(note).font(.footnote).foregroundStyle(.secondary) }
                ForEach(results) { r in
                    Button { Task { await add(r) } } label: {
                        HStack(alignment: .top, spacing: 12) {
                            AsyncImage(url: r.imageUrl.flatMap(URL.init(string:))) { image in
                                image.resizable().scaledToFill()
                            } placeholder: {
                                Color(.secondarySystemBackground)
                            }
                            .frame(width: 44, height: 66)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                            VStack(alignment: .leading, spacing: 2) {
                                Text(r.title).font(.subheadline.weight(.semibold))
                                if let s = r.subtitle { Text(s).font(.caption).foregroundStyle(.secondary) }
                                if let o = r.overview { Text(o).font(.caption2).foregroundStyle(.secondary).lineLimit(3) }
                            }
                        }
                    }
                    .foregroundStyle(.primary)
                    .disabled(adding)
                }
                if searched, !query.trimmingCharacters(in: .whitespaces).isEmpty {
                    Button {
                        Task { await add(CatalogResult(title: query.trimmingCharacters(in: .whitespaces), year: nil, category: kind.rawValue)) }
                    } label: {
                        Label("Add “\(query.trimmingCharacters(in: .whitespaces))” as typed — look it up after adding", systemImage: "plus.circle")
                    }
                    .disabled(adding)
                }
            }
            .overlay { if searching || adding { ProgressView() } }
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Name of the \(kind.label.lowercased())")
            .onSubmit(of: .search) { Task { await search() } }
            .navigationTitle("Add a \(kind.label.lowercased())")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }

    private func search() async {
        guard let api = ServerSettings.client else { note = "Connect your Magpie server first (Settings)."; return }
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return }
        searching = true
        defer { searching = false }
        do {
            let found = try await api.catalogSearch(kind: kind.rawValue, query: q)
            results = found.results
            note = found.note
            searched = true
            if found.results.count == 1 { await add(found.results[0]) }   // exactly one hit: no need to ask
        } catch {
            note = "Searching needs your server: \(error.localizedDescription)"
        }
    }

    private func add(_ r: CatalogResult) async {
        guard let api = ServerSettings.client else { note = "Connect your Magpie server first (Settings)."; return }
        adding = true
        defer { adding = false }
        let id = "ios-" + UUID().uuidString.lowercased()
        do {
            let item = try await api.addEntry(EntryBody(
                category: r.category, title: r.title, year: r.year, author: r.author, overview: r.overview,
                imageUrl: r.imageUrl, tmdb: r.tmdb, imdbId: r.imdbId, id: id))
            store.adopt(item)
            onAdded(item.id)
            dismiss()
        } catch {
            note = error.localizedDescription
        }
    }
}

extension CatalogResult {
    init(title: String, year: Int?, category: String) {
        self.init(title: title, year: year, category: category, subtitle: nil, overview: nil, imageUrl: nil,
                  author: nil, tmdb: nil, imdbId: nil)
    }
}
