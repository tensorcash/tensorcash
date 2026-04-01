import Foundation
import Combine
import IOKit
import SwiftUI

class MinerService: ObservableObject {
    // MARK: - Configuration

    struct Config {
        var modelPath: String = ""
        var brokerURL: String = ""
        var jwtToken: String = ""
        var workerCapacity: Double = 4
        var contextWindow: Double = 8192
        var gpuLayers: Double = 99
        var region: String = "us-west-2"
        var miningEnabled: Bool = true
        var debugLogging: Bool = false
        var ragToolEnabled: Bool = false
        var ragContextPath: String = ""
        var mcpToolEnabled: Bool = false
        var mcpToolId: String = "mcp_proxy"
        var mcpEndpointURL: String = ""

        // Model identity (must match broker's expected model)
        var modelName: String = ""      // e.g., "Qwen/Qwen3-8B"
        var modelHash: String = ""      // Commit hash for model verification

        // Paths to bundled binaries (set at runtime)
        var llamaServerPath: String = ""
        var minerProxyPath: String = ""
    }

    // MARK: - Process Status

    enum ProcessStatus: Equatable {
        case stopped
        case starting
        case running
        case error(String)

        var description: String {
            switch self {
            case .stopped: return "Stopped"
            case .starting: return "Starting..."
            case .running: return "Running"
            case .error(let msg): return "Error: \(msg)"
            }
        }

        var color: Color {
            switch self {
            case .stopped: return .gray
            case .starting: return .yellow
            case .running: return .green
            case .error: return .red
            }
        }
    }

    // MARK: - Log Entry

    struct LogEntry: Identifiable {
        let id = UUID()
        let timestamp: Date
        let message: String
        let level: LogLevel

        enum LogLevel {
            case info, warning, error, debug

            var color: Color {
                switch self {
                case .info: return .primary
                case .warning: return .orange
                case .error: return .red
                case .debug: return .secondary
                }
            }
        }
    }

    // MARK: - Published State

    @Published var config = Config()
    @Published var isRunning = false
    @Published var llamaStatus: ProcessStatus = .stopped
    @Published var proxyStatus: ProcessStatus = .stopped
    @Published var brokerStatus: ProcessStatus = .stopped
    @Published var jobsCompleted: Int = 0
    @Published var logs: [LogEntry] = []

    // MARK: - Private

    private var llamaProcess: Process?
    private var proxyProcess: Process?
    private var isStartingLlama = false
    private var isStartingProxy = false
    private var cancellables = Set<AnyCancellable>()
    private let maxLogs = 1000

    // MARK: - Computed

    var canStart: Bool {
        let hasBaseRequirements =
            !config.modelPath.isEmpty &&
            !config.brokerURL.isEmpty &&
            !config.jwtToken.isEmpty &&
            FileManager.default.fileExists(atPath: config.modelPath)

        if !hasBaseRequirements {
            return false
        }

        if config.ragToolEnabled {
            if config.ragContextPath.isEmpty ||
                !FileManager.default.fileExists(atPath: config.ragContextPath) {
                return false
            }
        }

        if config.mcpToolEnabled {
            let endpoint = config.mcpEndpointURL.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !endpoint.isEmpty else {
                return false
            }
            guard let url = URL(string: endpoint), let scheme = url.scheme else {
                return false
            }
            if scheme != "http" && scheme != "https" {
                return false
            }
        }

        return true
    }

    // MARK: - Init

    init() {
        let inAppBundle = Bundle.main.bundlePath.hasSuffix(".app")
        let searchPaths: [String]
        if inAppBundle {
            // Production app: only trust bundled resources.
            searchPaths = [Bundle.main.resourcePath].compactMap { $0 }
        } else {
            // Development runs can use build/output fallbacks.
            searchPaths = [
                Bundle.main.resourcePath,
                Bundle.main.executablePath.map { URL(fileURLWithPath: $0).deletingLastPathComponent().path },
                Bundle.main.executablePath.map { URL(fileURLWithPath: $0).deletingLastPathComponent().appendingPathComponent("../build/output").path },
                FileManager.default.currentDirectoryPath + "/build/output",
                NSHomeDirectory() + "/tensorcash/deployments/desktop-app/build/output"
            ].compactMap { $0 }
        }

        for basePath in searchPaths {
            let llamaPath = "\(basePath)/llama-server"
            let proxyPath = "\(basePath)/miner-proxy"

            if config.llamaServerPath.isEmpty && FileManager.default.fileExists(atPath: llamaPath) {
                config.llamaServerPath = llamaPath
                log("Found llama-server at \(llamaPath)", level: .info)
            }
            if config.minerProxyPath.isEmpty && FileManager.default.fileExists(atPath: proxyPath) {
                config.minerProxyPath = proxyPath
                log("Found miner-proxy at \(proxyPath)", level: .info)
            }

            if !config.llamaServerPath.isEmpty && !config.minerProxyPath.isEmpty {
                break
            }
        }

        // Load saved config (overrides auto-detected paths if saved)
        loadConfig()
    }

    // MARK: - Start/Stop

    func start() {
        guard !isRunning else {
            log("Services are already running", level: .warning)
            return
        }

        guard canStart else {
            log("Cannot start: missing configuration", level: .error)
            return
        }

        guard !config.llamaServerPath.isEmpty else {
            log("llama-server binary not found", level: .error)
            return
        }

        guard !config.minerProxyPath.isEmpty else {
            log("miner-proxy binary not found", level: .error)
            return
        }

        isRunning = true
        saveConfig()

        // Start llama-server first
        startLlamaServer()
    }

    func stop() {
        log("Stopping services...")
        isStartingLlama = false
        isStartingProxy = false

        // Stop proxy first
        if let process = proxyProcess, process.isRunning {
            process.terminate()
        }
        proxyProcess = nil
        proxyStatus = .stopped

        // Then stop llama-server
        if let process = llamaProcess, process.isRunning {
            process.terminate()
        }
        llamaProcess = nil
        llamaStatus = .stopped

        brokerStatus = .stopped
        isRunning = false

        log("Services stopped")
    }

    // MARK: - Process Management

    private func startLlamaServer() {
        if isStartingLlama || (llamaProcess?.isRunning ?? false) {
            log("llama-server is already starting/running", level: .warning)
            return
        }

        isStartingLlama = true
        llamaStatus = .starting
        log("Starting llama-server...")

        let process = Process()
        process.executableURL = URL(fileURLWithPath: config.llamaServerPath)
        var arguments = [
            "-m", config.modelPath,
            "--port", "8088",
            "--host", "127.0.0.1",
            "--ctx-size", String(Int(config.contextWindow)),
            "--n-gpu-layers", String(Int(config.gpuLayers)),
            "--mlock"
        ]
        // Tool/function calling in llama-server requires Jinja mode. Always enable —
        // client-supplied tools (chat completion tools=[...]) need this regardless of
        // whether worker-side tools (RAG/MCP) are configured. Previously gated on
        // ragToolEnabled || mcpToolEnabled, which broke generic OpenAI-style tool calls
        // for workers that did not opt into a worker-executed tool catalog.
        arguments.append("--jinja")

        // Override the GGUF's embedded chat_template with a canonical Jinja for
        // known-broken model families. Community Q4 quants often strip or break
        // tokenizer.chat_template, which makes llama.cpp's autoparser fail to
        // detect the `<tool_call>` marker — the lazy PEG grammar that constrains
        // tool_call JSON output is then never built, and the model emits
        // free-form garbage inside the tag. Matches the supervisor logic at
        // services/miner-api/llama_supervisor.py:resolve_chat_template_file.
        if let templateFile = resolveChatTemplateFile() {
            arguments.append("--chat-template-file")
            arguments.append(templateFile)
            log("Using chat-template-file: \(templateFile)")
        }
        process.arguments = arguments

        // Environment - include path to bundled libs
        var env = ProcessInfo.processInfo.environment
        env["LLAMA_METAL"] = "1"
        // Add libs directory to library path for bundled OpenSSL
        let libsPath = URL(fileURLWithPath: config.llamaServerPath).deletingLastPathComponent().appendingPathComponent("libs").path
        if let existingPath = env["DYLD_LIBRARY_PATH"] {
            env["DYLD_LIBRARY_PATH"] = "\(libsPath):\(existingPath)"
        } else {
            env["DYLD_LIBRARY_PATH"] = libsPath
        }
        process.environment = env

        // Capture output
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if let output = String(data: data, encoding: .utf8), !output.isEmpty {
                DispatchQueue.main.async {
                    self?.handleLlamaOutput(output)
                }
            }
        }

        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.handleLlamaTermination(exitCode: process.terminationStatus)
            }
        }

        do {
            try process.run()
            llamaProcess = process
            log("llama-server started (PID: \(process.processIdentifier))")

            // Wait for llama-server to be ready, then start proxy
            waitForLlamaReady()
        } catch {
            isStartingLlama = false
            llamaStatus = .error(error.localizedDescription)
            log("Failed to start llama-server: \(error.localizedDescription)", level: .error)
            isRunning = false
        }
    }

    /// Pick a canonical chat-template file when the configured model belongs to
    /// a known-broken family (community Q4 GGUFs whose embedded chat_template
    /// breaks llama.cpp's autoparser and skips the lazy PEG grammar that
    /// constrains tool_call JSON output). Mirrors the supervisor logic in
    /// services/miner-api/llama_supervisor.py:resolve_chat_template_file.
    ///
    /// Templates live alongside `llama-server` in the .app's Resources/
    /// (build-app.sh copies them in) under `chat-templates/`. Lookup uses a
    /// case-insensitive substring match across modelName and the GGUF
    /// basename so it works whether the user typed a HF repo id or just
    /// pointed at a local file. Returns nil when no override applies — the
    /// GGUF's own metadata is then used.
    private func resolveChatTemplateFile() -> String? {
        // Explicit override always wins (LLAMA_CHAT_TEMPLATE_FILE env var).
        if let explicit = ProcessInfo.processInfo.environment["LLAMA_CHAT_TEMPLATE_FILE"],
           !explicit.isEmpty {
            if (explicit as NSString).isAbsolutePath,
               FileManager.default.fileExists(atPath: explicit) {
                return explicit
            }
            // Fall back to treating it as a basename under the templates dir.
            if let dir = chatTemplateDirectory() {
                let candidate = "\(dir)/\(explicit)"
                if FileManager.default.fileExists(atPath: candidate) {
                    return candidate
                }
            }
            return nil
        }

        guard let dir = chatTemplateDirectory() else { return nil }

        let modelBasename = (config.modelPath as NSString).lastPathComponent
        let haystack = "\(config.modelName) \(modelBasename)".lowercased()

        // Single-source registry: keep in lockstep with
        // _CHAT_TEMPLATE_OVERRIDES in llama_supervisor.py.
        let overrides: [(needle: String, filename: String)] = [
            ("hermes", "hermes.jinja"),
        ]

        for (needle, filename) in overrides {
            if haystack.contains(needle) {
                let candidate = "\(dir)/\(filename)"
                if FileManager.default.fileExists(atPath: candidate) {
                    return candidate
                }
            }
        }
        return nil
    }

    private func chatTemplateDirectory() -> String? {
        // Env override for dev / sideload scenarios.
        if let envDir = ProcessInfo.processInfo.environment["LLAMA_CHAT_TEMPLATE_DIR"],
           !envDir.isEmpty,
           FileManager.default.fileExists(atPath: envDir) {
            return envDir
        }
        // Bundled location: Resources/chat-templates/ — populated by
        // deployments/desktop-app/scripts/build-app.sh from
        // services/miner-api/chat-templates/.
        if let resourcePath = Bundle.main.resourcePath {
            let bundled = "\(resourcePath)/chat-templates"
            if FileManager.default.fileExists(atPath: bundled) {
                return bundled
            }
        }
        // Fallback for `swift run` style dev: sibling of the llama-server
        // binary the service resolved at startup.
        if !config.llamaServerPath.isEmpty {
            let sibling = (URL(fileURLWithPath: config.llamaServerPath)
                .deletingLastPathComponent()
                .appendingPathComponent("chat-templates")).path
            if FileManager.default.fileExists(atPath: sibling) {
                return sibling
            }
        }
        return nil
    }

    private func waitForLlamaReady() {
        // Poll the health endpoint
        let url = URL(string: "http://127.0.0.1:8088/health")!
        var attempts = 0
        let maxAttempts = 60

        Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] timer in
            guard let self = self else {
                timer.invalidate()
                return
            }

            attempts += 1

            if attempts > maxAttempts {
                timer.invalidate()
                self.isStartingLlama = false
                self.log("llama-server health check timed out", level: .error)
                self.llamaStatus = .error("Timeout")
                self.stop()
                return
            }

            // Check if process died
            if self.llamaProcess == nil || !(self.llamaProcess?.isRunning ?? false) {
                timer.invalidate()
                self.isStartingLlama = false
                return
            }

            // Try health check
            var request = URLRequest(url: url)
            request.timeoutInterval = 2

            URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                    timer.invalidate()
                    DispatchQueue.main.async {
                        self?.isStartingLlama = false
                        self?.llamaStatus = .running
                        self?.log("llama-server is ready")
                        self?.startMinerProxy()
                    }
                }
            }.resume()
        }
    }

    private func startMinerProxy() {
        if isStartingProxy || (proxyProcess?.isRunning ?? false) {
            log("miner-proxy is already starting/running", level: .warning)
            return
        }

        isStartingProxy = true
        proxyStatus = .starting
        log("Starting miner-proxy...")

        let process = Process()
        process.executableURL = URL(fileURLWithPath: config.minerProxyPath)
        process.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory())

        // Environment for broker mode (standalone/desktop)
        var env = ProcessInfo.processInfo.environment
        env["WORKER_MODE"] = "broker"
        env["BROKER_WS_URL"] = config.brokerURL
        env["PROVIDER_JWT_TOKEN"] = config.jwtToken
        // Enable confidential mode by default in broker mode.
        // AGENT_ID is now auto-resolved from broker ACK (API-key introspection).
        env["CONFIDENTIAL_MODE_ENABLED"] = "true"
        env["WORKER_SUPPORTED_MODES"] = "plaintext,confidential"
        env["AUTH_SERVICE_URL"] = resolveAuthServiceURL(from: config.brokerURL)
        env["TARGET_URL"] = "http://127.0.0.1:8088"
        // Use port 8081 to avoid conflicts (8080 often in use)
        env["HTTP_HOST"] = "127.0.0.1"
        env["HTTP_PORT"] = "8081"
        // Disable HTTP server - broker mode is outbound-only via WSS
        // (requires rebuilt miner-proxy bundle to take effect)
        env["DISABLE_HTTP_SERVER"] = "true"
        env["WORKER_CAPACITY"] = String(Int(config.workerCapacity))
        // Context/output limits (required by broker for scheduling)
        env["MAX_CONTEXT_WINDOW"] = String(Int(config.contextWindow))
        env["MAX_OUTPUT_TOKENS"] = "8192"  // Default output limit for broker scheduling
        env["WORKER_REGION"] = config.region
        env["MINING_ENABLED"] = config.miningEnabled ? "true" : "false"
        env["LOG_LEVEL"] = config.debugLogging ? "DEBUG" : "INFO"
        env["PROOF_CACHE_ENABLED"] = "true"
        env["PROOF_COLLECTOR_PORT"] = "7002"
        // Standalone mode - skip remote model sync, use local model
        env["STANDALONE_MODE"] = "true"
        // Model identity for valid mining responses
        if !config.modelName.isEmpty {
            env["LOCAL_MODEL_NAME"] = config.modelName
            env["MODEL_NAME"] = config.modelName
        }
        if !config.modelHash.isEmpty {
            env["MODEL_HASH"] = config.modelHash
            env["MODEL_COMMIT"] = config.modelHash
        }

        // Metal/macOS specific capability advertisement
        env["COMPUTE_TYPE"] = "apple-metal"
        env["GPU_MODEL"] = getMetalGPUName()
        env["GPU_MEMORY_GB"] = String(getMetalMemoryGB())
        env["WORKER_TOOLS_JSON"] = buildWorkerToolsJSON()
        env["RAG_CONTEXT_PATH"] = ""
        env["MCP_TOOL_ENDPOINT"] = ""
        if config.ragToolEnabled {
            env["RAG_CONTEXT_PATH"] = config.ragContextPath
        }
        if config.mcpToolEnabled {
            env["MCP_TOOL_ENDPOINT"] = config.mcpEndpointURL.trimmingCharacters(in: .whitespacesAndNewlines)
        }

        var configuredTools: [String] = []
        if config.ragToolEnabled {
            configuredTools.append("file_search")
        }
        if config.mcpToolEnabled {
            let toolId = config.mcpToolId.trimmingCharacters(in: .whitespacesAndNewlines)
            configuredTools.append(toolId.isEmpty ? "mcp_proxy" : toolId)
        }
        if configuredTools.isEmpty {
            log("No worker tools configured")
        } else {
            log("Configured worker tools: \(configuredTools.joined(separator: ", "))")
        }
        process.environment = env

        // Capture output
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if let output = String(data: data, encoding: .utf8), !output.isEmpty {
                DispatchQueue.main.async {
                    self?.handleProxyOutput(output)
                }
            }
        }

        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.handleProxyTermination(exitCode: process.terminationStatus)
            }
        }

        do {
            try process.run()
            proxyProcess = process
            proxyStatus = .running
            isStartingProxy = false
            log("miner-proxy started (PID: \(process.processIdentifier))")
        } catch {
            isStartingProxy = false
            proxyStatus = .error(error.localizedDescription)
            log("Failed to start miner-proxy: \(error.localizedDescription)", level: .error)
            stop()
        }
    }

    private func resolveAuthServiceURL(from brokerURL: String) -> String {
        guard let url = URL(string: brokerURL), let host = url.host else {
            return "https://auth.tensorcash.org"
        }

        if host == "localhost" || host == "127.0.0.1" {
            return "http://localhost:8001"
        }

        if host == "compute.tensorcash.org" {
            return "https://auth.tensorcash.org"
        }

        if host.hasPrefix("compute.") {
            let suffix = host.dropFirst("compute.".count)
            let scheme = url.scheme ?? "https"
            return "\(scheme)://auth.\(suffix)"
        }

        return "https://auth.tensorcash.org"
    }

    private func buildWorkerToolsJSON() -> String {
        var tools: [[String: Any]] = []

        if config.ragToolEnabled {
            let schema: [String: Any] = [
                "type": "function",
                "function": [
                    "name": "file_search",
                    "description": "Search local files configured in TensorMiner RAG Context Folder.",
                    "parameters": [
                        "type": "object",
                        "properties": [
                            "query": [
                                "type": "string",
                                "description": "Search query over local worker context files",
                            ],
                            "k": [
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "description": "Maximum number of results",
                            ],
                        ],
                        "required": ["query"],
                        "additionalProperties": false,
                    ],
                ],
            ]
            if
                let schemaData = try? JSONSerialization.data(withJSONObject: schema, options: []),
                let schemaString = String(data: schemaData, encoding: .utf8)
            {
                tools.append(
                    [
                        "tool_id": "file_search",
                        "encryption": "aes-256-gcm",
                        "schema_ref": schemaString,
                        "executor": "file_search",
                    ]
                )
            }
        }

        if config.mcpToolEnabled {
            let toolId = config.mcpToolId.trimmingCharacters(in: .whitespacesAndNewlines)
            let resolvedToolId = toolId.isEmpty ? "mcp_proxy" : toolId
            let schema: [String: Any] = [
                "type": "function",
                "function": [
                    "name": resolvedToolId,
                    "description": "Forward a confidential tool request to the local MCP proxy endpoint configured in TensorMiner.",
                    "parameters": [
                        "type": "object",
                        "properties": [
                            "input": [
                                "type": "object",
                                "description": "MCP tool input payload",
                            ],
                            "query": [
                                "type": "string",
                                "description": "Optional natural language query",
                            ],
                        ],
                        "additionalProperties": true,
                    ],
                ],
            ]
            if
                let schemaData = try? JSONSerialization.data(withJSONObject: schema, options: []),
                let schemaString = String(data: schemaData, encoding: .utf8)
            {
                tools.append(
                    [
                        "tool_id": resolvedToolId,
                        "encryption": "aes-256-gcm",
                        "schema_ref": schemaString,
                        "executor": "mcp_proxy",
                    ]
                )
            }
        }

        guard
            let toolsData = try? JSONSerialization.data(withJSONObject: tools, options: []),
            let toolsString = String(data: toolsData, encoding: .utf8)
        else {
            return "[]"
        }

        return toolsString
    }

    // MARK: - Output Handlers

    private func handleLlamaOutput(_ output: String) {
        for line in output.components(separatedBy: .newlines) where !line.isEmpty {
            if config.debugLogging {
                log("[llama] \(line)", level: .debug)
            }

            // Detect ready state
            if line.contains("server listening") || line.contains("HTTP server listening") {
                llamaStatus = .running
            }
        }
    }

    private func handleProxyOutput(_ output: String) {
        for line in output.components(separatedBy: .newlines) where !line.isEmpty {
            // Parse log level
            let level: LogEntry.LogLevel
            if line.contains("ERROR") || line.contains("error") {
                level = .error
            } else if line.contains("WARNING") || line.contains("warning") {
                level = .warning
            } else if config.debugLogging {
                level = .debug
            } else {
                level = .info
            }

            log("[proxy] \(line)", level: level)

            // Detect broker connection
            if line.contains("Connected to broker") || line.contains("ACK received") {
                brokerStatus = .running
            }

            // Count completed jobs
            if line.contains("END message sent") || line.contains("Job completed") {
                jobsCompleted += 1
            }
        }
    }

    private func handleLlamaTermination(exitCode: Int32) {
        isStartingLlama = false
        if exitCode == 0 {
            llamaStatus = .stopped
            log("llama-server stopped normally")
        } else {
            llamaStatus = .error("Exit code: \(exitCode)")
            log("llama-server terminated with code: \(exitCode)", level: .error)
        }

        if isRunning {
            stop()
        }
    }

    private func handleProxyTermination(exitCode: Int32) {
        isStartingProxy = false
        if exitCode == 0 {
            proxyStatus = .stopped
            log("miner-proxy stopped normally")
        } else {
            proxyStatus = .error("Exit code: \(exitCode)")
            log("miner-proxy terminated with code: \(exitCode)", level: .error)
        }

        brokerStatus = .stopped

        if isRunning {
            stop()
        }
    }

    // MARK: - Metal GPU Detection

    private func getMetalGPUName() -> String {
        // Try IOKit first
        if let gpuName = getGPUNameFromIOKit() {
            return gpuName
        }

        // Fallback: detect Apple Silicon chip variant via sysctl
        var size: size_t = 0
        sysctlbyname("hw.machine", nil, &size, nil, 0)
        var machine = [CChar](repeating: 0, count: size)
        sysctlbyname("hw.machine", &machine, &size, nil, 0)
        let machineStr = String(cString: machine)

        // Check for specific chip identifiers
        // M3 family
        if machineStr.contains("Mac15") || machineStr.contains("Mac16") {
            return detectM3Variant()
        }
        // M2 family
        if machineStr.contains("Mac14") {
            return detectM2Variant()
        }
        // M1 family
        if machineStr.contains("Mac13") || machineStr.contains("Mac12") {
            return detectM1Variant()
        }

        return "Apple Silicon GPU"
    }

    private func getGPUNameFromIOKit() -> String? {
        let matchDict = IOServiceMatching("AGXAccelerator")
        var iterator: io_iterator_t = 0

        guard IOServiceGetMatchingServices(kIOMainPortDefault, matchDict, &iterator) == KERN_SUCCESS else {
            return nil
        }
        defer { IOObjectRelease(iterator) }

        var service = IOIteratorNext(iterator)
        while service != 0 {
            defer {
                IOObjectRelease(service)
                service = IOIteratorNext(iterator)
            }

            var properties: Unmanaged<CFMutableDictionary>?
            let result = IORegistryEntryCreateCFProperties(service, &properties, kCFAllocatorDefault, 0)

            guard result == KERN_SUCCESS, let props = properties?.takeRetainedValue() as? [String: Any] else {
                continue
            }

            // Try "model" key (may be Data or String)
            if let modelData = props["model"] as? Data,
               let name = String(data: modelData, encoding: .utf8)?.trimmingCharacters(in: .controlCharacters),
               !name.isEmpty {
                return name
            }

            if let name = props["model"] as? String, !name.isEmpty {
                return name
            }
        }

        return nil
    }

    private func detectM3Variant() -> String {
        let coreCount = ProcessInfo.processInfo.activeProcessorCount
        let memoryGB = Int(ProcessInfo.processInfo.physicalMemory / (1024 * 1024 * 1024))

        if memoryGB >= 128 || coreCount >= 24 { return "Apple M3 Max GPU" }
        if memoryGB >= 36 || coreCount >= 12 { return "Apple M3 Pro GPU" }
        return "Apple M3 GPU"
    }

    private func detectM2Variant() -> String {
        let coreCount = ProcessInfo.processInfo.activeProcessorCount
        let memoryGB = Int(ProcessInfo.processInfo.physicalMemory / (1024 * 1024 * 1024))

        if memoryGB >= 192 || coreCount >= 24 { return "Apple M2 Ultra GPU" }
        if memoryGB >= 64 || coreCount >= 12 { return "Apple M2 Max GPU" }
        if memoryGB >= 32 || coreCount >= 10 { return "Apple M2 Pro GPU" }
        return "Apple M2 GPU"
    }

    private func detectM1Variant() -> String {
        let coreCount = ProcessInfo.processInfo.activeProcessorCount
        let memoryGB = Int(ProcessInfo.processInfo.physicalMemory / (1024 * 1024 * 1024))

        if memoryGB >= 128 || coreCount >= 20 { return "Apple M1 Ultra GPU" }
        if memoryGB >= 64 || coreCount >= 10 { return "Apple M1 Max GPU" }
        if memoryGB >= 32 { return "Apple M1 Pro GPU" }
        return "Apple M1 GPU"
    }

    private func getMetalMemoryGB() -> Int {
        // On Apple Silicon, GPU uses unified memory
        let physicalMemory = ProcessInfo.processInfo.physicalMemory
        let totalGB = Int(physicalMemory / (1024 * 1024 * 1024))

        // GPU typically has access to ~75% of unified memory
        // Return a conservative estimate
        return max(8, (totalGB * 3) / 4)
    }

    // MARK: - Logging

    func log(_ message: String, level: LogEntry.LogLevel = .info) {
        let entry = LogEntry(
            timestamp: Date(),
            message: "[\(formatTime(Date()))] \(message)",
            level: level
        )

        logs.append(entry)

        // Trim old logs
        if logs.count > maxLogs {
            logs.removeFirst(logs.count - maxLogs)
        }
    }

    func clearLogs() {
        logs.removeAll()
    }

    private func formatTime(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }

    // MARK: - Config Persistence

    private func loadConfig() {
        let defaults = UserDefaults.standard

        if let modelPath = defaults.string(forKey: "modelPath") {
            config.modelPath = modelPath
        }
        if let brokerURL = defaults.string(forKey: "brokerURL") {
            config.brokerURL = brokerURL
        }
        if let jwtToken = defaults.string(forKey: "jwtToken") {
            config.jwtToken = jwtToken
        }
        if let region = defaults.string(forKey: "region") {
            config.region = region
        }
        if let ragContextPath = defaults.string(forKey: "ragContextPath") {
            config.ragContextPath = ragContextPath
        }
        if let mcpToolId = defaults.string(forKey: "mcpToolId") {
            config.mcpToolId = mcpToolId
        }
        if let mcpEndpointURL = defaults.string(forKey: "mcpEndpointURL") {
            config.mcpEndpointURL = mcpEndpointURL
        }

        config.workerCapacity = defaults.double(forKey: "workerCapacity")
        if config.workerCapacity == 0 { config.workerCapacity = 4 }

        config.contextWindow = defaults.double(forKey: "contextWindow")
        if config.contextWindow == 0 { config.contextWindow = 8192 }

        config.gpuLayers = defaults.double(forKey: "gpuLayers")
        if config.gpuLayers == 0 { config.gpuLayers = 99 }

        config.ragToolEnabled = defaults.bool(forKey: "ragToolEnabled")
        config.mcpToolEnabled = defaults.bool(forKey: "mcpToolEnabled")
        config.miningEnabled = defaults.bool(forKey: "miningEnabled")
        config.debugLogging = defaults.bool(forKey: "debugLogging")
    }

    private func saveConfig() {
        let defaults = UserDefaults.standard
        defaults.set(config.modelPath, forKey: "modelPath")
        defaults.set(config.brokerURL, forKey: "brokerURL")
        defaults.set(config.jwtToken, forKey: "jwtToken")
        defaults.set(config.region, forKey: "region")
        defaults.set(config.workerCapacity, forKey: "workerCapacity")
        defaults.set(config.contextWindow, forKey: "contextWindow")
        defaults.set(config.gpuLayers, forKey: "gpuLayers")
        defaults.set(config.ragToolEnabled, forKey: "ragToolEnabled")
        defaults.set(config.ragContextPath, forKey: "ragContextPath")
        defaults.set(config.mcpToolEnabled, forKey: "mcpToolEnabled")
        defaults.set(config.mcpToolId, forKey: "mcpToolId")
        defaults.set(config.mcpEndpointURL, forKey: "mcpEndpointURL")
        defaults.set(config.miningEnabled, forKey: "miningEnabled")
        defaults.set(config.debugLogging, forKey: "debugLogging")
    }
}
