// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "TensorMiner",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "TensorMiner", targets: ["TensorMiner"])
    ],
    targets: [
        .executableTarget(
            name: "TensorMiner",
            path: "TensorMiner",
            exclude: ["Info.plist", "TensorMiner.entitlements"],
            linkerSettings: [
                .linkedFramework("IOKit"),
                .linkedFramework("Metal"),
                .linkedFramework("AppKit")
            ]
        )
    ]
)
