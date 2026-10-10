import CryptoKit
import Photos
import SwiftUI
import UIKit

/// A photo-library collection the user can clean up.
struct CleanupAlbum: Identifiable, Hashable {
    let id: String              // PHAssetCollection.localIdentifier
    let title: String
    let count: Int
    let symbol: String
}

struct CleanupMatch: Identifiable, Equatable, Sendable {
    let id: String              // PHAsset.localIdentifier
    let itemID: String
    let itemTitle: String
}

enum CleanupMode: String, CaseIterable, Identifiable {
    case dryRun, deleteAll
    var id: String { rawValue }
    var label: String { self == .dryRun ? "Dry run" : "Delete all" }
}

struct CleanupReport: Equatable {
    var mode: CleanupMode
    var scanned: Int
    var sent: Int               // photos shaped like some capture, so worth asking the server about
    var matches: [CleanupMatch]
    var deleted: Int
    var cancelled: Bool
}

/// Walks the photos in a collection and removes the ones that are already saved in Magpie.
///
/// The server decides what "already saved" means (POST /api/cleanup/check): the screenshot of a
/// finished capture, byte-identical or the same picture by look. Photos whose shape matches no
/// capture are never sent. Deleted photos go to Recently Deleted, where iOS keeps them for 30 days.
@MainActor
final class PhotoCleanup: ObservableObject {
    enum Phase: Equatable {
        case idle
        case needsAccess
        case preparing
        case scanning(done: Int, total: Int)
        case deleting
        case finished(CleanupReport)
        case failed(String)

        var isWorking: Bool {
            switch self {
            case .preparing, .scanning, .deleting: return true
            default: return false
            }
        }
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var albums: [CleanupAlbum] = []
    @Published private(set) var limitedAccess = false

    private var task: Task<Void, Never>?

    // MARK: - albums

    func loadAlbums() async {
        guard await authorize() else { return }
        limitedAccess = PHPhotoLibrary.authorizationStatus(for: .readWrite) == .limited
        albums = Self.fetchAlbums()
    }

    private func authorize() async -> Bool {
        var status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        if status == .notDetermined { status = await PHPhotoLibrary.requestAuthorization(for: .readWrite) }
        guard status == .authorized || status == .limited else {
            phase = .needsAccess
            return false
        }
        if phase == .needsAccess { phase = .idle }
        return true
    }

    private nonisolated static func imageOptions() -> PHFetchOptions {
        let o = PHFetchOptions()
        o.predicate = NSPredicate(format: "mediaType == %d", PHAssetMediaType.image.rawValue)
        return o
    }

    private static func fetchAlbums() -> [CleanupAlbum] {
        var result: [CleanupAlbum] = []
        let smart: [(PHAssetCollectionSubtype, String, String)] = [
            (.smartAlbumScreenshots, "Screenshots", "camera.viewfinder"),
            (.smartAlbumUserLibrary, "All Photos", "photo.on.rectangle"),
            (.smartAlbumRecentlyAdded, "Recents", "clock"),
            (.smartAlbumFavorites, "Favorites", "heart"),
        ]
        for (subtype, title, symbol) in smart {
            let found = PHAssetCollection.fetchAssetCollections(with: .smartAlbum, subtype: subtype, options: nil)
            guard let collection = found.firstObject else { continue }
            let count = PHAsset.fetchAssets(in: collection, options: imageOptions()).count
            if count > 0 { result.append(CleanupAlbum(id: collection.localIdentifier, title: title, count: count, symbol: symbol)) }
        }
        let user = PHAssetCollection.fetchAssetCollections(with: .album, subtype: .albumRegular, options: nil)
        user.enumerateObjects { collection, _, _ in
            let count = PHAsset.fetchAssets(in: collection, options: imageOptions()).count
            if count > 0 {
                result.append(CleanupAlbum(id: collection.localIdentifier, title: collection.localizedTitle ?? "Album",
                                           count: count, symbol: "rectangle.stack"))
            }
        }
        return result
    }

    // MARK: - running

    func start(album: CleanupAlbum, mode: CleanupMode) {
        guard !phase.isWorking else { return }
        task = Task { await run(album: album, mode: mode) }
    }

    func cancel() { task?.cancel() }

    private func run(album: CleanupAlbum, mode: CleanupMode) async {
        guard await authorize() else { return }
        guard let api = ServerSettings.client else {
            phase = .failed("Connect your Magpie server first: it decides which photos are already saved.")
            return
        }

        // 1. Which shapes can match at all (the first call also fingerprints every capture on the server).
        phase = .preparing
        let shapes: CleanupShapes
        do {
            shapes = try await api.cleanupShapes()
        } catch {
            phase = .failed("Couldn't reach the server: \(error.localizedDescription)")
            return
        }
        guard !shapes.shapes.isEmpty else {
            phase = .failed("Nothing to compare with yet: no finished captures on the server.")
            return
        }

        // 2. Photos of a matching shape, then ask the server about each, a few at a time.
        let collectionID = album.id
        let (scanned, candidates) = await Task.detached(priority: .userInitiated) {
            Self.candidates(collectionID: collectionID, shapes: shapes)
        }.value
        phase = .scanning(done: 0, total: candidates.count)

        var matches: [CleanupMatch] = []
        var done = 0
        var serverError: String?
        await withTaskGroup(of: Verdict.self) { group in
            var queue = candidates.makeIterator()
            for _ in 0..<3 {
                guard let id = queue.next() else { break }
                group.addTask { await Self.check(assetID: id, api: api) }
            }
            for await verdict in group {
                done += 1
                phase = .scanning(done: done, total: candidates.count)
                switch verdict {
                case .match(let match): matches.append(match)
                case .failed(let message): serverError = serverError ?? message
                case .noMatch: break
                }
                if Task.isCancelled || serverError != nil {
                    group.cancelAll()
                } else if let id = queue.next() {
                    group.addTask { await Self.check(assetID: id, api: api) }
                }
            }
        }
        if let serverError {
            phase = .failed("The server couldn't check photos: \(serverError)")
            return
        }

        let cancelled = Task.isCancelled
        var report = CleanupReport(mode: mode, scanned: scanned, sent: candidates.count, matches: matches, deleted: 0, cancelled: cancelled)

        // 3. Delete (one system confirmation for the whole batch).
        if mode == .deleteAll, !cancelled, !matches.isEmpty {
            phase = .deleting
            let ids = matches.map(\.id)
            do {
                try await PHPhotoLibrary.shared().performChanges {
                    let assets = PHAsset.fetchAssets(withLocalIdentifiers: ids, options: nil)
                    PHAssetChangeRequest.deleteAssets(assets)
                }
                report.deleted = ids.count
            } catch {
                // The user declined the system prompt, or the library refused.
                phase = .failed("Nothing was deleted: \(error.localizedDescription)")
                return
            }
        }
        phase = .finished(report)
    }

    /// Ids of the photos in the collection whose shape matches some capture.
    private nonisolated static func candidates(collectionID: String, shapes: CleanupShapes) -> (scanned: Int, ids: [String]) {
        guard let collection = PHAssetCollection.fetchAssetCollections(withLocalIdentifiers: [collectionID], options: nil).firstObject
        else { return (0, []) }
        let assets = PHAsset.fetchAssets(in: collection, options: imageOptions())
        var ids: [String] = []
        assets.enumerateObjects { asset, _, _ in
            guard asset.pixelHeight > 0, shapes.contains(Double(asset.pixelWidth) / Double(asset.pixelHeight)) else { return }
            ids.append(asset.localIdentifier)
        }
        return (assets.count, ids)
    }

    private enum Verdict: Sendable {
        case match(CleanupMatch)
        case noMatch
        case failed(String)
    }

    /// Sends one photo (shrunk, plus the SHA256 of the original) to the server.
    private nonisolated static func check(assetID: String, api: APIClient) async -> Verdict {
        guard !Task.isCancelled,
              let asset = PHAsset.fetchAssets(withLocalIdentifiers: [assetID], options: nil).firstObject,
              let original = await originalData(asset),
              let image = UIImage(data: original),
              let small = shrink(image) else { return .noMatch }   // not on this phone, or unreadable: keep it
        let sha = SHA256.hash(data: original).map { String(format: "%02x", $0) }.joined()
        do {
            let verdict = try await api.cleanupCheck(image: small, sha256: sha)
            guard verdict.match, let itemID = verdict.itemId else { return .noMatch }
            return .match(CleanupMatch(id: assetID, itemID: itemID, itemTitle: verdict.title ?? "Untitled"))
        } catch {
            return .failed(error.localizedDescription)
        }
    }

    /// The photo's bytes as it looks now (with any crop or markup), if they are on this phone (iCloud-only ones are skipped,
    /// not downloaded). The same version the photo picker and Share hand to the app, so the hashes can match.
    private nonisolated static func originalData(_ asset: PHAsset) async -> Data? {
        await withCheckedContinuation { cont in
            let options = PHImageRequestOptions()
            options.isNetworkAccessAllowed = false
            options.deliveryMode = .highQualityFormat
            options.version = .current            // the bytes as shared, so the hash can match the upload exactly
            PHImageManager.default().requestImageDataAndOrientation(for: asset, options: options) { data, _, _, _ in
                cont.resume(returning: data)
            }
        }
    }

    /// A JPEG at most 512 px wide: all the server needs to compare by look.
    private nonisolated static func shrink(_ image: UIImage) -> Data? {
        let scale = min(1, 512 / max(image.size.width, 1))
        let size = CGSize(width: max(1, (image.size.width * scale).rounded()), height: max(1, (image.size.height * scale).rounded()))
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: size, format: format).jpegData(withCompressionQuality: 0.85) { ctx in
            UIColor.white.setFill()
            ctx.fill(CGRect(origin: .zero, size: size))
            image.draw(in: CGRect(origin: .zero, size: size))
        }
    }
}
