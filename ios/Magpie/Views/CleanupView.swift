import Photos
import SwiftUI

/// Pick a photo collection (Screenshots, All Photos, an album…) and remove the photos that are
/// already saved in Magpie. "Dry run" only reports what would go.
struct CleanupView: View {
    @StateObject private var cleanup = PhotoCleanup()

    @State private var albumID: String?
    @State private var mode: CleanupMode = .dryRun
    @State private var confirmDelete = false

    private var selected: CleanupAlbum? { cleanup.albums.first { $0.id == albumID } }

    var body: some View {
        Form {
            switch cleanup.phase {
            case .needsAccess:
                Section {
                    Text("Magpie needs access to your photos to find the ones it has already saved.")
                    Button("Open Settings") {
                        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                    }
                }
            default:
                setup
            }
            progress
            result
        }
        .navigationTitle("Clean up photos")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await cleanup.loadAlbums()
            if albumID == nil { albumID = cleanup.albums.first?.id }
        }
        .confirmationDialog("Delete matching photos?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete matches", role: .destructive) { run() }
        } message: {
            Text("Photos already saved in Magpie are moved to Recently Deleted, where iOS keeps them for 30 days.")
        }
    }

    // MARK: - sections

    @ViewBuilder
    private var setup: some View {
        Section {
            ForEach(cleanup.albums) { album in
                Button {
                    albumID = album.id
                } label: {
                    HStack {
                        Label(album.title, systemImage: album.symbol)
                        Spacer()
                        Text("\(album.count)").foregroundStyle(.secondary)
                        if albumID == album.id { Image(systemName: "checkmark").foregroundStyle(.tint) }
                    }
                }
                .foregroundStyle(.primary)
            }
            if cleanup.albums.isEmpty {
                Text("No photos found.").foregroundStyle(.secondary)
            }
        } header: {
            Text("Look in")
        } footer: {
            if cleanup.limitedAccess {
                Text("Magpie only has access to some of your photos, so only those are checked. You can allow more in iOS Settings.")
            }
        }

        Section {
            Picker("Mode", selection: $mode) {
                ForEach(CleanupMode.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            Button {
                if mode == .deleteAll { confirmDelete = true } else { run() }
            } label: {
                Text(mode == .dryRun ? "Check photos" : "Delete matching photos")
                    .frame(maxWidth: .infinity)
            }
            .disabled(selected == nil || cleanup.phase.isWorking)
        } footer: {
            Text(mode == .dryRun
                 ? "Dry run only lists the photos that are already in Magpie. Nothing is deleted."
                 : "Deletes every photo in the chosen collection that matches a capture Magpie has identified. Captures still waiting for review or sync are never used.")
        }
    }

    @ViewBuilder
    private var progress: some View {
        switch cleanup.phase {
        case .preparing:
            Section { HStack { ProgressView(); Text("Reading your captures…").padding(.leading, 8) } }
        case .scanning(let done, let total):
            progressSection("Checking photos…", done: done, total: total)
        case .deleting:
            Section { HStack { ProgressView(); Text("Deleting…").padding(.leading, 8) } }
        default:
            EmptyView()
        }
    }

    private func progressSection(_ title: String, done: Int, total: Int) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Text(title)
                ProgressView(value: Double(done), total: Double(max(total, 1)))
                Text("\(done) of \(total)").font(.footnote).foregroundStyle(.secondary)
            }
            Button("Cancel", role: .cancel) { cleanup.cancel() }
        }
    }

    @ViewBuilder
    private var result: some View {
        switch cleanup.phase {
        case .failed(let message):
            Section { Label(message, systemImage: "xmark.octagon.fill").foregroundStyle(.red) }
        case .finished(let report):
            Section {
                LabeledContent("Photos checked", value: "\(report.scanned)")
                LabeledContent("Shaped like a capture", value: "\(report.sent)")
                LabeledContent(report.mode == .dryRun ? "Would delete" : "Deleted",
                               value: "\(report.mode == .dryRun ? report.matches.count : report.deleted)")
            } header: {
                Text(report.cancelled ? "Cancelled" : report.mode == .dryRun ? "Dry run result" : "Done")
            }
            if report.mode == .dryRun, !report.matches.isEmpty {
                Section("Already in Magpie") {
                    ForEach(report.matches.prefix(100)) { match in
                        HStack(spacing: 12) {
                            AssetThumbnail(assetID: match.id)
                            Text(match.itemTitle).lineLimit(2)
                        }
                    }
                    if report.matches.count > 100 {
                        Text("and \(report.matches.count - 100) more").foregroundStyle(.secondary)
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    private func run() {
        guard let selected else { return }
        cleanup.start(album: selected, mode: mode)
    }
}

private struct AssetThumbnail: View {
    let assetID: String
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Color.secondary.opacity(0.15)
            }
        }
        .frame(width: 44, height: 44)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .task {
            guard let asset = PHAsset.fetchAssets(withLocalIdentifiers: [assetID], options: nil).firstObject else { return }
            image = await withCheckedContinuation { cont in
                let options = PHImageRequestOptions()
                options.deliveryMode = .opportunistic
                var resumed = false
                PHImageManager.default().requestImage(for: asset, targetSize: CGSize(width: 132, height: 132),
                                                      contentMode: .aspectFill, options: options) { image, info in
                    // Opportunistic delivery calls back twice (degraded, then final); resume once.
                    guard !resumed else { return }
                    resumed = true
                    cont.resume(returning: image)
                }
            }
        }
    }
}
