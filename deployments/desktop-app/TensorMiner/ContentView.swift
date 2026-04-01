import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject var minerService: MinerService
    @State private var showingModelFilePicker = false
    @State private var showingRagFolderPicker = false

    var body: some View {
        VStack(spacing: 0) {
            // Header
            headerView

            Divider()

            // Main content
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    modelSection
                    brokerSection
                    workerSection
                    controlSection
                    statusSection
                }
                .padding()
            }

            Divider()

            // Log view
            logView
        }
        .frame(minWidth: 500, minHeight: 600)
        .fileImporter(
            isPresented: $showingModelFilePicker,
            allowedContentTypes: [UTType(filenameExtension: "gguf") ?? .data],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first {
                    minerService.config.modelPath = url.path
                }
            case .failure(let error):
                minerService.log("Error selecting file: \(error.localizedDescription)")
            }
        }
        .fileImporter(
            isPresented: $showingRagFolderPicker,
            allowedContentTypes: [.folder],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first {
                    minerService.config.ragContextPath = url.path
                }
            case .failure(let error):
                minerService.log("Error selecting RAG folder: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Header

    private var headerView: some View {
        HStack {
            Image(systemName: "cpu")
                .font(.title)
                .foregroundColor(.accentColor)

            VStack(alignment: .leading, spacing: 2) {
                Text("TensorMiner")
                    .font(.headline)
                Text("Compute Provider")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Spacer()

            statusBadge
        }
        .padding()
        .background(Color(NSColor.windowBackgroundColor))
    }

    private var statusBadge: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(minerService.isRunning ? Color.green : Color.gray)
                .frame(width: 8, height: 8)

            Text(minerService.isRunning ? "Running" : "Stopped")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(Color.secondary.opacity(0.1))
        .cornerRadius(12)
    }

    // MARK: - Model Section

    private var modelSection: some View {
        GroupBox(label: Label("Model", systemImage: "doc.fill")) {
            VStack(alignment: .leading, spacing: 12) {
                // GGUF file path
                HStack {
                    TextField("GGUF Model Path", text: $minerService.config.modelPath)
                        .textFieldStyle(.roundedBorder)
                        .disabled(minerService.isRunning)

                    Button("Browse...") {
                        showingModelFilePicker = true
                    }
                    .disabled(minerService.isRunning)
                }

                if !minerService.config.modelPath.isEmpty {
                    let url = URL(fileURLWithPath: minerService.config.modelPath)
                    Text(url.lastPathComponent)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }

                Divider()

                // Model identity (for valid mining)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Model Name (for mining)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("e.g., Qwen/Qwen3-8B", text: $minerService.config.modelName)
                        .textFieldStyle(.roundedBorder)
                        .disabled(minerService.isRunning)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Model Commit Hash")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("e.g., abc123...", text: $minerService.config.modelHash)
                        .textFieldStyle(.roundedBorder)
                        .disabled(minerService.isRunning)
                }
            }
            .padding(.vertical, 8)
        }
    }

    // MARK: - Broker Section

    private var brokerSection: some View {
        GroupBox(label: Label("Broker Connection", systemImage: "network")) {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("WebSocket URL")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("wss://broker.tensorcash.io/v1/ws", text: $minerService.config.brokerURL)
                        .textFieldStyle(.roundedBorder)
                        .disabled(minerService.isRunning)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("JWT Token")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    SecureField("eyJ...", text: $minerService.config.jwtToken)
                        .textFieldStyle(.roundedBorder)
                        .disabled(minerService.isRunning)
                }
            }
            .padding(.vertical, 8)
        }
    }

    // MARK: - Worker Section

    private var workerSection: some View {
        GroupBox(label: Label("Worker Settings", systemImage: "slider.horizontal.3")) {
            VStack(alignment: .leading, spacing: 16) {
                // Capacity slider
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Worker Capacity")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                        Text("\(Int(minerService.config.workerCapacity)) concurrent requests")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    Slider(value: $minerService.config.workerCapacity, in: 1...8, step: 1)
                        .disabled(minerService.isRunning)
                }

                // Context window
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Context Window")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                        Text("\(Int(minerService.config.contextWindow / 1024))K tokens")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    Slider(value: $minerService.config.contextWindow, in: 2048...131072, step: 2048)
                        .disabled(minerService.isRunning)
                }

                // GPU layers
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("GPU Layers")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                        Text(minerService.config.gpuLayers == 99 ? "All" : "\(Int(minerService.config.gpuLayers))")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    Slider(value: $minerService.config.gpuLayers, in: 0...99, step: 1)
                        .disabled(minerService.isRunning)
                }

                // Region
                HStack {
                    Text("Region")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                    Picker("", selection: $minerService.config.region) {
                        Text("US West").tag("us-west-2")
                        Text("US East").tag("us-east-1")
                        Text("EU West").tag("eu-west-1")
                        Text("AP Tokyo").tag("ap-northeast-1")
                    }
                    .pickerStyle(.menu)
                    .frame(width: 120)
                    .disabled(minerService.isRunning)
                }

                Divider()

                Toggle("Expose File Search Tool (RAG)", isOn: $minerService.config.ragToolEnabled)
                    .disabled(minerService.isRunning)

                if minerService.config.ragToolEnabled {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("RAG Context Folder")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        HStack {
                            TextField("Select a local folder to expose as tool context", text: $minerService.config.ragContextPath)
                                .textFieldStyle(.roundedBorder)
                                .disabled(minerService.isRunning)

                            Button("Browse...") {
                                showingRagFolderPicker = true
                            }
                            .disabled(minerService.isRunning)
                        }
                        Text("Registers worker tool `file_search` via `WORKER_TOOLS_JSON` at startup.")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }

                Toggle("Expose Local MCP Tool", isOn: $minerService.config.mcpToolEnabled)
                    .disabled(minerService.isRunning)

                if minerService.config.mcpToolEnabled {
                    VStack(alignment: .leading, spacing: 8) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Fallback MCP Tool ID")
                                .font(.caption)
                                .foregroundColor(.secondary)
                            TextField("mcp_proxy", text: $minerService.config.mcpToolId)
                                .textFieldStyle(.roundedBorder)
                                .disabled(minerService.isRunning)
                        }

                        VStack(alignment: .leading, spacing: 4) {
                            Text("MCP Endpoint URL")
                                .font(.caption)
                                .foregroundColor(.secondary)
                            TextField("http://127.0.0.1:9000/tool", text: $minerService.config.mcpEndpointURL)
                                .textFieldStyle(.roundedBorder)
                                .disabled(minerService.isRunning)
                        }

                        Text("On startup, TensorMiner discovers MCP tools from this endpoint and registers each one. If discovery fails, fallback tool `\(minerService.config.mcpToolId.isEmpty ? "mcp_proxy" : minerService.config.mcpToolId)` is registered.")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }
            }
            .padding(.vertical, 8)
        }
    }

    // MARK: - Control Section

    private var controlSection: some View {
        HStack {
            Spacer()

            if minerService.isRunning {
                Button(action: { minerService.stop() }) {
                    Label("Stop Mining", systemImage: "stop.fill")
                        .frame(width: 140)
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
            } else {
                Button(action: { minerService.start() }) {
                    Label("Start Mining", systemImage: "play.fill")
                        .frame(width: 140)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!minerService.canStart)
            }

            Spacer()
        }
        .padding(.vertical, 8)
    }

    // MARK: - Status Section

    private var statusSection: some View {
        GroupBox(label: Label("Status", systemImage: "chart.bar")) {
            VStack(alignment: .leading, spacing: 8) {
                statusRow("llama-server", status: minerService.llamaStatus)
                statusRow("miner-proxy", status: minerService.proxyStatus)
                statusRow("Broker", status: minerService.brokerStatus)

                if minerService.isRunning {
                    Divider()
                    HStack {
                        Text("Jobs Completed")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                        Text("\(minerService.jobsCompleted)")
                            .font(.caption.monospacedDigit())
                    }
                }
            }
            .padding(.vertical, 8)
        }
    }

    private func statusRow(_ name: String, status: MinerService.ProcessStatus) -> some View {
        HStack {
            Circle()
                .fill(status.color)
                .frame(width: 8, height: 8)
            Text(name)
                .font(.caption)
            Spacer()
            Text(status.description)
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    // MARK: - Log View

    private var logView: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Logs")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
                Button(action: { minerService.clearLogs() }) {
                    Image(systemName: "trash")
                        .font(.caption)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal)
            .padding(.vertical, 4)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(minerService.logs), id: \.id) { (entry: MinerService.LogEntry) in
                            Text(entry.message)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(entry.level.color)
                                .textSelection(.enabled)
                                .id(entry.id)
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, 4)
                }
                .onChange(of: minerService.logs.count) { _ in
                    if let last = minerService.logs.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
            .frame(height: 150)
            .background(Color(NSColor.textBackgroundColor))
        }
    }
}

// MARK: - Settings View

struct SettingsView: View {
    @EnvironmentObject var minerService: MinerService

    var body: some View {
        Form {
            Section("Paths") {
                TextField("llama-server", text: $minerService.config.llamaServerPath)
                TextField("miner-proxy", text: $minerService.config.minerProxyPath)
            }

            Section("Advanced") {
                Toggle("Enable Mining", isOn: $minerService.config.miningEnabled)
                Toggle("Debug Logging", isOn: $minerService.config.debugLogging)
            }
        }
        .padding()
        .frame(width: 400, height: 200)
    }
}

#if DEBUG
#Preview {
    ContentView()
        .environmentObject(MinerService())
}
#endif
