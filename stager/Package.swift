// swift-tools-version:5.9
// Linux build of NOOP's sleep stager. Sources/StrandAnalytics + Tests are symlinks made by ./link-noop.sh.
import PackageDescription

let package = Package(
    name: "whoop-stage",
    dependencies: [.package(path: "WhoopProtocol")],
    targets: [
        .target(name: "StrandAnalytics", dependencies: ["WhoopProtocol"]),
        .executableTarget(name: "whoop-stage", dependencies: ["StrandAnalytics", "WhoopProtocol"]),
        .testTarget(name: "StrandAnalyticsTests", dependencies: ["StrandAnalytics"]),
    ]
)
