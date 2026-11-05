import { useCallback, useEffect, useRef, useState } from "react";

type Session = {
  id: string;
  title?: string;
  status?: string;
  updated_at?: string;
};

type WorkspaceFile = {
  filename: string;
  size_bytes: number;
};

type Activity = {
  id: string;
  type: string;
  label: string;
  detail?: string;
  status: "running" | "complete" | "error";
};

type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  activities?: Activity[];
};

const API = "";

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function fileIcon(filename: string) {
  const ext = filename.split(".").pop()?.toLowerCase();
  if (ext === "pdf") return "PDF";
  if (ext === "xlsx") return "XLS";
  if (ext === "docx") return "DOC";
  if (["png", "jpg", "jpeg"].includes(ext || "")) return "IMG";
  if (ext === "csv") return "CSV";
  return "TXT";
}

function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [input, setInput] = useState("");
  const [selectedAttachment, setSelectedAttachment] = useState("");
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [activityOpen, setActivityOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [dragging, setDragging] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const activeSession = sessions.find((s) => s.id === activeId);

  const loadSessions = useCallback(async () => {
    const response = await fetch(`${API}/sessions`);
    if (!response.ok) throw new Error("Could not load sessions");
    const data = await response.json();
    setSessions(data.sessions || []);
    return data.sessions as Session[];
  }, []);

  const loadFiles = useCallback(async (sessionId: string) => {
    const response = await fetch(`${API}/sessions/${sessionId}/files`);
    if (!response.ok) throw new Error("Could not load workspace files");
    const data = await response.json();
    setFiles(data.files || []);
  }, []);

  const selectSession = useCallback(async (id: string) => {
    setActiveId(id);
    setMessages([]);
    setError("");
    try {
      const [sessionResponse, historyResponse] = await Promise.all([
        fetch(`${API}/sessions/${id}`),
        fetch(`${API}/sessions/${id}/messages`),
        loadFiles(id)
      ]);
      if (!sessionResponse.ok) throw new Error("Could not load session");
      if (!historyResponse.ok) throw new Error("Could not load conversation history");

      const history = await historyResponse.json();
      setMessages((history.messages || []).map((message: Message) => ({
        id: message.id,
        role: message.role,
        content: message.content,
        activities: []
      })));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load session");
    }
  }, [loadFiles]);

  useEffect(() => {
    loadSessions().then((items) => {
      if (items.length) selectSession(items[0].id);
    }).catch((err) => setError(err.message));
  }, [loadSessions, selectSession]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const createSession = async () => {
    setError("");
    try {
      const response = await fetch(`${API}/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "New chat" })
      });
      if (!response.ok) throw new Error("Could not create session");
      const session = await response.json();
      await loadSessions();
      await selectSession(session.id);
      inputRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create session");
    }
  };

  const updateMessage = (id: string, update: (message: Message) => Message) => {
    setMessages((current) => current.map((m) => m.id === id ? update(m) : m));
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || !activeId || loading) return;

    setInput("");
    setError("");
    setLoading(true);

    const assistantId = crypto.randomUUID();
    const userMessage: Message = {
      id: crypto.randomUUID(), role: "user", content: text
    };
    setMessages((current) => [
      ...current,
      userMessage,
      { id: assistantId, role: "assistant", content: "", activities: [] }
    ]);

    const addActivity = (activity: Activity) => {
      updateMessage(assistantId, (m) => ({
        ...m,
        activities: [...(m.activities || []), activity]
      }));
    };

    const updateActivity = (toolId: string, update: Partial<Activity>) => {
      updateMessage(assistantId, (m) => ({
        ...m,
        activities: (m.activities || []).map((a) =>
          a.id === toolId ? { ...a, ...update } : a
        )
      }));
    };

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch(`${API}/sessions/${activeId}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          attachment_filename: files.some((file) =>
            file.filename === selectedAttachment &&
            /\.(pdf|png|jpe?g)$/i.test(file.filename)
          ) ? selectedAttachment : null
        }),
        signal: controller.signal
      });

      if (!response.ok || !response.body) {
        throw new Error(`Chat request failed (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      const handleEvent = (raw: string) => {
        let eventType = "message";
        let data = "";
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) eventType = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) return;

        const payload = JSON.parse(data);

        if (eventType === "text") {
          updateMessage(assistantId, (m) => ({
            ...m, content: m.content + (payload.text || "")
          }));
        } else if (eventType === "tool_start") {
          addActivity({
            id: payload.tool_id || crypto.randomUUID(),
            type: "tool",
            label: payload.tool_name || "Tool execution",
            status: "running"
          });
        } else if (eventType === "tool_input") {
          updateMessage(assistantId, (m) => ({
            ...m,
            activities: (m.activities || []).map((a) =>
              a.id === payload.tool_id
                ? {
                    ...a,
                    detail: (a.detail || "") + (payload.partial_json || ""),
                    status: "running"
                  }
                : a
            )
          }));
        } else if (eventType === "tool_result") {
          updateActivity(payload.tool_id, {
            detail: typeof payload.content === "string"
              ? payload.content
              : JSON.stringify(payload.content),
            status: payload.is_error ? "error" : "complete"
          });
        } else if (eventType === "error") {
          setError(payload.message || "Agent execution failed");
          updateMessage(assistantId, (m) => ({
            ...m,
            activities: (m.activities || []).map((a) =>
              a.status === "running" ? { ...a, status: "error" } : a
            )
          }));
        } else if (eventType === "done") {
          setSessions((current) => current.map((s) =>
            s.id === activeId ? { ...s, updated_at: new Date().toISOString() } : s
          ));
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const event of events) {
          if (event.trim()) handleEvent(event);
        }
      }
      buffer = (buffer + decoder.decode()).replace(/\r\n/g, "\n");
      for (const event of buffer.split("\n\n")) {
        if (event.trim()) handleEvent(event);
      }
      await Promise.all([loadFiles(activeId), loadSessions()]);
    } catch (err) {
      if (err instanceof Error && err.name !== "AbortError") {
        setError(err.message);
      }
    } finally {
      setLoading(false);
      abortRef.current = null;
    }
  };

  const uploadFiles = async (selected: FileList | File[]) => {
    if (!activeId || !selected.length) return;
    setUploading(true);
    setError("");
    try {
      for (const file of Array.from(selected)) {
        const body = new FormData();
        body.append("file", file, file.name);
        const response = await fetch(`${API}/sessions/${activeId}/files`, {
          method: "POST",
          body
        });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.detail || `Upload failed: ${file.name}`);
        }
      }
      await loadFiles(activeId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const downloadFile = async (filename: string) => {
    if (!activeId) return;
    try {
      const path = filename.split("/").map(encodeURIComponent).join("/");
      const response = await fetch(`${API}/sessions/${activeId}/files/${path}`);
      if (!response.ok) throw new Error("Download failed");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename.split("/").pop() || filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    }
  };

  const cancelGeneration = () => abortRef.current?.abort();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">P</div>
          <div>
            <div className="brand-name">Persistent Agent</div>
            <div className="brand-subtitle">Your workspace, remembered</div>
          </div>
        </div>

        <button className="new-chat" onClick={createSession}>
          <span>＋</span> New chat
          <kbd>⌘ K</kbd>
        </button>

        <div className="side-label">YOUR CHATS</div>
        <div className="session-list">
          {sessions.map((session) => (
            <button
              key={session.id}
              className={`session-item ${activeId === session.id ? "active" : ""}`}
              onClick={() => selectSession(session.id)}
              title={session.id}
            >
              <span className="session-dot" />
              <span className="session-title">{session.title || "New chat"}</span>
            </button>
          ))}
          {!sessions.length && <div className="empty-side">No chats yet</div>}
        </div>

        <div className="sidebar-footer">
          <span className="status-indicator" />
          Workspace storage enabled
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>Workspace</span><span className="crumb-separator">/</span>
            <strong>{activeSession?.title || "New conversation"}</strong>
          </div>
          <div className="top-actions">
            <span className="connection-status"><i /> Connected</span>
            <button className="icon-button" title="Toggle activity panel" onClick={() => setActivityOpen(!activityOpen)}>☷</button>
            <button className="icon-button" title="Toggle files panel" onClick={() => setRightOpen(!rightOpen)}>▤</button>
          </div>
        </header>

        <section className="conversation">
          {!messages.length ? (
            <div className="welcome">
              <div className="welcome-icon">✳</div>
              <div className="eyebrow">PERSISTENT AI WORKSPACE</div>
              <h1>What are we working<br />on today?</h1>
              <p>Your files, code, and conversation context stay together.<br />Pick up where you left off, whenever you're ready.</p>
              <div className="suggestions">
                <button onClick={() => setInput("Analyze the files in my workspace and summarize the key findings.")}>
                  <span>↗</span><b>Analyze my files</b><small>Find insights across documents</small>
                </button>
                <button onClick={() => setInput("Use Python to analyze my data and create an Excel report.")}>
                  <span>▦</span><b>Create a report</b><small>Work with Python and spreadsheets</small>
                </button>
                <button onClick={() => setInput("Inspect the workspace files and explain their contents.")}>
                  <span>⌕</span><b>Explore workspace</b><small>Understand what's already here</small>
                </button>
              </div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message) => (
                <article className={`message ${message.role}`} key={message.id}>
                  <div className="message-avatar">{message.role === "user" ? "Y" : "✳"}</div>
                  <div className="message-body">
                    <div className="message-author">{message.role === "user" ? "You" : "Agent"}</div>
                    {message.content ? (
                      <div className="message-content">{message.content}</div>
                    ) : message.role === "assistant" && loading ? (
                      <div className="thinking"><i /><i /><i /> <span>Thinking</span></div>
                    ) : null}
                    {!!message.activities?.length && (
                      <div className="activity-list">
                        {message.activities.map((activity) => (
                          <div className="activity-item" key={activity.id}>
                            <span className={`activity-state ${activity.status}`}>
                              {activity.status === "complete" ? "✓" : activity.status === "error" ? "!" : "◌"}
                            </span>
                            <div className="activity-copy">
                              <b>{activity.label}</b>
                              {activity.detail && <pre>{activity.detail}</pre>}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </article>
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </section>

        {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError("")}>Dismiss</button></div>}

        <div className="composer-wrap">
          <div
            className={`composer ${dragging ? "dragging" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              uploadFiles(event.dataTransfer.files);
            }}
          >
            <textarea
              ref={inputRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  sendMessage();
                }
              }}
              placeholder={activeId ? "Message your agent..." : "Create a chat to get started"}
              disabled={!activeId || loading}
              rows={2}
            />
            <div className="composer-toolbar">
              <div className="composer-hint">
                <button title="Upload files" onClick={() => fileInputRef.current?.click()}>＋</button>
                <span>Attach files</span>
                <span className="hint-divider">·</span>
                <select
                  aria-label="Send file as native attachment"
                  value={selectedAttachment}
                  onChange={(event) => setSelectedAttachment(event.target.value)}
                  disabled={!activeId || loading}
                >
                  <option value="">Workspace files</option>
                  {files.filter((file) => /\.(pdf|png|jpe?g)$/i.test(file.filename)).map((file) => (
                    <option key={file.filename} value={file.filename}>{file.filename}</option>
                  ))}
                </select>
                <span>Shift + Enter for new line</span>
              </div>
              {loading ? (
                <button className="send-button stop" onClick={cancelGeneration}>Stop</button>
              ) : (
                <button className="send-button" onClick={sendMessage} disabled={!input.trim() || !activeId}>↑</button>
              )}
            </div>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".pdf,.docx,.xlsx,.csv,.txt,.png,.jpg,.jpeg"
            hidden
            onChange={(event) => event.target.files && uploadFiles(event.target.files)}
          />
          <div className="composer-disclaimer">AI can make mistakes. Verify important results and generated files.</div>
        </div>
      </main>

      {activityOpen && (
        <aside className="activity-panel">
          <div className="panel-heading">
            <div><span className="panel-eyebrow">LIVE</span><h3>Activity</h3></div>
            <button className="icon-button" onClick={() => setActivityOpen(false)}>×</button>
          </div>
          <div className="activity-overview">
            <div className="overview-icon">✳</div>
            <div><b>Agent runtime</b><small>Ready for tasks</small></div>
            <span className="ready-pill">Ready</span>
          </div>
          <div className="panel-divider" />
          <div className="panel-section-title">EXECUTION DETAILS</div>
          <div className="detail-row"><span>Session</span><code>{activeId ? activeId.slice(0, 8) : "—"}</code></div>
          <div className="detail-row"><span>Workspace</span><span className="detail-green">Persistent</span></div>
          <div className="detail-row"><span>Execution</span><span>Isolated</span></div>
          {messages.some((message) => message.activities?.length) ? (
            <div className="activity-list">
              {[...messages]
                .reverse()
                .flatMap((message) => [...(message.activities || [])].reverse())
                .slice(0, 10)
                .map((activity) => (
                  <div className="activity-item" key={activity.id}>
                    <span className={`activity-state ${activity.status}`}>
                      {activity.status === "complete" ? "✓" :
                       activity.status === "error" ? "!" : "◌"}
                    </span>
                    <div className="activity-copy">
                      <b>{activity.label}</b>
                      {activity.detail && <pre>{activity.detail}</pre>}
                    </div>
                  </div>
                ))}
            </div>
          ) : (
            <div className="activity-empty">
              <div>⌁</div>
              <b>No active operations</b>
              <span>Tool calls and execution events will appear here as they happen.</span>
            </div>
          )}
        </aside>
      )}

      {rightOpen && (
        <aside
          className="files-panel"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => { event.preventDefault(); uploadFiles(event.dataTransfer.files); }}
        >
          <div className="panel-heading">
            <div><span className="panel-eyebrow">SESSION STORAGE</span><h3>Files <span className="file-count">{files.length}</span></h3></div>
            <button className="icon-button" onClick={() => setRightOpen(false)}>×</button>
          </div>
          <button className="upload-button" onClick={() => fileInputRef.current?.click()} disabled={!activeId || uploading}>
            {uploading ? "Uploading..." : "＋ Upload files"}
          </button>
          <div className="file-list">
            {files.map((file) => (
              <button className="file-row" key={file.filename} onClick={() => downloadFile(file.filename)} title="Download file">
                <span className={`file-type type-${fileIcon(file.filename).toLowerCase()}`}>{fileIcon(file.filename)}</span>
                <span className="file-info"><b>{file.filename}</b><small>{formatSize(file.size_bytes)}</small></span>
                <span className="download-icon">↓</span>
              </button>
            ))}
            {!files.length && (
              <div className="files-empty">
                <div>▧</div><b>No files yet</b>
                <span>Upload documents or drop files<br />here to add them to this workspace.</span>
              </div>
            )}
          </div>
          <div className="storage-footer">
            <div className="storage-icon">◫</div>
            <div><b>Persistent storage</b><span>Files stay available between sessions.</span></div>
          </div>
        </aside>
      )}
    </div>
  );
}

export default App;
