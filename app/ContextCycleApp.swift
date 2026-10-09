// Context Cycle — a small native macOS front end for the context-cycle extractor.
// Lists the sessions of a project directory (or claude.ai chats / cloud sessions),
// and copies a selected session as a NEW Claude Code session into another project
// (or saves its condensed extract as Markdown), at the detail level you choose.
// All logic lives in the plugin's Python script; this app only drives it.

import SwiftUI
import AppKit

// MARK: - Model

struct SessionRow: Identifiable, Decodable, Hashable {
    let agent: String
    let session_id: String
    let project: String?
    let title: String?
    let user_messages: Int?
    let start: String?
    let end: String?
    let est_tokens: Int?
    let cloud_context_tokens: Double?
    let active: Bool?
    let path: String?
    var id: String { agent + ":" + session_id }
    var tokensLabel: String {
        if let t = est_tokens { return t >= 1000 ? String(format: "~%.1fk", Double(t) / 1000) : "~\(t)" }
        if let c = cloud_context_tokens { return String(format: "ctx ~%.0fk", c / 1000) }
        return "–"
    }
}

enum Source: String, CaseIterable, Identifiable {
    case project = "Project directory"
    case chats = "claude.ai chats (Claude Desktop)"
    case cloud = "claude.ai/code cloud sessions"
    var id: String { rawValue }
}

struct DetailLevel: Identifiable {
    let name: String
    let description: String
    let flags: [String]
    var id: String { name }
    static let all = [
        DetailLevel(name: "Full (recommended)",
                    description: "Everything: your messages, the agent's notes between tool calls, final answers, and every subagent's summary AND full report.",
                    flags: []),
        DetailLevel(name: "Without subagent full reports",
                    description: "Like Full, but subagent blocks keep only the short summary the main agent received.",
                    flags: ["--no-subagent-reports"]),
        DetailLevel(name: "Final answers only",
                    description: "Your messages and the agent's final answer per turn, plus subagent summaries. No agent notes.",
                    flags: ["--final-only", "--no-subagent-reports"]),
        DetailLevel(name: "Minimal",
                    description: "Only your messages and the agent's final answers. No notes, no subagent blocks.",
                    flags: ["--final-only", "--no-subagents"]),
    ]
}

struct CreateResult: Decodable {
    let session_id: String
    let path: String
    let project: String
}

struct BackendError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

// MARK: - Backend (drives extract_session.py)

enum Backend {
    /// The newest installed plugin copy of the script, else the copy bundled with the app.
    static func scriptPath() -> String? {
        let cache = NSHomeDirectory() + "/.claude/plugins/cache/hafnera/context-cycle"
        if let versions = try? FileManager.default.contentsOfDirectory(atPath: cache) {
            let sorted = versions.filter { $0.first?.isNumber == true }
                .sorted { $0.compare($1, options: .numeric) == .orderedAscending }
            for v in sorted.reversed() {
                let p = cache + "/\(v)/skills/cycle-context/scripts/extract_session.py"
                if FileManager.default.isExecutableFile(atPath: p) || FileManager.default.fileExists(atPath: p) { return p }
            }
        }
        if let bundled = Bundle.main.resourcePath.map({ $0 + "/scripts/extract_session.py" }),
           FileManager.default.fileExists(atPath: bundled) { return bundled }
        return nil
    }

    static func python() -> String {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/env"
    }

    static func run(_ args: [String], cwd: String? = nil) throws -> (out: String, err: String, code: Int32) {
        guard let script = scriptPath() else {
            throw BackendError(message: "extract_session.py not found: install the context-cycle plugin or rebuild the app.")
        }
        let proc = Process()
        let py = python()
        proc.executableURL = URL(fileURLWithPath: py)
        proc.arguments = (py == "/usr/bin/env" ? ["python3"] : []) + [script] + args
        if let cwd = cwd { proc.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONIOENCODING"] = "utf-8"
        proc.environment = env
        let outPipe = Pipe(), errPipe = Pipe()
        proc.standardOutput = outPipe; proc.standardError = errPipe
        try proc.run()
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return (String(decoding: outData, as: UTF8.self), String(decoding: errData, as: UTF8.self), proc.terminationStatus)
    }

    static func selectionArgs(for row: SessionRow, projectDir: String) -> [String] {
        switch row.agent {
        case "chat": return [row.session_id, "--agent", "chat", "--all-projects"]
        case "remote": return [row.session_id, "--agent", "remote", "--all-projects"]
        default: return [row.session_id, "--agent", row.agent, "--project", projectDir.precomposedStringWithCanonicalMapping]
        }
    }

    static func list(source: Source, projectDir: String) throws -> [SessionRow] {
        var args = ["list", "--json", "-n", "1000"]
        switch source {
        case .project: args += ["--project", projectDir.precomposedStringWithCanonicalMapping, "--agent", "all"]
        case .chats: args += ["--agent", "chat"]
        case .cloud: args += ["--agent", "remote"]
        }
        let r = try run(args)
        guard r.code == 0, let data = r.out.data(using: .utf8) else {
            throw BackendError(message: r.err.isEmpty ? "list failed (exit \(r.code))" : r.err.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        if r.out.hasPrefix("No sessions") { return [] }
        return try JSONDecoder().decode([SessionRow].self, from: data)
    }

    /// Size of the condensed extract at a detail level (chars / 4), via `extract -o /dev/null`.
    static func estimateTokens(row: SessionRow, projectDir: String, level: DetailLevel) -> Int? {
        guard let r = try? run(["extract"] + selectionArgs(for: row, projectDir: projectDir) + level.flags + ["-o", "/dev/null"]) else { return nil }
        guard let m = r.out.range(of: #"Wrote ([\d,]+) chars"#, options: .regularExpression) else { return nil }
        let digits = r.out[m].filter { $0.isNumber }
        return Int(digits).map { $0 / 4 }
    }

    static func create(row: SessionRow, projectDir: String, level: DetailLevel, into target: String) throws -> CreateResult {
        let r = try run(["create"] + selectionArgs(for: row, projectDir: projectDir) + level.flags
                        + ["--into", target.precomposedStringWithCanonicalMapping, "--json"])
        guard r.code == 0, let brace = r.out.firstIndex(of: "{"),
              let data = String(r.out[brace...]).data(using: .utf8) else {
            throw BackendError(message: r.err.isEmpty ? "create failed (exit \(r.code))\n\(r.out)" : r.err.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return try JSONDecoder().decode(CreateResult.self, from: data)
    }

    static func saveMarkdown(row: SessionRow, projectDir: String, level: DetailLevel, to url: URL) throws -> String {
        let r = try run(["extract"] + selectionArgs(for: row, projectDir: projectDir) + level.flags + ["-o", url.path])
        guard r.code == 0 else {
            throw BackendError(message: r.err.isEmpty ? "extract failed (exit \(r.code))" : r.err.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return r.out.trimmingCharacters(in: .whitespacesAndNewlines) + "\n" + r.err.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

func chooseDirectory(title: String, start: String?) -> String? {
    let panel = NSOpenPanel()
    panel.title = title; panel.message = title
    panel.canChooseDirectories = true; panel.canChooseFiles = false
    panel.allowsMultipleSelection = false; panel.canCreateDirectories = true
    if let s = start, !s.isEmpty { panel.directoryURL = URL(fileURLWithPath: s) }
    // precomposed (NFC): the panel returns decomposed Unicode, Claude Code names project dirs by NFC
    return panel.runModal() == .OK ? panel.url?.path.precomposedStringWithCanonicalMapping : nil
}

// MARK: - App

@main
struct ContextCycleApp: App {
    var body: some Scene {
        WindowGroup("Context Cycle") {
            ContentView().frame(minWidth: 820, minHeight: 480)
        }
        .windowResizability(.contentSize)
    }
}

struct ContentView: View {
    @AppStorage("projectDir") private var projectDir = NSHomeDirectory() + "/Coding"
    @AppStorage("source") private var sourceRaw = Source.project.rawValue
    @State private var rows: [SessionRow] = []
    @State private var filter = ""
    @State private var selection: SessionRow.ID?
    @State private var loading = false
    @State private var status = ""
    @State private var errorText: String?
    @State private var sheetMode: SheetMode?

    enum SheetMode: Identifiable { case copy(SessionRow), markdown(SessionRow)
        var id: String { switch self { case .copy(let r): return "copy:" + r.id; case .markdown(let r): return "md:" + r.id } } }

    private var source: Source { Source(rawValue: sourceRaw) ?? .project }
    private var filtered: [SessionRow] {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? rows : rows.filter { ($0.title ?? "").lowercased().contains(q) || $0.session_id.hasPrefix(q) }
    }
    private var selectedRow: SessionRow? { filtered.first { $0.id == selection } }

    var body: some View {
        VStack(spacing: 10) {
            HStack {
                Picker("Source", selection: $sourceRaw) {
                    ForEach(Source.allCases) { Text($0.rawValue).tag($0.rawValue) }
                }.frame(maxWidth: 330)
                if source == .project {
                    TextField("Project directory", text: $projectDir).textFieldStyle(.roundedBorder)
                        .onSubmit { reload() }
                    Button("Choose…") {
                        if let d = chooseDirectory(title: "Choose the project directory whose sessions to list", start: projectDir) {
                            projectDir = d; reload()
                        }
                    }
                }
                Button(action: reload) { Label("Reload", systemImage: "arrow.clockwise") }
                    .keyboardShortcut("r")
            }
            TextField("Filter by title or id", text: $filter).textFieldStyle(.roundedBorder)
            Table(filtered, selection: $selection) {
                TableColumn("Source") { Text($0.agent) }.width(min: 55, ideal: 60, max: 80)
                TableColumn("Last activity") { Text($0.end ?? "?") }.width(min: 120, ideal: 125, max: 140)
                TableColumn("Msgs") { Text($0.user_messages.map(String.init) ?? "?") }.width(min: 40, ideal: 45, max: 55)
                TableColumn("Size") { Text($0.tokensLabel) }.width(min: 60, ideal: 70, max: 90)
                TableColumn("Title") { r in
                    HStack { Text(r.title ?? r.session_id); if r.active == true { Text("ACTIVE").font(.caption).foregroundStyle(.orange) } }
                }
                TableColumn("Id") { Text(String($0.session_id.prefix(8))).font(.system(.body, design: .monospaced)) }.width(min: 70, ideal: 80, max: 90)
            }
            .overlay { if loading { ProgressView("Reading sessions…") } }
            HStack {
                if let e = errorText { Text(e).foregroundStyle(.red).lineLimit(2) } else { Text(status).foregroundStyle(.secondary).lineLimit(1) }
                Spacer()
                Button("Save extract as Markdown…") { if let r = selectedRow { sheetMode = .markdown(r) } }
                    .disabled(selectedRow == nil)
                Button("Copy as new session…") { if let r = selectedRow { sheetMode = .copy(r) } }
                    .keyboardShortcut(.defaultAction).disabled(selectedRow == nil)
            }
        }
        .padding(14)
        .onAppear(perform: reload)
        .onChange(of: sourceRaw) { _ in reload() }
        .sheet(item: $sheetMode) { mode in
            switch mode {
            case .copy(let r): CopySheet(row: r, projectDir: projectDir, markdownOnly: false)
            case .markdown(let r): CopySheet(row: r, projectDir: projectDir, markdownOnly: true)
            }
        }
    }

    private func reload() {
        loading = true; errorText = nil; selection = nil
        let src = source, dir = projectDir
        Task.detached {
            do {
                let list = try Backend.list(source: src, projectDir: dir)
                await MainActor.run {
                    rows = list; loading = false
                    status = "\(list.count) session(s) · script: \(Backend.scriptPath() ?? "not found") · \(Backend.python())"
                }
            } catch {
                await MainActor.run { rows = []; loading = false; errorText = error.localizedDescription }
            }
        }
    }
}

struct CopySheet: View {
    let row: SessionRow
    let projectDir: String
    let markdownOnly: Bool
    @Environment(\.dismiss) private var dismiss
    @AppStorage("targetDir") private var targetDir = ""
    @State private var levelIndex = 0
    @State private var estimates: [Int?] = Array(repeating: nil, count: DetailLevel.all.count)
    @State private var running = false
    @State private var errorText: String?
    @State private var result: CreateResult?
    @State private var savedNote: String?

    private var level: DetailLevel { DetailLevel.all[levelIndex] }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(markdownOnly ? "Save extract as Markdown" : "Copy as a new session into another project").font(.title2).bold()
            Text("\(row.title ?? row.session_id)  ·  \(row.agent)  ·  \(row.end ?? "")").foregroundStyle(.secondary)
            Divider()
            if let res = result {
                resultView(res)
            } else if let note = savedNote {
                Text("Saved.").bold(); Text(note).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
            } else {
                Text("Detail level").bold()
                ForEach(Array(DetailLevel.all.enumerated()), id: \.offset) { i, lv in
                    HStack(alignment: .top) {
                        Toggle(isOn: Binding(get: { levelIndex == i }, set: { if $0 { levelIndex = i } })) { EmptyView() }
                            .toggleStyle(.checkbox).labelsHidden()
                        VStack(alignment: .leading) {
                            HStack { Text(lv.name).bold(); Spacer()
                                Text(estimates[i].map { $0 >= 1000 ? String(format: "~%.1fk tokens", Double($0) / 1000) : "~\($0) tokens" } ?? "…")
                                    .foregroundStyle(.secondary).font(.callout) }
                            Text(lv.description).font(.callout).foregroundStyle(.secondary)
                        }
                    }.contentShape(Rectangle()).onTapGesture { levelIndex = i }
                }
                if !markdownOnly {
                    Text("Target project directory").bold().padding(.top, 4)
                    HStack {
                        TextField("The project the new session belongs to", text: $targetDir).textFieldStyle(.roundedBorder)
                        Button("Choose…") { if let d = chooseDirectory(title: "Choose the target project directory", start: targetDir) { targetDir = d } }
                    }
                    Text("The new session appears in that project's /resume list as “Imported: …”. Tool calls, tool results and thinking are not included.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if let e = errorText { Text(e).foregroundStyle(.red).textSelection(.enabled) }
                HStack {
                    if running { ProgressView().controlSize(.small); Text("Working…").foregroundStyle(.secondary) }
                    Spacer()
                    Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                    if markdownOnly {
                        Button("Save…") { saveMarkdown() }.keyboardShortcut(.defaultAction).disabled(running)
                    } else {
                        Button("Create session") { create() }.keyboardShortcut(.defaultAction)
                            .disabled(running || targetDir.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
        }
        .padding(20).frame(width: 640)
        .task { loadEstimates() }
    }

    @ViewBuilder private func resultView(_ res: CreateResult) -> some View {
        let cmd = "cd \"\(res.project)\" && claude --resume \(res.session_id)"
        Text("Session created").bold()
        Text("Open it in Terminal with:").foregroundStyle(.secondary)
        Text(cmd).font(.system(.body, design: .monospaced)).textSelection(.enabled)
            .padding(8).background(Color(nsColor: .textBackgroundColor)).cornerRadius(6)
        Text("Or start Claude Code in \(res.project) and pick “Imported: …” in /resume.").font(.callout).foregroundStyle(.secondary)
        HStack {
            Button("Copy command") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(cmd, forType: .string) }
            Button("Reveal session file") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: res.path)]) }
            Spacer()
            Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
        }
    }

    private func loadEstimates() {
        let r = row, dir = projectDir
        Task.detached {
            for (i, lv) in DetailLevel.all.enumerated() {
                let est = Backend.estimateTokens(row: r, projectDir: dir, level: lv)
                await MainActor.run { estimates[i] = est }
            }
        }
    }

    private func create() {
        running = true; errorText = nil
        let r = row, dir = projectDir, lv = level, target = targetDir
        Task.detached {
            do {
                let res = try Backend.create(row: r, projectDir: dir, level: lv, into: target)
                await MainActor.run { result = res; running = false }
            } catch {
                await MainActor.run { errorText = error.localizedDescription; running = false }
            }
        }
    }

    private func saveMarkdown() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = (row.title ?? row.session_id).replacingOccurrences(of: "/", with: "-").prefix(60) + "-session-extract.md"
        panel.allowedContentTypes = [.init(filenameExtension: "md") ?? .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        running = true; errorText = nil
        let r = row, dir = projectDir, lv = level
        Task.detached {
            do {
                let note = try Backend.saveMarkdown(row: r, projectDir: dir, level: lv, to: url)
                await MainActor.run { savedNote = url.path + "\n" + note; running = false }
            } catch {
                await MainActor.run { errorText = error.localizedDescription; running = false }
            }
        }
    }
}
