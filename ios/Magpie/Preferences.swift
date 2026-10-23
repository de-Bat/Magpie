import SwiftUI

/// Per-device display choices, the counterpart of the web app's localStorage settings.
enum Preferences {
    static let columnsKey = "magpie.columns"      // 0 = automatic
    static let layoutKey = "magpie.layout"
    static let textSizeKey = "magpie.size"
    static let expandedCoverKey = "magpie.expandedCover"
}

enum LibraryLayout: String, CaseIterable, Identifiable {
    case grid, list
    var id: String { rawValue }
    var label: String { self == .grid ? "Grid" : "List" }
    var symbol: String { self == .grid ? "square.grid.2x2" : "list.bullet" }
}

/// The "Show" filter on the library screen.
enum ShowFilter: String, CaseIterable, Identifiable {
    case all, toCheck, notReviewed, reviewed, favorites, watchlist
    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All"
        case .toCheck: return "To check"
        case .notReviewed: return "Not reviewed"
        case .reviewed: return "Reviewed"
        case .favorites: return "Favorites"
        case .watchlist: return "Watchlist"
        }
    }

    var symbol: String {
        switch self {
        case .all: return "square.stack"
        case .toCheck: return "exclamationmark.triangle"
        case .notReviewed: return "circle"
        case .reviewed: return "checkmark.circle"
        case .favorites: return "star"
        case .watchlist: return "eye"
        }
    }

    func matches(_ item: Item) -> Bool {
        switch self {
        case .all: return true
        case .toCheck: return item.needsChecking
        case .notReviewed: return !item.reviewed
        case .reviewed: return item.reviewed
        case .favorites: return item.favorite
        case .watchlist: return item.watchlist
        }
    }
}

/// Text size, like the web app's Small / Standard / Large / Extra large.
enum TextSizeChoice: String, CaseIterable, Identifiable {
    case small, standard, large, xlarge
    var id: String { rawValue }

    var label: String {
        switch self {
        case .small: return "Small"
        case .standard: return "Standard"
        case .large: return "Large"
        case .xlarge: return "Extra large"
        }
    }

    var dynamicType: DynamicTypeSize {
        switch self {
        case .small: return .small
        case .standard: return .large
        case .large: return .xxxLarge
        case .xlarge: return .accessibility2
        }
    }
}
