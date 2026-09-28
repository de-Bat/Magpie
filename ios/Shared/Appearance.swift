import SwiftUI

/// Light / dark theme choice, shared with the share extension through the App Group.
/// `.system` follows the phone's setting (the default).
enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark

    static let storageKey = "appearance"
    /// App Group defaults, so the share extension matches the app; falls back to standard defaults.
    static let store = UserDefaults(suiteName: AppGroup.identifier) ?? .standard

    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

    /// nil lets SwiftUI follow the system setting.
    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }

    /// For UIKit hosts (the share extension's view controller).
    var interfaceStyle: UIUserInterfaceStyle {
        switch self {
        case .system: return .unspecified
        case .light: return .light
        case .dark: return .dark
        }
    }

    static var current: Appearance {
        Appearance(rawValue: store.string(forKey: storageKey) ?? "") ?? .system
    }
}
