// swift-tools-version: 5.10
// Variant with the share extension. Used by `stage.sh --with-share`; blocked on
// https://github.com/xtool-org/xtool/issues/379 (zsign cannot sign the extension stub).

import PackageDescription

let package = Package(
    name: "Magpie",
    platforms: [.iOS(.v17)],
    products: [
        .library(name: "Magpie", targets: ["Magpie"]),
        .library(name: "MagpieShare", targets: ["MagpieShare"]),
    ],
    targets: [
        .target(name: "Magpie"),
        .target(name: "MagpieShare"),
    ]
)
